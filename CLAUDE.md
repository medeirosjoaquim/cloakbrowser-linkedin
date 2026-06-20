# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI service that renders LinkedIn pages through a logged-in stealth browser
(`cloakbrowser`) and returns raw HTML, clean visible text, and structured
sections. Two source files (`app.py`, `browser.py`) plus an optional
`bootstrap_login.py` — see `README.md` for the full setup/usage walkthrough.

## Commands

- Setup: `uv venv venv --python 3.13` then `uv pip install --python venv/bin/python -r requirements.txt` then `venv/bin/python -c "import cloakbrowser; cloakbrowser.ensure_binary()"`. Python 3.13 is required — cloakbrowser has no 3.14 build.
- Run the app: `venv/bin/python app.py` (uvicorn on `http://127.0.0.1:5000`; logs in headless in the background on startup). Ask the user before starting it.
- Unit tests (fast, offline, no creds): `venv/bin/python -m pytest -m "not e2e"`
- E2e test (real headless login + live scrape): `venv/bin/python -m pytest -m e2e` — auto-skipped unless `LINKEDIN_EMAIL`/`LINKEDIN_PASSWORD` are set, and self-skips if a confirmation code is required.
- Single test: `venv/bin/python -m pytest tests/test_app.py::test_extract_profile_success`

## Architecture (the non-obvious parts)

- **Single login worker thread (+ opt-in scrape pool).** Playwright's sync API
  binds its objects to the thread that created them, but the server serves
  requests on arbitrary threads. So in `browser.py` all LOGIN/AUTH context
  operations (`start_login`, `submit_code`, `reset_login`, `is_logged_in`,
  `close`) run on one dedicated `ThreadPoolExecutor(max_workers=1)` driving the
  ONE persistent context that owns `./profile`. Data fetches (`fetch_html`,
  `fetch_company`, `fetch_company_api`, `fetch_profile_api`) used to share that
  single worker too; they now go through `_scrape()`, which routes to a bounded
  POOL of cookie-seeded ephemeral contexts when `SCRAPE_POOL_SIZE >= 2`, and
  otherwise fall back to the single login worker (the exact legacy path, and the
  default — so tests and the deployed box are unchanged until opted in). Each
  pool worker is its own thread owning one persistent TEMP profile and re-seeds
  its cookies whenever the session changes (`_cookie_gen`); it pins its context
  in a thread-local that `_current_ctx()` reads, so the same `_fetch_*_impl`
  runs unchanged on either thread. Overflow raises `ScrapeBusyError` → HTTP 503
  + `Retry-After` after `SCRAPE_QUEUE_TIMEOUT`s. Never call an `_impl` directly
  from outside a worker thread, and never call a public wrapper from inside an
  `_impl` (it deadlocks the worker — this is why `_fetch_company_impl` calls
  `_fetch_html_impl` directly). `login_state()`, `cookies()`, and
  `scrape_stats()` are the exceptions: pure in-memory reads, safe from any
  thread. ANTI-BOT: more concurrent navigations from one IP raises LinkedIn's
  scrutiny — start the pool at 2 and watch logs for `/checkpoint` before raising.
- **Browser lifecycle / OOM defense.** A wedged Chromium is reclaimed by
  `_reap_chromium(user_data_dir)`, which SIGKILLs every Chromium whose
  `--user-data-dir` matches the context's unique profile dir (a fresh mkdtemp
  per pool worker; `PROFILE_DIR` for login). This runs on EVERY abandon path:
  deadline breach (`run()` → `_hard_reap` before poisoning), worker close, and
  shutdown (`close_context()` → `_reap_all_chromium`, also `atexit`-registered).
  `ctx.close()` alone is NOT trusted — it can hang on a stuck browser; the
  SIGKILL is the guarantee that no orphan ~200MB process survives (the cause of
  the OOM-thrash under bursts). `SCRAPE_WORKER_MAX_REQUESTS` recycles a worker's
  Chromium after N requests to shed in-process memory growth. Killing the
  browser also unblocks the worker thread stuck inside a Playwright call.
- **Result cache + single-flight (`_cached_scrape`).** LinkedIn data is stable
  for hours, so `fetch_*` routes through a TTL cache keyed on `(endpoint, url)`
  (profiles/extract ~1h, companies ~6h; tunable via `CACHE_TTL_*`) — a repeat
  hit is ~0ms and never touches a browser. Single-flight coalesces N concurrent
  requests for the SAME url onto ONE scrape (the leader fills the cache;
  followers share its result/exc) — fewer parallel hits = better stealth AND
  throughput. Failures are never cached. Bypass per-request with `?force=1`.
  Cleared on `reset_login` (the underlying session is gone). Off via
  `CACHE_DISABLED=1`. `runtime_stats()` (surfaced in `/health`) exposes
  `cache_hits/misses/coalesced/size` + `last_scrape_ok` + `consecutive_failures`
  + pool `in_flight`/`queue_depth` so callers/LBs can self-throttle.
- **Circuit breaker (#8).** When the session dies, every fetch lands on a
  `/login`|`/authwall`|`/checkpoint` URL. The fetch impls call `_record_wall`/
  `_record_authed` after `goto`; after `CB_THRESHOLD` consecutive wall-hits the
  breaker opens → `_check_breaker()` (called in `_cached_scrape` AFTER the cache
  check) makes NEW scrapes raise `SessionDegradedError` → HTTP `409 + Retry-After`
  and a background `start_login` is kicked. Crucially **cached data still flows**
  while open (cache hits bypass the check). Auto-resets after `CB_COOLDOWN_SECONDS`
  (half-open: one probe request through).
- **Async jobs + SSE (#12, #6).** `?async=1` on `/extract`/`/profile`/`/company`
  enqueues the work via `_enqueue_job` (a bounded `JOB_WORKERS` pool + `JOB_MAX_PENDING`
  cap → 429) and returns `202 + job_id` immediately, so a long scrape (full
  company, 2-3 min) can't outlive a client timeout. `GET /jobs/{id}` polls;
  `GET /jobs/{id}/events` is an SSE stream (status heartbeats → terminal
  `result`/`error`). Jobs run the SAME `_build_*_by_url` helpers the sync path
  uses, so they reuse the cache/pool/breaker.
- **Horizontal scaling (#11).** The service is stateless per request, so it scales
  by running N replicas (each with its own LinkedIn account + profile volume)
  behind a round-robin LB; `/health` carries an `instance` id for LB/debugging.
  See `docs/SCALING.md`. (Single-process multi-identity is NOT implemented — the
  seam is `_cookies`/`_cookie_gen`; prefer N containers until you need to collapse.)
- **Login is a state machine, not a blocking call.** State lives in module
  globals guarded by `_state_lock`: `idle → logging_in → {logged_in |
  awaiting_code | no_credentials | failed}`. The app no longer raises/exits on a
  failed login. Startup kicks `start_login()` off in a *background thread* (in
  the FastAPI `lifespan`) so the server is responsive immediately — the user may
  need to `POST /login/code` before login finishes.
- **The paused confirmation-code flow.** LinkedIn often emails/texts a code even
  with 2FA disabled. When `_start_login_impl` detects a challenge page (URL has
  `/checkpoint`|`/challenge`, or a PIN input is found), it **keeps that page
  object alive** in the module global `_pending_page` and sets state
  `awaiting_code`. `_submit_code_impl` later types the code into that same page
  (both run on the worker thread, so the page stays on its bound thread) and
  verifies. The PIN input and submit button are matched against several
  candidate selectors (`PIN_INPUT_SELECTORS` / `PIN_SUBMIT_SELECTORS`) because
  LinkedIn rotates the challenge markup.
- **Cookies are in-memory.** After login succeeds, cookies are snapshotted into
  the `_cookies` global (exposed via `cookies()`). The persistent context also
  writes `./profile` on disk for restart reuse. There is no per-account/DB
  persistence — intentionally "all in memory" for now.
- **Structured extraction keys on `<h2>`, never class names.** LinkedIn's React
  profile/company pages use fully hashed classes and **no `<ul>/<li>`** for
  entries. `_EXTRACT_JS` (in `browser.py`) therefore walks `main section`
  elements, keys each by its `<h2>` title (normalized, e.g.
  `licenses_and_certifications`), and returns the section's clean `innerText` +
  company/school/profile links. It drops the top-card wrapper by skipping any
  section whose `<h2>` equals the profile/company name. `name` comes from the
  `<h1>`, falling back to the first top-card line (the owner-view profile has no
  `<h1>` in `main`). This is deliberately resilient to the dynamic DOM; it does
  **not** produce typed fields.
- **Exhaustive scroll before reading.** `_scroll_all` loops `mouse.wheel` +
  native `time.sleep` until `document.body.scrollHeight` stops growing. LinkedIn
  lazy-renders lower sections (Education, Skills, Recommendations) only when
  scrolled into view, so a fixed number of scrolls misses them on long pages.
  Uses `time.sleep`, **not** `page.wait_for_timeout` (the latter emits CDP
  signals anti-bot checks detect).
- **Text extraction reads the live DOM, not the HTML.** `_CLEAN_TEXT_JS` removes
  script/style/code/nav/footer nodes from the live page then returns
  `main.innerText`; hidden Voyager JSON blobs are excluded for free. It runs
  *after* `_EXTRACT_JS` because it mutates the DOM. The raw `html` field
  (captured by `page.content()` before any mutation) is the source of truth.
- **Company full-scrape discovers sections from the rendered nav.**
  `_fetch_company_impl` scrapes `home`, regex-finds which `COMPANY_SECTIONS`
  appear as `/company/<slug>/<seg>/` nav links, and scrapes each.
  `app.py::_strip_common_header` lifts the identical company-card/tab-bar prefix
  shared across all section texts into the top-level `header` and trims it from
  each section.

## Company data via the Voyager API

`POST /company` (and `browser.fetch_company_api`) does NOT scrape the DOM — it
calls LinkedIn's authenticated internal **Voyager API** via a same-origin
`fetch` from the logged-in page (`_COMPANY_API_JS`): the CSRF token is read from
the `JSESSIONID` cookie and sent as the `csrf-token` header with
`x-restli-protocol-version: 2.0.0`. Two endpoints:
`/voyager/api/organization/companies?q=universalName&universalName=<slug>` for
the typed company object, and `/voyager/api/organization/updatesV2?q=companyFeedByUniversalName&companyUniversalName=<slug>`
for posts. The JS maps the verbose Voyager response (with `$type`/`$recipeType`
artifacts) to a clean typed schema and builds image URLs as
`vectorImage.rootUrl + artifacts[largest].fileIdentifyingUrlPathSegment`. This is
far more reliable than the DOM for typed fields (companyId, employeeCount,
foundedOn, follower count, …). `app.py::_build_company_envelope` wraps it in the
Kruncher-style envelope (`analysisId`, `array_content.content[]` with
`LINKEDIN_COMPANY_PRO` + `LINKEDIN_POST`, `responseCode`, `pdf_path` reserved for
later). The impl navigates to the company page first only to be on the
linkedin.com origin so the fetch carries cookies.

## Profile data via SDUI (not Voyager)

`POST /profile` (and `browser.fetch_profile_api`) can't use Voyager like
`/company` does — LinkedIn **deprecated the profile REST/GraphQL APIs** (they
return only URN references; verified via DevTools). The live profile page is
**Server-Driven UI**, so `_PROFILE_API_JS` POSTs to the SDUI component endpoints
(`/flagship-web/rsc-action/actions/component?componentId=…profileCardsAboveActivity`
and `…profileCardsBelowActivityPart1..6`) with a body carrying the **vanity slug**
(`{"clientArguments":{"payload":{"isSelfView":false,"vanityName":"<slug>"}…}}`).
These use a **stable componentId** (no rotating queryId) + the session cookie +
`csrf-token`. The response is React-Server-Component "flight" text; the visible
values live in `children:["…"]` string nodes, which we reconstruct in document
order and parse into sections. Experience uses one **date-anchored** parser
(`_PROFILE_API_JS`) that handles every layout: each position is anchored on its
date-range line; lead-in lines before it give title/company; a grouped/promotion
block sets a `blockCompany` (header line followed by a duration like "9 mos")
inherited by following single-line roles; a 2-line lead-in is a new single
position UNLESS its first line is a strong location keyword (Remote/Hybrid/Area/
Greater/Region) — comma-locations are disambiguated by line count, not by the
comma (so titles like "Founding LP, Investment Committee" aren't mistaken for
locations); a location attaches to the position it follows, guarded to reject
description leakage (len ≤ 60, no trailing period). Validated live against
grouped, single, mixed, comma-title and comma-location profiles. If a profile
ever parses oddly, re-test against it via DevTools and extend the heuristics —
do NOT regress the validated set.

Robustness guarantees that make the typed parse safe to be best-effort:
- **`raw_sections`** in the response holds the complete ordered lines of EVERY
  section. The typed fields can never *lose* data — a consumer (or LLM pass) can
  always recover the full content from raw, even on a never-seen layout.
- **Section slicing is bounded to the SDUI part its header lives in** — a
  following part's content can render before its own header, so an unbounded
  slice bleeds (e.g. recommendation testimonials into Education). The part bound
  prevents that.
- **Education skips streamed description paragraphs and "+N more" markers** (a
  school name is short and never ends in sentence punctuation; longer/sentence
  lines are descriptions the SDUI appends at the end of the part). Date detection
  covers year-spans, month-year spans, and single years. `about` and the top card (name/headline/location) come
from the **page DOM** (the long About summary isn't in the SDUI children nodes);
`skills` falls back to the "Top skills" line when the full Skills section isn't
in the fetched parts. Fragility note: the SDUI node schema can shift over time
(less stable than `/company`'s clean JSON, but far more stable than rotating
queryIds). **TOC-aware slicing**: LinkedIn renders a "table of contents" nav
(several section headers listed CONSECUTIVELY) then dumps all their entries in
order WITHOUT re-stating each header; `sliceSection` detects that run (the
`ranges` IIFE) and partitions the post-TOC content by section-type boundary
signals — Education ⇒ first year-only date span (Experience dates are
month-year), Licenses ⇒ a line followed by `Issued …`, Projects ⇒ followed by
`Associated with …`. In-content headers keep the old part-bounded next-header
logic, so profiles with no TOC are unchanged. Known limitation: when LinkedIn
INTERLEAVES Licenses and Projects in the feed, a contiguous partition can't
fully separate them — `raw_sections` preserves the truth; typed `licenses[]`
may include adjacent project noise. `app.py::_build_profile_envelope` wraps it
as `LINKEDIN_PROFILE_PRO`.

## Endpoints

- `POST /extract` `{url, full?}` → structured JSON (DOM sections). Returns `409`
  unless login state is `logged_in`. `full` only matters for `/company/` URLs.
  Query params: `?force=1` (bypass cache), `?async=1` (→ `202 + job_id`).
- `POST /company` `{url}` → typed Voyager company data + posts envelope (accepts
  a company URL or a bare slug). `409` unless `logged_in`. Same query params.
- `POST /profile` `{url}` → typed profile envelope (`LINKEDIN_PROFILE_PRO`):
  name/headline/location/about + `experience[]`/`education[]`/`licenses[]`/`skills[]`
  + extra sections. Accepts a `/in/` URL or a bare vanity slug. `409` unless `logged_in`. Same query params.
- `POST /login/code` `{code}` → submit the confirmation code; `409` if rejected.
- `GET /jobs/{id}` → poll an async job (`pending`/`done`/`error`); `404` if
  unknown/expired. `GET /jobs/{id}/events` → SSE stream (status heartbeats, then
  a terminal `result`/`error` event) so a client waits on one connection.
- `GET /login/status` → the login state dict.
- `GET /health` → `{status, instance, ...login, ...scrape_stats (pool in_flight/
  queue_depth), ...runtime_stats (cache hits/misses/coalesced/size, last_scrape_ok,
  consecutive_failures), ...breaker_state (open, wall_hits, threshold)}`.
- `GET /` → a small single-file web UI (the `INDEX_HTML` string in `app.py`).

## Docker

`Dockerfile` (+ `docker-compose.yml`) containerizes the app on `python:3.13-slim`.
Non-obvious bits:
- Chromium OS libraries come from `python -m playwright install-deps chromium`
  (cloakbrowser pulls in Playwright), not a hand-maintained apt list.
- The stealth Chromium is baked into the image at build time
  (`cloakbrowser.ensure_binary()`), pinned under `/opt/cloakbrowser`
  (`CLOAKBROWSER_CACHE_DIR`) with `CLOAKBROWSER_AUTO_UPDATE=false` so it never
  downloads at runtime.
- `CHROMIUM_NO_SANDBOX=true` makes `browser._launch_args()` pass
  `--no-sandbox --disable-dev-shm-usage` (Chromium can't sandbox in a container).
  These are launch flags, invisible to pages, so stealth is unaffected. Locally
  the env var is unset, so nothing changes.
- The `CMD` runs uvicorn bound to `0.0.0.0` (the `app.py __main__` block binds
  `127.0.0.1`, which is unreachable from the host). Login still auto-starts via
  the lifespan. The session persists in a volume at `/app/profile`.

## Conventions

- The web UI is one big `INDEX_HTML` string literal inside `app.py` (no separate
  template/static files). `test_app.py::test_index_js_parses` runs `node
  --check` on the embedded `<script>` to guard against the trap where a raw
  newline inside a Python-string-embedded JS literal silently breaks the UI — if
  you edit the inline JS, keep escapes (`\\n`) intact.
- Unit tests fake `cloakbrowser` entirely (`test_browser.py`) and stub
  `browser.*` + `browser.login_state` (`test_app.py`), so they need no network or
  credentials. The FastAPI `TestClient` is created **without** the `with`
  context manager so the `lifespan` (background login) does not fire; `start_login`
  is also stubbed in the fixture. Keep real-login coverage in the `e2e` test.
