"""browser.py logic tests. cloakbrowser is faked so nothing real launches."""

import types

import pytest

import browser

KRUNCHER = "https://www.linkedin.com/company/kruncher/"
FEED = "https://www.linkedin.com/feed/"
AUTHWALL = "https://www.linkedin.com/authwall"
CHECKPOINT = "https://www.linkedin.com/checkpoint/challenge/"


@pytest.mark.parametrize(
    "url,ok",
    [
        (KRUNCHER, True),
        (FEED, True),
        ("https://www.linkedin.com/authwall?trk=bf", False),
        ("https://www.linkedin.com/login", False),
        (CHECKPOINT, False),
        ("https://example.com/", False),
    ],
)
def test_authed(url, ok):
    assert browser._authed(url) is ok


# --- fakes for the login + render flow ---------------------------------------

class _Mouse:
    def wheel(self, *a, **k):
        pass


class _Keyboard:
    def press(self, *a, **k):
        pass


class _Btn:
    def __init__(self, page):
        self._page = page

    def click(self, *a, **k):
        if self._page.ctx.submit_url is not None:
            self._page.ctx.url = self._page.ctx.submit_url


class _Page:
    """A scriptable fake page. URL transitions are driven by the ctx."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.mouse = _Mouse()
        self.keyboard = _Keyboard()

    def goto(self, url, **k):
        if "feed" in url:
            self.ctx.url = self.ctx.feed_url
        elif "login" in url:
            self.ctx.url = self.ctx.login_landing
        else:
            self.ctx.url = url

    @property
    def url(self):
        return self.ctx.url

    def type(self, *a, **k):
        pass

    def fill(self, *a, **k):
        pass

    def wait_for_selector(self, *a, **k):
        pass

    def click(self, sel=None, *a, **k):  # only the submit click advances the URL
        if sel and "submit" in str(sel) and self.ctx.submit_url is not None:
            self.ctx.url = self.ctx.submit_url

    def wait_for_url(self, pred, timeout=0):
        pass

    def query_selector(self, sel):
        if "submit" in sel:
            return _Btn(self)
        if self.ctx.pin and ("pin" in sel or "verification" in sel or sel == "input[type=tel]"):
            return object()  # a (visible) pin input
        return None

    def content(self):
        return self.ctx.html

    def title(self):
        return self.ctx.title

    def evaluate(self, js, *args):
        if "data-auto-login" in js:
            return {"email": True, "password": True, "submit": True}
        if "voyager/api/organization" in js:  # _COMPANY_API_JS
            slug = args[0] if args else None
            return {"found": True, "company": {"universalName": slug, "companyName": "X"},
                    "posts": [{"urn": "urn:li:activity:1", "text": "hi"}], "error": None}
        if "profileCards" in js:  # _PROFILE_API_JS
            slug = args[0] if args else None
            return {"found": True, "name": "Joaquim", "headline": "Engineer", "location": "BR",
                    "about": "about", "experience": [{"company": "X", "title": "Eng"}],
                    "education": [{"school": "U"}], "licenses": [], "skills": ["py"], "sections": {}}
        if "scrollHeight" in js:
            return 1000  # constant height -> _scroll_all settles quickly
        if "section_order" in js:
            return {
                "name": "Kruncher",
                "top_card_lines": ["Kruncher", "Infra for private markets", "Redwood City, California"],
                "section_order": ["about"],
                "sections": {"about": {"title": "About", "text": "about kruncher", "links": []}},
            }
        if "innerText" in js:
            return "clean visible text"
        return None

    def close(self):
        pass


class _Ctx:
    def __init__(self, *, feed_url=AUTHWALL, login_landing=AUTHWALL, submit_url=None,
                 pin=False, html="<html>kruncher</html>", title="Kruncher | LinkedIn"):
        self.url = "about:blank"
        self.feed_url = feed_url
        self.login_landing = login_landing
        self.submit_url = submit_url
        self.pin = pin
        self.html = html
        self.title = title
        self.persisted = False

    def new_page(self):
        return _Page(self)

    def storage_state(self, path=None):
        self.persisted = True

    def cookies(self):
        return [{"name": "li_at", "value": "x"}]

    def close(self):
        pass


@pytest.fixture(autouse=True)
def reset(monkeypatch, tmp_path):
    """Neutralize side effects and reset login state before each test."""
    monkeypatch.setattr(browser, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(browser.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(browser, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(browser, "PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(browser, "_ctx", None)
    monkeypatch.setattr(browser, "_pending_page", None)
    browser._set_state("idle")
    # The result cache + single-flight map + circuit breaker are module-level;
    # clear them between tests so one test can't affect another.
    browser._cache_clear()
    with browser._inflight_lock:
        browser._inflight.clear()
    with browser._state_lock:
        browser._wall_hits = 0
        browser._cb_open = False
        browser._cb_open_until = 0.0
    monkeypatch.delenv("LINKEDIN_EMAIL", raising=False)
    monkeypatch.delenv("LINKEDIN_PASSWORD", raising=False)


def _install(monkeypatch, ctx):
    monkeypatch.setattr(
        browser, "cb",
        types.SimpleNamespace(launch_persistent_context=lambda *a, **k: ctx),
    )
    return ctx


# --- login state machine -----------------------------------------------------

def test_reuses_valid_session(monkeypatch):
    ctx = _install(monkeypatch, _Ctx(feed_url=FEED))
    st = browser.start_login()
    assert st["state"] == "logged_in"
    assert ctx.persisted is False  # nothing re-persisted when a session is reused


def test_missing_creds_sets_no_credentials(monkeypatch):
    _install(monkeypatch, _Ctx(feed_url=AUTHWALL))
    st = browser.start_login()
    assert st["state"] == "no_credentials"


def test_password_login_success(monkeypatch):
    monkeypatch.setenv("LINKEDIN_EMAIL", "a@b.c")
    monkeypatch.setenv("LINKEDIN_PASSWORD", "pw")
    ctx = _install(monkeypatch, _Ctx(feed_url=AUTHWALL, submit_url=FEED))
    st = browser.start_login()
    assert st["state"] == "logged_in"
    assert ctx.persisted is True


def test_challenge_parks_at_awaiting_code_then_code_logs_in(monkeypatch):
    monkeypatch.setenv("LINKEDIN_EMAIL", "a@b.c")
    monkeypatch.setenv("LINKEDIN_PASSWORD", "pw")
    ctx = _install(monkeypatch, _Ctx(feed_url=AUTHWALL, submit_url=CHECKPOINT, pin=True))
    st = browser.start_login()
    assert st["state"] == "awaiting_code"  # does NOT raise; waits for the code
    assert browser._pending_page is not None

    # correct code -> the challenge page resolves to the feed
    ctx.submit_url = FEED
    result = browser.submit_code("123456")
    assert result["ok"] is True
    assert browser.login_state()["state"] == "logged_in"
    assert ctx.persisted is True


def test_wrong_code_stays_awaiting(monkeypatch):
    monkeypatch.setenv("LINKEDIN_EMAIL", "a@b.c")
    monkeypatch.setenv("LINKEDIN_PASSWORD", "pw")
    ctx = _install(monkeypatch, _Ctx(feed_url=AUTHWALL, submit_url=CHECKPOINT, pin=True))
    browser.start_login()
    # code rejected -> still on the checkpoint
    ctx.submit_url = CHECKPOINT
    result = browser.submit_code("000000")
    assert result["ok"] is False
    assert browser.login_state()["state"] == "awaiting_code"


def test_submit_code_when_not_awaiting_is_rejected(monkeypatch):
    _install(monkeypatch, _Ctx(feed_url=FEED))
    browser.start_login()  # -> logged_in, not awaiting
    result = browser.submit_code("123456")
    assert result["ok"] is False


def test_reset_clears_session_and_profile(monkeypatch, tmp_path):
    ctx = _install(monkeypatch, _Ctx(feed_url=FEED))
    browser.start_login()
    assert browser.login_state()["state"] == "logged_in"
    # simulate an on-disk profile to be wiped
    prof = tmp_path / "profile"
    prof.mkdir()
    (prof / "cookies.sqlite").write_text("x")
    monkeypatch.setattr(browser, "PROFILE_DIR", str(prof))

    st = browser.reset_login()
    assert st["state"] == "idle"
    assert browser._ctx is None
    assert browser.cookies() is None
    assert not prof.exists()  # profile dir removed


# --- rendering / structured extraction ---------------------------------------

def test_ops_run_on_a_single_worker_thread(monkeypatch):
    """Regression: Playwright is thread-bound, so every op must run on ONE
    worker thread no matter which thread calls the public API."""
    import threading

    impl_threads = []

    def fake_impl(url):
        impl_threads.append(threading.get_ident())
        return {"requested_url": url, "final_url": url, "title": "", "html": ""}

    monkeypatch.setattr(browser, "_fetch_html_impl", fake_impl)
    caller_threads = set()

    def call():
        caller_threads.add(threading.get_ident())
        browser.fetch_html(KRUNCHER)

    workers = [threading.Thread(target=call) for _ in range(6)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    assert len(set(impl_threads)) == 1  # all work funneled to one thread
    assert impl_threads[0] not in caller_threads  # and it's the worker, not callers


def test_fetch_html_returns_sections_and_diagnostics(monkeypatch):
    ctx = _Ctx(feed_url=KRUNCHER)
    ctx.url = KRUNCHER
    monkeypatch.setattr(browser, "_ctx", ctx)
    result = browser.fetch_html(KRUNCHER)
    assert result["final_url"] == KRUNCHER
    assert result["title"] == "Kruncher | LinkedIn"
    assert result["name"] == "Kruncher"
    assert result["headline"] == "Infra for private markets"
    assert result["location"] == "Redwood City, California"
    assert "about" in result["sections"]
    assert result["sections"]["about"]["text"] == "about kruncher"
    assert "kruncher" in result["html"]
    assert result["text"] == "clean visible text"


def test_company_slug():
    assert browser._company_slug(KRUNCHER) == "kruncher"
    assert browser._company_slug("https://www.linkedin.com/company/kruncher/about/") == "kruncher"
    assert browser._company_slug("https://www.linkedin.com/in/someone") is None


def test_discover_sections_from_nav():
    html = (
        '<a href="/company/kruncher/about/">About</a>'
        '<a href="/company/kruncher/posts/?feedView=all">Posts</a>'
        '<a href="/company/kruncher/jobs/">Jobs</a>'
        '<a href="/company/kruncher/people/">People</a>'
    )
    assert browser._discover_sections(html, "kruncher") == ["about", "posts", "jobs", "people"]


def test_fetch_company_visits_each_section(monkeypatch):
    home_html = (
        '<a href="/company/kruncher/about/">About</a>'
        '<a href="/company/kruncher/posts/">Posts</a>'
    )
    visited = []

    def fake_impl(u):
        visited.append(u)
        html = home_html if u.endswith("/company/kruncher/") else "<html>section</html>"
        return {"requested_url": u, "final_url": u, "title": "t", "html": html}

    monkeypatch.setattr(browser, "_fetch_html_impl", fake_impl)
    result = browser.fetch_company(KRUNCHER)

    assert result["slug"] == "kruncher"
    assert set(result["sections"]) == {"home", "about", "posts"}
    assert visited == [
        "https://www.linkedin.com/company/kruncher/",
        "https://www.linkedin.com/company/kruncher/about/",
        "https://www.linkedin.com/company/kruncher/posts/",
    ]


def test_fetch_company_api(monkeypatch):
    ctx = _Ctx(feed_url=KRUNCHER)
    ctx.url = KRUNCHER
    monkeypatch.setattr(browser, "_ctx", ctx)
    data = browser.fetch_company_api(KRUNCHER)
    assert data["slug"] == "kruncher"
    assert data["found"] is True
    assert data["company"]["universalName"] == "kruncher"
    assert data["posts"][0]["text"] == "hi"


def test_fetch_company_api_rejects_non_company():
    with pytest.raises(RuntimeError, match="not a company"):
        browser.fetch_company_api("https://www.linkedin.com/in/someone")


def test_profile_slug():
    assert browser._profile_slug("https://www.linkedin.com/in/joaquim-medeiros/") == "joaquim-medeiros"
    assert browser._profile_slug("https://www.linkedin.com/company/kruncher") is None


def test_fetch_profile_api(monkeypatch):
    ctx = _Ctx(feed_url="https://www.linkedin.com/in/joaquim-medeiros/")
    ctx.url = "https://www.linkedin.com/in/joaquim-medeiros/"
    monkeypatch.setattr(browser, "_ctx", ctx)
    data = browser.fetch_profile_api("https://www.linkedin.com/in/joaquim-medeiros/")
    assert data["slug"] == "joaquim-medeiros"
    assert data["found"] is True
    assert data["name"] == "Joaquim"
    assert data["experience"][0]["company"] == "X"


def test_fetch_profile_api_rejects_non_profile():
    with pytest.raises(RuntimeError, match="not a profile"):
        browser.fetch_profile_api("https://www.linkedin.com/company/kruncher")


def test_fetch_company_non_company_url_falls_back(monkeypatch):
    monkeypatch.setattr(
        browser, "_fetch_html_impl",
        lambda u: {"requested_url": u, "final_url": u, "title": "x", "html": "<html/>"},
    )
    result = browser.fetch_company("https://www.linkedin.com/in/someone")
    assert result["slug"] is None
    assert set(result["sections"]) == {"page"}
