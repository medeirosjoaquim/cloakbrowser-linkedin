"""FastAPI-layer tests. The browser is stubbed so these are fast and offline."""

import hashlib
import json
import re
import shutil
import subprocess
import uuid

import pytest
from fastapi.testclient import TestClient

import app as appmod
import browser

KRUNCHER = "https://www.linkedin.com/company/kruncher/"
PROFILE = "https://www.linkedin.com/in/joaquim-medeiros/"


@pytest.fixture
def client(monkeypatch):
    # Don't let the lifespan kick off a real login; default to logged_in.
    monkeypatch.setattr(browser, "start_login", lambda: None)
    monkeypatch.setattr(
        browser, "login_state",
        lambda: {"state": "logged_in", "detail": "", "logged_in": True},
    )
    # The result cache is module-level; clear it per-test so stubbed scrapes
    # don't satisfy a later test's request (cache-pollution).
    browser._cache_clear()
    return TestClient(appmod.app)


def test_index_serves_ui(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "LinkedIn scraper" in r.text
    assert "/extract" in r.text       # the UI posts here
    assert "/login/code" in r.text    # and submits the confirmation code here


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_index_js_parses(client, tmp_path):
    """Guard against the Python-string-eats-JS-escape trap (a raw newline inside
    a JS string literal silently breaks the whole UI)."""
    js = re.search(r"<script>(.*?)</script>", client.get("/").text, re.S).group(1)
    f = tmp_path / "ui.js"
    f.write_text(js)
    proc = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    "url,ok",
    [
        (KRUNCHER, True),
        ("https://linkedin.com/company/kruncher/", True),
        ("https://www.linkedin.com/in/someone", True),
        ("https://evil-linkedin.com.attacker.net/x", False),
        ("https://example.com", False),
        ("not a url", False),
    ],
)
def test_is_linkedin(url, ok):
    assert appmod._is_linkedin(url) is ok


def test_extract_requires_url(client):
    assert client.post("/extract", json={}).status_code == 400


def test_extract_rejects_non_linkedin(client):
    r = client.post("/extract", json={"url": "https://example.com"})
    assert r.status_code == 400


def test_extract_blocked_until_logged_in(monkeypatch):
    monkeypatch.setattr(browser, "start_login", lambda: None)
    monkeypatch.setattr(
        browser, "login_state",
        lambda: {"state": "awaiting_code", "detail": "code required", "logged_in": False},
    )
    c = TestClient(appmod.app)
    r = c.post("/extract", json={"url": PROFILE})
    assert r.status_code == 409
    assert "awaiting_code" in r.json()["detail"]


def test_extract_profile_success(client, monkeypatch):
    fake = {
        "requested_url": PROFILE,
        "final_url": PROFILE,
        "title": "Joaquim Medeiros | LinkedIn",
        "html": "<html><body>profile</body></html>",
        "text": "Joaquim Medeiros\nSenior Full-Stack Engineer",
        "name": "Joaquim Medeiros",
        "headline": "Senior Full-Stack Engineer",
        "location": "Curitiba, Paraná, Brazil",
        "top_card_lines": ["Joaquim Medeiros", "Senior Full-Stack Engineer"],
        "sections": {
            "about": {"title": "About", "text": "10+ years building systems", "links": []},
            "experience": {"title": "Experience", "text": "Senior Full-Stack Engineer", "links": []},
        },
    }
    monkeypatch.setattr(browser, "fetch_html", lambda url, force=False: fake)

    r = client.post("/extract", json={"url": PROFILE})
    assert r.status_code == 200
    d = r.json()
    assert d["type"] == "profile"
    assert d["name"] == "Joaquim Medeiros"
    assert d["headline"] == "Senior Full-Stack Engineer"
    assert d["location"] == "Curitiba, Paraná, Brazil"
    assert set(d["sections"]) == {"about", "experience"}
    assert d["sections"]["about"]["text"] == "10+ years building systems"
    assert d["html_length"] == len(fake["html"])
    assert d["text_length"] == len(d["text"])
    assert "fetched_at" in d


def test_extract_full_company(client, monkeypatch):
    header = "Kruncher\nFinancial Services\nHome\nAbout\nPosts"

    def raw(seg):
        return {
            "requested_url": f"https://www.linkedin.com/company/kruncher/{seg}",
            "final_url": f"https://www.linkedin.com/company/kruncher/{seg}",
            "title": f"Kruncher {seg}",
            "html": f"<html><body>section {seg}</body></html>",
            "text": f"{header}\nSection {seg} about kruncher.",
            "name": "Kruncher",
            "headline": "The Knowledge Infrastructure for Private Markets",
            "location": None,
            "top_card_lines": ["Kruncher"],
            "sections": {},
        }

    monkeypatch.setattr(
        browser, "fetch_company",
        lambda url, force=False: {
            "base_url": "https://www.linkedin.com/company/kruncher/",
            "slug": "kruncher",
            "sections": {"home": raw("home"), "about": raw("about")},
        },
    )

    r = client.post("/extract", json={"url": KRUNCHER, "full": True})
    assert r.status_code == 200
    d = r.json()
    assert d["type"] == "company"
    assert d["slug"] == "kruncher"
    assert set(d["sections"]) == {"home", "about"}
    assert d["header"] == "Kruncher\nFinancial Services\nHome\nAbout\nPosts"
    for sec in d["sections"].values():
        assert sec["html_length"] > 0
        assert sec["text_length"] == len(sec["text"] or "")
        # the shared company-card header is stripped from each section body
        assert "Financial Services" not in sec["text"]
        assert sec["text"].startswith("Section ")
    assert "fetched_at" in d


def test_strip_common_header_edge_cases():
    one = {"home": {"text": "Kruncher\nOnly section"}}
    assert appmod._strip_common_header(one) == ""
    assert one["home"]["text"] == "Kruncher\nOnly section"

    no_shared = {
        "a": {"text": "Alpha body", "text_length": 10},
        "b": {"text": "Beta body", "text_length": 9},
    }
    assert appmod._strip_common_header(no_shared) == ""
    assert no_shared["a"]["text"] == "Alpha body"


def test_extract_render_failure_returns_502(client, monkeypatch):
    def boom(url, force=False):
        raise RuntimeError("render boom")

    monkeypatch.setattr(browser, "fetch_html", boom)
    r = client.post("/extract", json={"url": PROFILE})
    assert r.status_code == 502
    assert "render boom" in r.json()["detail"]


def test_health_and_login_status(client, monkeypatch):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    # login + pool stats (stable, asserted exactly)
    assert body["status"] == "ok"
    assert body["state"] == "logged_in"
    assert "instance" in body  # per-replica identity for LB/debugging (#11)
    assert body["detail"] == ""
    assert body["logged_in"] is True
    # scrape pool off by default (size 1 = legacy single-worker path)
    assert body["enabled"] is False
    assert body["size"] == 1
    assert body["in_flight"] == 0
    assert body["queue_depth"] == 0
    # runtime/cache stats present (#9) — just check the keys exist
    for k in ("cache_enabled", "cache_size", "cache_hits", "cache_misses",
              "cache_coalesced", "last_scrape_ok", "consecutive_failures"):
        assert k in body, f"missing runtime stat {k!r}"
    # circuit breaker (#8) state present
    for k in ("open", "wall_hits", "threshold"):
        assert k in body, f"missing breaker stat {k!r}"
    assert body["open"] is False

    r = client.get("/login/status")
    assert r.json()["state"] == "logged_in"


def test_login_code_success(client, monkeypatch):
    monkeypatch.setattr(browser, "submit_code", lambda code: {"ok": True, "state": "logged_in"})
    r = client.post("/login/code", json={"code": "123456"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_login_code_rejected_returns_409(client, monkeypatch):
    monkeypatch.setattr(
        browser, "submit_code",
        lambda code: {"ok": False, "state": "awaiting_code", "error": "code not accepted"},
    )
    r = client.post("/login/code", json={"code": "000000"})
    assert r.status_code == 409
    assert "not accepted" in r.json()["detail"]


def test_login_code_requires_code(client):
    r = client.post("/login/code", json={"code": ""})
    assert r.status_code == 400


def test_auth_disabled_when_unset(client, monkeypatch):
    # default fixture leaves API_AUTH_SHA256 unset -> no auth required
    monkeypatch.setattr(appmod, "API_AUTH_SHA256", "")
    assert client.get("/health").status_code == 200


def test_auth_required_when_set(monkeypatch):
    secret = "test-secret-not-real"
    monkeypatch.setattr(browser, "start_login", lambda: None)
    monkeypatch.setattr(
        browser, "login_state",
        lambda: {"state": "logged_in", "detail": "", "logged_in": True},
    )
    monkeypatch.setattr(appmod, "API_AUTH_SHA256", hashlib.sha256(secret.encode()).hexdigest())
    c = TestClient(appmod.app)

    assert c.get("/health").status_code == 401                       # no creds
    assert c.get("/health", headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.get("/health", headers={"Authorization": f"Bearer {secret}"}).status_code == 200
    assert c.get("/health", headers={"X-API-Key": secret}).status_code == 200


def test_company_url_helper():
    assert appmod._company_url("https://www.linkedin.com/company/freda-ab/").endswith("/company/freda-ab/")
    assert appmod._company_url("freda-ab") == "https://www.linkedin.com/company/freda-ab/"
    assert appmod._company_url("https://www.linkedin.com/in/someone") is None  # not a company
    assert appmod._company_url("https://example.com/company/x") is None
    assert appmod._company_url("") is None


def test_company_envelope_shape(client, monkeypatch):
    fake_company = {
        "companyName": "Freda", "companyId": 107067644, "tagline": "Autonomous compliance.",
        "employeeCount": 22, "followerCount": 1105, "websiteUrl": "www.freda.com",
        "universalName": "freda-ab", "url": "https://www.linkedin.com/company/freda-ab/",
    }
    posts = [{"urn": "urn:li:activity:1", "text": "hello", "numLikes": 3}]
    monkeypatch.setattr(
        browser, "fetch_company_api",
        lambda url, force=False: {"slug": "freda-ab", "found": True, "company": fake_company, "posts": posts, "error": None},
    )
    r = client.post("/company", json={"url": "https://www.linkedin.com/company/freda-ab/"})
    assert r.status_code == 200
    d = r.json()
    assert d["responseCode"] == "1000"
    assert d["error"] == ""
    assert d["website"] == "freda.com"  # www. stripped
    assert uuid.UUID(d["analysisId"])  # valid uuid
    content = d["array_content"]["content"]
    assert d["array_content"]["pdf_path"] is None
    pro = next(c for c in content if c["type"] == "LINKEDIN_COMPANY_PRO")
    assert pro["html_dump"] == fake_company
    assert json.loads(pro["text_dump"])["companyId"] == 107067644  # text_dump is the JSON string
    assert pro["meta"]["hero_section"] == "Autonomous compliance."
    post_entry = next(c for c in content if c["type"] == "LINKEDIN_POST")
    assert json.loads(post_entry["text_dump"]) == posts
    assert "html_dump" not in post_entry


def test_company_not_found(client, monkeypatch):
    monkeypatch.setattr(
        browser, "fetch_company_api",
        lambda url, force=False: {"slug": "nope", "found": False, "company": None, "posts": [], "error": "company http 404"},
    )
    d = client.post("/company", json={"url": "nope"}).json()
    assert d["responseCode"] == "1001"
    assert "404" in d["error"]
    # still returns the (empty) posts entry
    assert d["array_content"]["content"][0]["type"] == "LINKEDIN_POST"


def test_company_rejects_bad_url(client):
    assert client.post("/company", json={"url": "https://example.com/x"}).status_code == 400
    assert client.post("/company", json={}).status_code == 400


def test_profile_url_helper():
    assert appmod._profile_url("https://www.linkedin.com/in/joaquim-medeiros/").endswith("/in/joaquim-medeiros/")
    assert appmod._profile_url("joaquim-medeiros") == "https://www.linkedin.com/in/joaquim-medeiros/"
    assert appmod._profile_url("https://www.linkedin.com/company/kruncher") is None
    assert appmod._profile_url("") is None


def test_profile_envelope(client, monkeypatch):
    data = {
        "slug": "joaquim-medeiros", "found": True, "name": "Joaquim Medeiros",
        "headline": "Senior Full-Stack Engineer", "location": "Curitiba, Brazil",
        "about": "10+ years…", "skills": ["React", "Node.js"],
        "experience": [{"company": "LumiMeds", "title": "Team Lead", "dateRange": "Oct 2025 - Present"}],
        "education": [{"school": "PUC-PR", "degree": "Law", "dates": "2002 – 2006"}],
        "licenses": [], "sections": {},
    }
    monkeypatch.setattr(browser, "fetch_profile_api", lambda url, force=False: data)
    r = client.post("/profile", json={"url": "joaquim-medeiros"})
    assert r.status_code == 200
    d = r.json()
    assert d["responseCode"] == "1000"
    pro = next(c for c in d["array_content"]["content"] if c["type"] == "LINKEDIN_PROFILE_PRO")
    assert pro["html_dump"]["name"] == "Joaquim Medeiros"
    assert pro["html_dump"]["experience"][0]["company"] == "LumiMeds"
    assert json.loads(pro["text_dump"])["education"][0]["school"] == "PUC-PR"


def test_profile_not_found(client, monkeypatch):
    monkeypatch.setattr(browser, "fetch_profile_api",
                        lambda url, force=False: {"slug": "x", "found": False, "name": None, "experience": [], "error": "empty"})
    d = client.post("/profile", json={"url": "x"}).json()
    assert d["responseCode"] == "1001"
    assert d["array_content"]["content"] == []


def test_profile_rejects_bad_url(client):
    assert client.post("/profile", json={"url": "https://example.com/x"}).status_code == 400
    assert client.post("/profile", json={}).status_code == 400


def test_company_blocked_until_logged_in(monkeypatch):
    monkeypatch.setattr(browser, "start_login", lambda: None)
    monkeypatch.setattr(browser, "login_state", lambda: {"state": "awaiting_code", "detail": "", "logged_in": False})
    c = TestClient(appmod.app)
    r = c.post("/company", json={"url": "freda-ab"})
    assert r.status_code == 409


def test_login_reset(client, monkeypatch):
    calls = {"reset": 0, "start": 0}
    monkeypatch.setattr(browser, "reset_login", lambda: calls.__setitem__("reset", calls["reset"] + 1) or {"state": "idle"})
    monkeypatch.setattr(browser, "start_login", lambda: calls.__setitem__("start", calls["start"] + 1))
    r = client.post("/login/reset")
    assert r.status_code == 200
    assert r.json()["reset"] is True
    assert calls["reset"] == 1
    assert calls["start"] == 1  # fresh login re-triggered in the background


def test_extract_returns_409_when_breaker_open(client, monkeypatch):
    """When the circuit breaker is open (session degraded), an uncached scrape
    must fast-fail with 409 + Retry-After instead of burning a full goto."""
    with browser._state_lock:
        was_open = browser._cb_open
        was_until = browser._cb_open_until
        browser._cb_open = True
        browser._cb_open_until = browser.time.monotonic() + 60
    try:
        r = client.post("/extract", json={"url": "https://www.linkedin.com/in/someone/"})
        assert r.status_code == 409
        assert r.headers.get("retry-after") == "5"
    finally:
        with browser._state_lock:
            browser._cb_open = was_open
            browser._cb_open_until = was_until


def test_top_card_strips_connection_degree_badge():
    """Regression: the '· 1st' / pt-BR '· 2º' connection-degree badge rendered in
    the top card must not be returned as the headline."""
    h, loc = browser._top_card_fields(
        ["Eugene Kim", "· 1st", "CEO at Foo", "Redwood City, CA"], "Eugene Kim"
    )
    assert h == "CEO at Foo"
    h2, _ = browser._top_card_fields(["Simone", "· 2º", "Engineer"], "Simone")
    assert h2 == "Engineer"
    # a real headline containing '1st' must survive (whole-line match only)
    h3, _ = browser._top_card_fields(["Jane", "Building 1st products"], "Jane")
    assert h3 == "Building 1st products"


# --- async job model (#12) --------------------------------------------------

def test_extract_async_returns_202_then_completes(client, monkeypatch):
    """?async=1 enqueues the scrape and returns 202 + job_id immediately; polling
    /jobs/{id} transitions pending → done with the payload."""
    import time as _t
    monkeypatch.setattr(browser, "fetch_html",
                        lambda url, force=False: {"requested_url": url, "final_url": url,
                                                  "title": "T", "html": "<html/>",
                                                  "text": "x", "name": None,
                                                  "headline": None, "location": None,
                                                  "top_card_lines": [], "sections": {}})
    r = client.post("/extract?async=1", json={"url": "https://www.linkedin.com/in/x/"})
    assert r.status_code == 202
    assert r.headers["location"].startswith("/jobs/")
    job_id = r.json()["job_id"]
    assert r.json()["status"] == "pending"

    # poll until done (the job runs on a real thread pool)
    view = None
    for _ in range(50):
        view = client.get(f"/jobs/{job_id}").json()
        if view["status"] != "pending":
            break
        _t.sleep(0.05)
    assert view["status"] == "done"
    assert view["result"]["type"] == "profile"


def test_async_job_captures_failure(client, monkeypatch):
    def boom(url, force=False):
        raise RuntimeError("nope")
    monkeypatch.setattr(browser, "fetch_profile_api", boom)
    r = client.post("/profile?async=1", json={"url": "joaquim-medeiros"})
    job_id = r.json()["job_id"]
    import time as _t
    view = None
    for _ in range(50):
        view = client.get(f"/jobs/{job_id}").json()
        if view["status"] != "pending":
            break
        _t.sleep(0.05)
    assert view["status"] == "error"
    assert "nope" in view["error"]


def test_unknown_job_returns_404(client):
    r = client.get("/jobs/does-not-exist")
    assert r.status_code == 404


def test_job_sse_stream_emits_result_event(client, monkeypatch):
    """GET /jobs/{id}/events streams heartbeats then a terminal event (#6)."""
    import time as _t
    monkeypatch.setattr(browser, "fetch_html",
                        lambda url, force=False: {"requested_url": url, "final_url": url,
                                                  "title": "T", "html": "<html/>", "text": "x",
                                                  "name": None, "headline": None, "location": None,
                                                  "top_card_lines": [], "sections": {}})
    jid = client.post("/extract?async=1", json={"url": "https://www.linkedin.com/in/x/"}).json()["job_id"]

    seen = []
    grab = 0
    with client.stream("GET", f"/jobs/{jid}/events") as resp:
        assert resp.status_code == 200
        for raw in resp.iter_lines():
            seen.append(raw)
            if grab > 0:
                grab -= 1
                if grab == 0:
                    break
                continue
            if raw.startswith("event: done") or raw.startswith("event: error"):
                grab = 2  # capture the data line(s) that follow the terminal event
    text = "\n".join(seen)
    assert "event: done" in text
    assert '"status": "done"' in text


def test_job_sse_unknown_job_404(client):
    r = client.get("/jobs/nope/events")
    assert r.status_code == 404
