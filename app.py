"""FastAPI wrapper: POST a LinkedIn URL, get back rendered HTML + clean text +
structured sections.

Login is handled as a state machine. On startup the app attempts a headless
login in the background; if LinkedIn asks for an email/SMS confirmation code
(it does this even with 2FA disabled), the state becomes `awaiting_code` and the
code is submitted via POST /login/code. Cookies for the live session are held in
memory.

Run with:  venv/bin/python app.py      (or: uvicorn app:app)
"""

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

from fastapi.responses import JSONResponse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

import browser

LOG_FILE = str(Path(__file__).resolve().parent / "scraper.log")


def _setup_logging() -> None:
    """Log to stdout + a rotating scraper.log. Idempotent across reloads."""
    root = logging.getLogger("scraper")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(stream)
    root.addHandler(file_handler)


_setup_logging()
log = logging.getLogger("scraper.app")

# Per-instance identity for /health (helps an LB see WHICH replica served a
# request, and is the building block for horizontal scaling — N containers each
# with their own LinkedIn account + profile volume behind a round-robin LB).
INSTANCE_ID = os.environ.get("INSTANCE_ID") or uuid.uuid4().hex[:12]


# --- async job model (#12) --------------------------------------------------
# Heavy scrapes (esp. full-company /extract, 2-3 min) shouldn't hold an HTTP
# connection open past a client's timeout. `?async=1` enqueues the work and
# returns 202 + a job_id immediately; clients poll GET /jobs/{job_id}. Jobs run
# on a BOUNDED worker pool and reuse the cache/pool/breaker, so they benefit
# from everything else here. Results expire after _JOB_TTL (memory-bounded).
_JOB_WORKERS = max(1, int(os.environ.get("JOB_WORKERS", "2") or "2"))
_JOB_MAX = int(os.environ.get("JOB_MAX_PENDING", "128") or "128")  # cap memory
_JOB_TTL = float(os.environ.get("JOB_TTL_SECONDS", "3600") or "3600")
_jobs: dict = {}           # job_id -> {status, kind, url, full, result, error, created, done_at}
_jobs_lock = threading.Lock()
_job_exec = ThreadPoolExecutor(max_workers=_JOB_WORKERS, thread_name_prefix="scrape-job")


def _job_cleanup_locked() -> None:
    """Drop expired jobs (under _jobs_lock). Keeps the store bounded."""
    now = time.time()
    expired = [jid for jid, j in _jobs.items()
               if j["status"] == "done" and now - j.get("done_at", now) > _JOB_TTL]
    for jid in expired:
        _jobs.pop(jid, None)


def _enqueue_job(kind: str, url: str, full: bool, runner) -> str:
    """Register + start a background job. `runner` is a zero-arg callable that
    does the scrape (closes over the validated URL). Returns the job_id. Raises
    RuntimeError if the pending cap is exceeded (→ HTTP 429)."""
    with _jobs_lock:
        _job_cleanup_locked()
        if len(_jobs) >= _JOB_MAX:
            raise RuntimeError("too many pending jobs")
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {"status": "pending", "kind": kind, "url": url, "full": full,
                         "result": None, "error": None, "created": time.time(), "done_at": None}

    def _work():
        try:
            result = runner()
            with _jobs_lock:
                j = _jobs.get(job_id)
                if j:
                    j["result"] = result
                    j["status"] = "done"
                    j["done_at"] = time.time()
        except Exception as exc:  # noqa: BLE001 - capture any failure for the poller
            with _jobs_lock:
                j = _jobs.get(job_id)
                if j:
                    j["error"] = str(exc)
                    j["status"] = "error"
                    j["done_at"] = time.time()

    _job_exec.submit(_work)
    return job_id


def _job_view(job_id: str):
    """Public-safe job snapshot, or None if unknown/expired."""
    with _jobs_lock:
        j = _jobs.get(job_id)
        if j is None:
            return None
        view = {"job_id": job_id, "status": j["status"], "kind": j["kind"],
                "url": j["url"], "created_at": j["created"]}
        if j["status"] == "done":
            view["result"] = j["result"]
        elif j["status"] == "error":
            view["error"] = j["error"]
        return view


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Kick off login in a background thread so the server is immediately
    # responsive — the user may need to POST /login/code before login finishes.
    threading.Thread(
        target=browser.start_login, name="login-bootstrap", daemon=True
    ).start()
    yield
    # Close the browser context on shutdown so Chromium flushes its cookies to
    # the profile — lets the next start reuse the session instead of re-logging
    # in (and re-triggering a confirmation code) on every restart.
    browser.close_context()


app = FastAPI(title="LinkedIn scraper", lifespan=lifespan)

# Auth: when API_AUTH_SHA256 is set, EVERY request must present the secret whose
# SHA-256 equals it, via `Authorization: Bearer <secret>` or `X-API-Key: <secret>`.
# Only the hash is stored (never the plaintext); the comparison is constant-time.
# Unset (e.g. local dev / tests) => no auth.
API_AUTH_SHA256 = os.environ.get("API_AUTH_SHA256", "").strip().lower()


def _presented_secret(request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key") or None


@app.middleware("http")
async def require_auth(request, call_next):
    if API_AUTH_SHA256:
        secret = _presented_secret(request)
        ok = secret is not None and hmac.compare_digest(
            hashlib.sha256(secret.encode()).hexdigest(), API_AUTH_SHA256
        )
        if not ok:
            return JSONResponse(
                {"detail": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)


class CodeIn(BaseModel):
    code: str


class ExtractIn(BaseModel):
    url: str | None = None
    full: bool = False


class CompanyIn(BaseModel):
    url: str | None = None


class ProfileIn(BaseModel):
    url: str | None = None


def _is_linkedin(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _strip_common_header(sections: dict) -> str:
    """LinkedIn renders the same company card + tab nav atop every section page,
    so each section's text starts with an identical block. Lift that shared
    prefix out once (returned as the company `header`) and trim it from each
    section so the section text holds only its unique content.

    Returns "" and leaves sections untouched if there's no meaningful shared
    prefix (e.g. a single section, or unrelated pages).
    """
    texts = [s.get("text") or "" for s in sections.values()]
    if len(texts) < 2:
        return ""
    prefix = texts[0]
    for t in texts[1:]:
        n = 0
        limit = min(len(prefix), len(t))
        while n < limit and prefix[n] == t[n]:
            n += 1
        prefix = prefix[:n]
        if not prefix:
            return ""
    # Cut at the last line boundary so we never split a line mid-way.
    cut = prefix.rfind("\n")
    if cut <= 0:
        return ""
    for s in sections.values():
        s["text"] = (s.get("text") or "")[cut:].lstrip("\n")
        s["text_length"] = len(s["text"])
    return prefix[:cut].strip()


def _page_payload(raw: dict) -> dict:
    """API payload for one rendered page: raw HTML, clean text, and the
    structured sections keyed by their LinkedIn <h2> title.
    """
    html = raw["html"]
    text = raw.get("text") or ""
    return {
        "requested_url": raw.get("requested_url"),
        "final_url": raw["final_url"],
        "title": raw["title"],
        "name": raw.get("name"),
        "headline": raw.get("headline"),
        "location": raw.get("location"),
        "top_card_lines": raw.get("top_card_lines") or [],
        "sections": raw.get("sections") or {},
        "html": html,
        "html_length": len(html),
        "text": text,
        "text_length": len(text),
    }


_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _company_url(value: str) -> str | None:
    """Accept a full LinkedIn company URL or a bare slug; return a company URL."""
    value = (value or "").strip()
    if not value:
        return None
    if "linkedin.com" in value:
        return value if _is_linkedin(value) and "/company/" in value else None
    if _SLUG_RE.match(value):  # bare slug, e.g. "freda-ab"
        return f"https://www.linkedin.com/company/{value}/"
    return None


def _profile_url(value: str) -> str | None:
    """Accept a full LinkedIn profile URL or a bare vanity slug; return a /in/ URL."""
    value = (value or "").strip()
    if not value:
        return None
    if "linkedin.com" in value:
        return value if _is_linkedin(value) and "/in/" in value else None
    if _SLUG_RE.match(value):  # bare vanity slug, e.g. "joaquim-medeiros"
        return f"https://www.linkedin.com/in/{value}/"
    return None


def _build_profile_envelope(slug: str, data: dict) -> dict:
    """Assemble the Kruncher-style envelope from the SDUI profile data."""
    found = data.get("found") and (data.get("name") or data.get("experience"))
    base = f"https://www.linkedin.com/in/{slug}"
    now = str(datetime.now(timezone.utc))
    content = []
    if found:
        profile = {
            "name": data.get("name"),
            "headline": data.get("headline"),
            "location": data.get("location"),
            "about": data.get("about"),
            "experience": data.get("experience") or [],
            "education": data.get("education") or [],
            "licenses": data.get("licenses") or [],
            "skills": data.get("skills") or [],
            "sections": data.get("sections") or {},
            "raw_sections": data.get("raw_sections") or {},
            "vanityName": slug,
            "url": base + "/",
        }
        content.append({
            "url": base,
            "type": "LINKEDIN_PROFILE_PRO",
            "date_retrieve": now,
            "text_dump": json.dumps(profile, indent=2, ensure_ascii=False),
            "html_dump": profile,
            "starting_page": 1,
            "page_number": 1,
            "meta": {
                "readability_title": "LinkedIn Profile",
                "hero_section": data.get("headline") or "",
            },
        })
    return {
        "analysisId": str(uuid.uuid4()),
        "profile_url": base,
        "array_content": {"content": content, "pdf_path": None},
        "responseCode": "1000" if found else "1001",
        "error": "" if found else (data.get("error") or "profile not found or empty"),
    }


def _build_company_envelope(slug: str, data: dict) -> dict:
    """Assemble the Kruncher-style envelope from the Voyager company + posts data."""
    company = data.get("company")
    base = f"https://www.linkedin.com/company/{slug}"
    now = str(datetime.now(timezone.utc))
    content = []
    if company:
        content.append({
            "url": base,
            "type": "LINKEDIN_COMPANY_PRO",
            "date_retrieve": now,
            "text_dump": json.dumps(company, indent=2, ensure_ascii=False),
            "html_dump": company,
            "starting_page": 1,
            "page_number": 2,
            "meta": {
                "readability_title": "LinkedIn Company Profile",
                "hero_section": company.get("tagline") or "",
            },
        })
    posts = data.get("posts") or []
    content.append({
        "url": base,
        "type": "LINKEDIN_POST",
        "date_retrieve": now,
        "text_dump": json.dumps(posts, ensure_ascii=False),
        "starting_page": 2,
        "page_number": 1,
    })
    website = re.sub(r"^www\.", "", (company or {}).get("websiteUrl") or "")
    return {
        "analysisId": str(uuid.uuid4()),
        "website": website,
        "array_content": {"content": content, "pdf_path": None},
        "responseCode": "1000" if company else "1001",
        "error": "" if company else (data.get("error") or "company not found"),
    }


def _page_kind(url: str, final_url: str) -> str:
    blob = f"{url} {final_url}"
    if "/company/" in blob:
        return "company"
    if "/in/" in blob:
        return "profile"
    return "page"


# --- async-job runners: fetch + build response, shared shape with sync path ----
# These run on the job worker pool. They let exceptions propagate (busy/deadline/
# degraded/generic); the job layer captures them into the job's error field.

def _build_company_envelope_by_url(company_url: str, force: bool) -> dict:
    data = browser.fetch_company_api(company_url, force=force)
    return _build_company_envelope(data.get("slug") or "", data)


def _build_profile_envelope_by_url(profile_url: str, force: bool) -> dict:
    data = browser.fetch_profile_api(profile_url, force=force)
    return _build_profile_envelope(_profile_slug(profile_url) or "", data)


def _build_extract_result_by_url(url: str, full: bool, force: bool) -> dict:
    if full and "/company/" in url:
        company = browser.fetch_company(url, force=force)
        sections = {name: _page_payload(raw) for name, raw in company["sections"].items()}
        header = _strip_common_header(sections)
        return {
            "url": url, "type": "company", "base_url": company["base_url"],
            "slug": company["slug"], "header": header, "sections": sections,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    result = browser.fetch_html(url, force=force)
    payload = _page_payload(result)
    return {
        "url": url, "type": _page_kind(url, payload["final_url"]),
        "fetched_at": datetime.now(timezone.utc).isoformat(), **payload,
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


@app.get("/health")
def health():
    return {
        "status": "ok",
        "instance": INSTANCE_ID,
        **browser.login_state(),
        **browser.scrape_stats(),
        **browser.runtime_stats(),
        **browser.breaker_state(),
    }


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    """Poll an async scrape job (from ?async=1). Returns 200 with {status, ...}:
    status is 'pending' (still scraping) or 'done' (result populated) or 'error'.
    404 if the job is unknown or expired."""
    view = _job_view(job_id)
    if view is None:
        raise HTTPException(status_code=404, detail="unknown or expired job_id")
    return view


@app.get("/jobs/{job_id}/events")
async def stream_job(job_id: str):
    """Server-Sent Events stream for a job (#6). Emits `status` heartbeats while
    pending, then a terminal `result` (or `error`) event and closes — so a client
    can wait on ONE connection for a long scrape instead of polling. Use after
    POSTing with ?async=1 to get the job_id."""
    if _job_view(job_id) is None:
        raise HTTPException(status_code=404, detail="unknown or expired job_id")

    async def gen():
        import asyncio
        while True:
            view = _job_view(job_id)
            if view is None:
                yield b"event: error\ndata: job expired\n\n"
                return
            if view["status"] == "pending":
                yield b"event: status\ndata: pending\n\n"
                await asyncio.sleep(1.0)
                continue
            # terminal state — emit the full payload, then close
            yield f"event: {view['status']}\ndata: {json.dumps(view)}\n\n\n".encode()
            return

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/company")
def company(body: CompanyIn, force: bool = False, async_: bool = Query(False, alias="async")):
    req_id = uuid.uuid4().hex[:8]
    company_url = _company_url(body.url or "")
    if not company_url:
        raise HTTPException(
            status_code=400,
            detail="url must be a linkedin.com/company/... URL or a company slug",
        )
    state = browser.login_state()
    if state["state"] != "logged_in":
        raise HTTPException(
            status_code=409,
            detail=(
                f"not logged in (state={state['state']}). "
                "Check GET /login/status; POST /login/code if a code is required."
            ),
        )

    if async_:
        try:
            job_id = _enqueue_job("company", company_url, False,
                                  lambda: _build_company_envelope_by_url(company_url, force))
        except RuntimeError:
            raise HTTPException(status_code=429, detail="too many pending jobs; retry shortly")
        log.info("[%s] company async job=%s url=%s", req_id, job_id, company_url)
        return JSONResponse({"job_id": job_id, "status": "pending", "url": company_url},
                            status_code=202, headers={"Location": f"/jobs/{job_id}"})

    log.info("[%s] company url=%s", req_id, company_url)
    try:
        data = browser.fetch_company_api(company_url, force=force)
    except browser.ScrapeBusyError:
        raise HTTPException(
            status_code=503,
            detail="server busy — all scrape workers occupied; retry shortly",
            headers={"Retry-After": "5"},
        )
    except browser.ScrapeDeadlineError:
        raise HTTPException(status_code=504, detail="scrape timed out; retry shortly")
    except browser.SessionDegradedError:
        raise HTTPException(
            status_code=409,
            detail="session degraded — re-login in flight; retry in a few seconds",
            headers={"Retry-After": "5"},
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("[%s] company fetch failed url=%s", req_id, company_url)
        raise HTTPException(status_code=502, detail=f"company fetch failed: {exc}")

    slug = data.get("slug") or ""
    log.info("[%s] company done slug=%s found=%s posts=%d", req_id, slug, data.get("found"), len(data.get("posts") or []))
    return _build_company_envelope(slug, data)


@app.post("/profile")
def profile(body: ProfileIn, force: bool = False, async_: bool = Query(False, alias="async")):
    req_id = uuid.uuid4().hex[:8]
    profile_url = _profile_url(body.url or "")
    if not profile_url:
        raise HTTPException(
            status_code=400,
            detail="url must be a linkedin.com/in/... URL or a profile vanity slug",
        )
    state = browser.login_state()
    if state["state"] != "logged_in":
        raise HTTPException(
            status_code=409,
            detail=(
                f"not logged in (state={state['state']}). "
                "Check GET /login/status; POST /login/code if a code is required."
            ),
        )

    if async_:
        try:
            job_id = _enqueue_job("profile", profile_url, False,
                                  lambda: _build_profile_envelope_by_url(profile_url, force))
        except RuntimeError:
            raise HTTPException(status_code=429, detail="too many pending jobs; retry shortly")
        log.info("[%s] profile async job=%s url=%s", req_id, job_id, profile_url)
        return JSONResponse({"job_id": job_id, "status": "pending", "url": profile_url},
                            status_code=202, headers={"Location": f"/jobs/{job_id}"})

    log.info("[%s] profile url=%s", req_id, profile_url)
    try:
        data = browser.fetch_profile_api(profile_url, force=force)
    except browser.ScrapeBusyError:
        raise HTTPException(
            status_code=503,
            detail="server busy — all scrape workers occupied; retry shortly",
            headers={"Retry-After": "5"},
        )
    except browser.ScrapeDeadlineError:
        raise HTTPException(status_code=504, detail="scrape timed out; retry shortly")
    except browser.SessionDegradedError:
        raise HTTPException(
            status_code=409,
            detail="session degraded — re-login in flight; retry in a few seconds",
            headers={"Retry-After": "5"},
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("[%s] profile fetch failed url=%s", req_id, profile_url)
        raise HTTPException(status_code=502, detail=f"profile fetch failed: {exc}")

    slug = data.get("slug") or ""
    log.info("[%s] profile done slug=%s found=%s exp=%d", req_id, slug, data.get("found"), len(data.get("experience") or []))
    return _build_profile_envelope(slug, data)


@app.get("/login/status")
def login_status():
    return browser.login_state()


@app.post("/login/reset")
def login_reset():
    """Wipe the session (context + ./profile + in-memory cookies) and kick off a
    fresh headless login in the background. Use this to test the cold-start /
    confirmation-code path. Poll GET /login/status afterwards."""
    browser.reset_login()
    threading.Thread(
        target=browser.start_login, name="login-bootstrap", daemon=True
    ).start()
    log.info("session reset; fresh login started")
    return {
        "reset": True,
        "message": "fresh login started — poll GET /login/status",
        **browser.login_state(),
    }


@app.post("/login/code")
def login_code(body: CodeIn):
    code = (body.code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="missing 'code' in JSON body")
    result = browser.submit_code(code)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "code not accepted"))
    return result


@app.post("/extract")
def extract(body: ExtractIn, force: bool = False, async_: bool = Query(False, alias="async")):
    req_id = uuid.uuid4().hex[:8]
    url = body.url
    if not url:
        raise HTTPException(status_code=400, detail="missing 'url' in JSON body")
    if not _is_linkedin(url):
        log.warning("[%s] rejected: non-linkedin url=%s", req_id, url)
        raise HTTPException(status_code=400, detail="url must be a linkedin.com address")

    state = browser.login_state()
    if state["state"] != "logged_in":
        raise HTTPException(
            status_code=409,
            detail=(
                f"not logged in (state={state['state']}). "
                "Check GET /login/status; POST /login/code if a code is required."
            ),
        )

    full = bool(body.full)
    if async_:
        try:
            job_id = _enqueue_job("extract", url, full,
                                  lambda: _build_extract_result_by_url(url, full, force))
        except RuntimeError:
            raise HTTPException(status_code=429, detail="too many pending jobs; retry shortly")
        log.info("[%s] extract async job=%s url=%s full=%s", req_id, job_id, url, full)
        return JSONResponse({"job_id": job_id, "status": "pending", "url": url, "full": full},
                            status_code=202, headers={"Location": f"/jobs/{job_id}"})

    log.info("[%s] extract url=%s full=%s", req_id, url, full)
    try:
        if full and "/company/" in url:
            company = browser.fetch_company(url, force=force)
        else:
            result = browser.fetch_html(url, force=force)
    except browser.ScrapeBusyError:
        raise HTTPException(
            status_code=503,
            detail="server busy — all scrape workers occupied; retry shortly",
            headers={"Retry-After": "5"},
        )
    except browser.ScrapeDeadlineError:
        raise HTTPException(status_code=504, detail="scrape timed out; retry shortly")
    except browser.SessionDegradedError:
        raise HTTPException(
            status_code=409,
            detail="session degraded — re-login in flight; retry in a few seconds",
            headers={"Retry-After": "5"},
        )
    except Exception as exc:  # noqa: BLE001 - surface any render failure to caller
        log.exception("[%s] render failed url=%s", req_id, url)
        raise HTTPException(status_code=502, detail=f"render failed: {exc}")

    if full and "/company/" in url:
        sections = {name: _page_payload(raw) for name, raw in company["sections"].items()}
        header = _strip_common_header(sections)
        log.info("[%s] done full base=%s sections=%s", req_id, company["base_url"], ",".join(sections))
        return {
            "url": url,
            "type": "company",
            "base_url": company["base_url"],
            "slug": company["slug"],
            "header": header,
            "sections": sections,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    payload = _page_payload(result)
    log.info(
        "[%s] done final_url=%s title=%r html_chars=%d text_chars=%d sections=%s",
        req_id,
        payload["final_url"],
        payload["title"],
        payload["html_length"],
        payload["text_length"],
        ",".join(payload["sections"]),
    )
    return {
        "url": url,
        "type": _page_kind(url, payload["final_url"]),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LinkedIn scraper</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 system-ui, sans-serif; }
  header { padding: 14px 20px; border-bottom: 1px solid #8883; display: flex; justify-content: space-between; align-items: center; }
  header h1 { margin: 0; font-size: 16px; }
  #status { font-size: 12px; padding: 4px 10px; border-radius: 999px; background: #8882; }
  #status.ok { background: #1e7e3433; color: #1e7e34; }
  #status.warn { background: #c0840033; color: #c08400; }
  #status.bad { background: #c0392b33; color: #c0392b; }
  .cols { display: flex; gap: 0; height: calc(100vh - 52px); }
  .left { width: 360px; min-width: 280px; padding: 20px; border-right: 1px solid #8883; overflow: auto; }
  .right { flex: 1; padding: 20px; overflow: auto; }
  label { display: block; font-weight: 600; margin-bottom: 6px; }
  input[type=url], input[type=text] { width: 100%; padding: 9px 10px; border: 1px solid #8886; border-radius: 6px; font: inherit; background: transparent; color: inherit; }
  button { margin-top: 12px; padding: 9px 16px; border: 0; border-radius: 6px; background: #0a66c2; color: #fff; font: inherit; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: .6; cursor: default; }
  .meta { margin-top: 14px; font-size: 12px; opacity: .7; word-break: break-all; }
  .inline { display: flex; align-items: center; gap: 8px; font-weight: 400; margin-top: 12px; }
  .inline input { margin: 0; width: auto; }
  #codeBox { display: none; margin-top: 18px; padding: 14px; border: 1px solid #c0840066; border-radius: 8px; background: #c0840011; }
  #sectionWrap { display: none; margin-top: 14px; }
  select { width: 100%; padding: 8px 10px; border: 1px solid #8886; border-radius: 6px; background: transparent; color: inherit; font: inherit; }
  .tabs { display: flex; gap: 8px; margin-bottom: 12px; }
  .tabs button { margin: 0; background: #8882; color: inherit; }
  .tabs button.active { background: #0a66c2; color: #fff; }
  pre { white-space: pre-wrap; word-wrap: break-word; margin: 0; padding: 12px; background: #8881; border-radius: 6px; font: 12px/1.5 ui-monospace, monospace; }
  .err { color: #c0392b; font-weight: 600; }
  .placeholder { opacity: .5; }
</style>
</head>
<body>
<header>
  <h1>LinkedIn scraper</h1>
  <span id="status">checking…</span>
</header>
<div class="cols">
  <div class="left">
    <div id="codeBox">
      <label for="code">Confirmation code</label>
      <input id="code" type="text" inputmode="numeric" autocomplete="one-time-code" placeholder="LinkedIn sent a code">
      <button id="sendCode">Submit code</button>
      <div class="meta" id="codeMsg"></div>
    </div>
    <label for="url" style="margin-top:18px">LinkedIn URL</label>
    <input id="url" type="url" placeholder="https://www.linkedin.com/...">
    <label class="inline"><input type="checkbox" id="full"> Full scrape (all company sections)</label>
    <button id="go">Scrape</button>
    <div id="sectionWrap">
      <label for="section">Section</label>
      <select id="section"></select>
    </div>
    <div class="meta" id="meta"></div>
  </div>
  <div class="right">
    <div class="tabs">
      <button data-tab="text" class="active">Text</button>
      <button data-tab="json">JSON</button>
      <button data-tab="html">HTML</button>
    </div>
    <pre id="out" class="placeholder">Enter a LinkedIn URL and press Scrape.</pre>
  </div>
</div>
<script>
  let last = null, tab = "text", section = null;
  const $ = id => document.getElementById(id);
  const out = $("out"), meta = $("meta"), go = $("go"), url = $("url");
  const full = $("full"), sectionSel = $("section"), sectionWrap = $("sectionWrap");
  const statusEl = $("status"), codeBox = $("codeBox"), codeMsg = $("codeMsg");

  async function refreshStatus() {
    try {
      const r = await fetch("/login/status");
      const s = await r.json();
      statusEl.textContent = s.state;
      statusEl.className = s.state === "logged_in" ? "ok"
        : s.state === "awaiting_code" ? "warn"
        : (s.state === "failed" || s.state === "no_credentials") ? "bad" : "";
      codeBox.style.display = s.state === "awaiting_code" ? "block" : "none";
      go.disabled = s.state !== "logged_in";
    } catch (e) {
      statusEl.textContent = "server down";
      statusEl.className = "bad";
    }
  }
  refreshStatus();
  setInterval(refreshStatus, 4000);

  async function sendCode() {
    const code = $("code").value.trim();
    if (!code) return;
    $("sendCode").disabled = true; codeMsg.textContent = "submitting…";
    try {
      const r = await fetch("/login/code", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ code }),
      });
      const d = await r.json();
      codeMsg.textContent = r.ok ? "logged in" : ("error: " + (d.detail || r.status));
    } catch (e) { codeMsg.textContent = String(e); }
    finally { $("sendCode").disabled = false; refreshStatus(); }
  }
  $("sendCode").onclick = sendCode;

  function sectionData() {
    if (!last || last.error) return null;
    if (last.sections && section && section !== ALL && last.type === "company") return last.sections[section];
    return last;
  }
  const ALL = "__all__";

  function renderMeta() {
    if (!last || last.error) { meta.innerHTML = ""; return; }
    let s = "";
    s += "<div>type: " + (last.type || "?") + "</div>";
    if (last.name) s += "<div>name: " + last.name + "</div>";
    if (last.headline) s += "<div>headline: " + last.headline + "</div>";
    if (last.location) s += "<div>location: " + last.location + "</div>";
    if (last.type === "company" && last.sections) {
      s += "<div>company sections: " + Object.keys(last.sections).join(", ") + "</div>";
    } else if (last.sections) {
      s += "<div>sections: " + Object.keys(last.sections).join(", ") + "</div>";
    }
    s += "<div>fetched " + last.fetched_at + "</div>";
    meta.innerHTML = s;
  }

  function renderAll() {
    const entries = Object.entries(last.sections);
    if (tab === "json") return JSON.stringify(last, null, 2);
    const key = tab === "text" ? "text" : "html";
    const body = entries.map(([n, s]) =>
      "===== " + n + "  (" + s.final_url + ") =====\\n" + (s[key] || "(empty)")
    ).join("\\n\\n\\n");
    if (tab === "text" && last.header) {
      return "===== company header (shared) =====\\n" + last.header + "\\n\\n\\n" + body;
    }
    return body;
  }

  function renderSections() {
    if (tab === "json") return JSON.stringify(last, null, 2);
    if (tab === "html") return last.html;
    const order = Object.keys(last.sections || {});
    let body = "";
    if (last.headline) body += last.headline + "\\n";
    if (last.location) body += last.location + "\\n";
    body += "\\n";
    body += order.map(k => {
      const sec = last.sections[k];
      return "===== " + (sec.title || k) + " =====\\n" + (sec.text || "(empty)");
    }).join("\\n\\n\\n");
    return body || (last.text || "(no text)");
  }

  function render() {
    out.classList.remove("placeholder", "err");
    if (!last) return;
    if (last.error) { out.classList.add("err"); out.textContent = last.error; return; }
    if (last.type === "company" && last.sections && section === ALL) { out.textContent = renderAll(); return; }
    if (last.type === "company" && last.sections) {
      const d = last.sections[section];
      if (!d) { out.textContent = "(no data)"; return; }
      out.textContent = tab === "json" ? JSON.stringify(d, null, 2) : (tab === "text" ? (d.text || "(no text)") : d.html);
      return;
    }
    out.textContent = renderSections();
  }

  function buildSections() {
    sectionSel.innerHTML = "";
    if (last && last.type === "company" && last.sections) {
      const all = document.createElement("option");
      all.value = ALL; all.textContent = "★ all sections";
      sectionSel.appendChild(all);
      Object.keys(last.sections).forEach(n => { const o = document.createElement("option"); o.value = n; o.textContent = n; sectionSel.appendChild(o); });
      section = ALL; sectionSel.value = section; sectionWrap.style.display = "block";
    } else {
      section = null; sectionWrap.style.display = "none";
    }
  }

  sectionSel.onchange = () => { section = sectionSel.value; renderMeta(); render(); };
  document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => {
    tab = b.dataset.tab;
    document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("active", x === b));
    render();
  });

  async function scrape() {
    const u = url.value.trim();
    if (!u) return;
    go.disabled = true; meta.innerHTML = ""; last = null;
    out.classList.remove("err"); out.classList.add("placeholder");
    out.textContent = full.checked ? "Scraping all sections (this takes a while)..." : "Scraping...";
    try {
      const r = await fetch("/extract", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ url: u, full: full.checked }),
      });
      last = await r.json();
      if (!r.ok) { last = { error: last.detail || ("HTTP " + r.status) }; }
    } catch (e) { last = { error: String(e) }; }
    finally { go.disabled = false; buildSections(); renderMeta(); render(); }
  }
  go.onclick = scrape;
  url.addEventListener("keydown", e => { if (e.key === "Enter") scrape(); });
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=5000)
