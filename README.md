# LinkedIn scraper

Stealth-browser LinkedIn fetcher behind a FastAPI service.

## What it does

- A logged-in [cloakbrowser](https://github.com/CloakHQ/cloakbrowser) session renders a LinkedIn profile/company/page.
- The API returns, for each page: the raw rendered HTML, a clean plain-text view (the page's visible `innerText`), and **structured sections** keyed by their LinkedIn `<h2>` title (About, Experience, Education, …).
- `innerText` reads only visible nodes, so LinkedIn's embedded Voyager API JSON (hidden `<code id="bpr-guid-…">` blobs) is excluded. The raw HTML is still the source of truth; the text/sections are the readable convenience views.
- Login is a small state machine: on startup it tries to reuse/establish a session. **LinkedIn often emails or texts a confirmation code even when 2FA is disabled** — when that happens the app parks at `awaiting_code` and you submit the code to `POST /login/code`. The live session's cookies are held in memory.

## The files

- `browser.py` — one long-lived persistent browser context (lazy singleton, reused across requests, lock-serialized). Holds the login session in `./profile`, plus the login state machine and the structured-section extractor.
- `app.py` — FastAPI app exposing `POST /extract`, `POST /login/code`, `GET /login/status`, `GET /health`, and a small web UI at `/`.
- `bootstrap_login.py` — optional standalone helper that opens a **real browser window** for a one-off manual login (handy to clear a CAPTCHA by hand). The app does not need it.

## Quick start (install → running)

Everything below runs from the repo root. Steps 1–4 are one-time; step 5 is how
you start the app from then on. Detailed explanations of each step follow in the
sections below.

**0. Prerequisites** — install [`uv`](https://docs.astral.sh/uv/) (provides
Python 3.13; cloakbrowser has no 3.14 build):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # skip if uv is already installed
```

**1. Create the virtualenv and install dependencies:**

```bash
uv venv venv --python 3.13
uv pip install --python venv/bin/python -r requirements.txt
```

**2. Download the stealth Chromium binary** (~200MB, one time, shared across projects):

```bash
venv/bin/python -c "import cloakbrowser; cloakbrowser.ensure_binary()"
```

**3. Add the scraping account credentials:**

```bash
cp .env.example .env        # then edit .env and set LINKEDIN_EMAIL / LINKEDIN_PASSWORD
```

**4. Start the app** (serves `http://127.0.0.1:5000`, logs in headless in the background):

```bash
venv/bin/python app.py
```

**5. Confirm it's logged in** (in another terminal):

```bash
curl -s http://127.0.0.1:5000/login/status
# {"state":"logged_in",...}  -> ready to scrape
```

If it shows `{"state":"awaiting_code",...}`, LinkedIn sent a confirmation code to
the account's email/phone — submit it:

```bash
curl -s -X POST http://127.0.0.1:5000/login/code \
  -H 'Content-Type: application/json' -d '{"code": "123456"}'
```

**6. Scrape a page:**

```bash
curl -s -X POST http://127.0.0.1:5000/extract \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.linkedin.com/in/joaquim-medeiros/"}'
```

Or open `http://127.0.0.1:5000/` in a browser for the web UI.

## Docker

The image bakes the stealth Chromium binary in (no runtime download) and installs
the Chromium OS libraries via `playwright install-deps`.

```bash
cp .env.example .env          # set LINKEDIN_EMAIL / LINKEDIN_PASSWORD
docker compose up --build     # serves http://127.0.0.1:5000
```

Then drive it exactly like the local app — login starts automatically on boot:

```bash
curl -s http://127.0.0.1:5000/login/status
# if awaiting_code:
curl -s -X POST http://127.0.0.1:5000/login/code \
  -H 'Content-Type: application/json' -d '{"code":"123456"}'
```

Notes:
- The logged-in session persists in the `linkedin_profile` named volume, so a
  restart reuses it instead of re-authenticating (and re-triggering a code).
- `CHROMIUM_NO_SANDBOX=true` is set in the image — Chromium can't use its sandbox
  in a container. These are launch flags invisible to pages, so stealth is intact.
- `shm_size: 1gb` (compose) gives Chromium memory headroom.
- Plain `docker` without compose:
  ```bash
  docker build -t linkedin-scraper .
  docker run -d -p 5000:5000 --shm-size=1g --env-file .env \
    -v linkedin_profile:/app/profile linkedin-scraper
  ```
- The image is ~1.9GB (Python + Chromium 146 + its libraries).

## Stealth Chromium binary

- The stealth Chromium binary (~200MB) downloads once to `~/.cloakbrowser`.
- It is shared across every project and is not part of this repo.

## Step 1 — Add credentials

```bash
cp .env.example .env          # then set LINKEDIN_EMAIL / LINKEDIN_PASSWORD
```

In CI/CD, set `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` as secrets instead of
committing a `.env`. cloakbrowser's stealth fingerprinting handles the
bot-detection / CAPTCHA layer, so the headless login normally goes through —
the one thing it can't do is read a confirmation code sent to your phone/email,
which is what `POST /login/code` is for.

## Step 2 — Start the app

```bash
venv/bin/python app.py     # serves http://127.0.0.1:5000 (uvicorn)
```

On startup the app logs in **headless** in the background: it reuses the session
in `./profile` if still valid, otherwise logs in with `LINKEDIN_EMAIL` /
`LINKEDIN_PASSWORD`. The server comes up immediately so you can submit a
confirmation code if one is required. Watch the login state:

```bash
curl -s http://127.0.0.1:5000/login/status
# {"state":"logged_in","detail":"...","logged_in":true}
```

`state` is one of: `logging_in`, `awaiting_code`, `logged_in`, `no_credentials`,
`failed`.

### If LinkedIn asks for a confirmation code

When `state` is `awaiting_code`, LinkedIn sent a code to the account's
email/phone. Submit it (the headless challenge page is kept open waiting for it):

```bash
curl -s -X POST http://127.0.0.1:5000/login/code \
  -H 'Content-Type: application/json' \
  -d '{"code": "123456"}'
```

On success the state flips to `logged_in`. A wrong/expired code returns `409` and
stays `awaiting_code` so you can retry. The web UI at `/` shows a code box
automatically when one is needed.

## Step 3 — Scrape a page

### Easiest: the web UI

Open **http://127.0.0.1:5000/**, paste a LinkedIn URL, optionally tick **Full
scrape** for a company (pulls every sub-tab), and click **Scrape**. The status
chip top-right reflects login state and prompts for a code when needed.

### API / curl

Scrape a **profile** (returns structured sections):

```bash
curl -s -X POST http://127.0.0.1:5000/extract \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.linkedin.com/in/joaquim-medeiros/"}'
```

Profile/page response shape: `{ "url", "type", "final_url", "title", "name",
"headline", "location", "top_card_lines", "sections", "html", "html_length",
"text", "text_length", "fetched_at" }`.

- `type` is `profile`, `company`, or `page`.
- `sections` is an object keyed by the normalized section title — e.g.
  `about`, `experience`, `education`, `licenses_and_certifications`,
  `recommendations`, `interests`. Each section has `{ "title", "text", "links" }`,
  where `text` is the section's clean visible text and `links` are the
  company/school/profile URLs it references.

Scrape a **company**, full multi-tab — visits every sub-tab (about, posts, jobs,
people, insights, life — whichever the nav exposes):

```bash
curl -s -X POST http://127.0.0.1:5000/extract \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.linkedin.com/company/kruncher/", "full": true}'
```

Full-company response shape: `{ "url", "type": "company", "base_url", "slug",
"header", "sections": { "home": {...}, "about": {...}, ... }, "fetched_at" }`,
where each section has the profile/page fields above. `header` is the company
card + tab bar that LinkedIn renders atop **every** section page; because it's
identical across sections it's lifted out once and stripped from each section's
`text`. (`full` is ignored for non-company URLs.)

`/extract` returns `409` until login reaches `logged_in` — check `/login/status`
first.

Pull just one section's text (needs `jq`):

```bash
curl -s -X POST http://127.0.0.1:5000/extract \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.linkedin.com/in/some-profile/"}' | jq -r '.sections.experience.text'
```

## Authentication (optional)

Set `API_AUTH_SHA256` to the SHA-256 hex of a shared secret to lock **every**
endpoint. Clients then send the secret on each request:

```bash
curl -s -H "Authorization: Bearer <secret>" "$BASE/health"   # or:
curl -s -H "X-API-Key: <secret>" "$BASE/health"
```

Only the hash is stored (it's preimage-resistant, so safe to commit/keep in
compose). Generate it with
`python3 -c "import hashlib;print(hashlib.sha256(b'YOUR_SECRET').hexdigest())"`.
Unauthenticated requests get `401`. Unset the var to disable auth (local default).

## API reference (curl)

All endpoints are on `http://127.0.0.1:5000`. Start the app first
(`venv/bin/python app.py`). `BASE=http://127.0.0.1:5000` is used below for brevity.
If `API_AUTH_SHA256` is set, add `-H "X-API-Key: <secret>"` to every call below.

**Health check** — liveness plus current login state:

```bash
curl -s "$BASE/health"
# {"status":"ok","state":"logged_in","detail":"...","logged_in":true}
```

**Auth status** — just the login state (`logging_in` · `awaiting_code` ·
`logged_in` · `no_credentials` · `failed`):

```bash
curl -s "$BASE/login/status"
# {"state":"awaiting_code","detail":"confirmation code required (url: ...)","logged_in":false}
```

**Submit a confirmation code** — when state is `awaiting_code` (LinkedIn texted/
emailed the account a code). Returns `200` on success, `409` if rejected (retry):

```bash
curl -s -X POST "$BASE/login/code" \
  -H 'Content-Type: application/json' \
  -d '{"code": "123456"}'
# {"ok":true,"state":"logged_in"}
```

There is no "initialize/login" call to make — login starts automatically on app
startup. Poll `/login/status` until it is `logged_in` (submitting a code if
asked). A one-liner that waits for readiness:

```bash
until curl -sf "$BASE/login/status" | grep -q '"state":"logged_in"'; do
  echo "waiting for login… (POST /login/code if awaiting_code)"; sleep 3
done; echo "logged in"
```

**Reset the session** — wipe the context, `./profile`, and in-memory cookies,
then start a fresh headless login. Use this to test the cold-start /
confirmation-code path. Poll `/login/status` afterwards (it will likely go to
`awaiting_code`):

```bash
curl -s -X POST "$BASE/login/reset"
# {"reset":true,"message":"fresh login started — poll GET /login/status","state":"idle",...}
```

**Typed company data + posts** — `POST /company` returns the Voyager-based
envelope (typed fields like `companyId`, `employeeCount`, `tagline`,
`followerCount`, `industryV2Taxonomy`, `foundedOn`, logo/cover image URLs, plus
recent posts). Accepts a company URL **or** a bare slug:

```bash
curl -s -X POST "$BASE/company" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.linkedin.com/company/freda-ab/"}'
# or: -d '{"url": "freda-ab"}'
```

Response: `{ "analysisId", "website", "array_content": { "content": [ … ],
"pdf_path": null }, "responseCode": "1000", "error": "" }`. `content` holds a
`LINKEDIN_COMPANY_PRO` entry (`html_dump` = the typed object, `text_dump` = its
JSON string) and a `LINKEDIN_POST` entry (`text_dump` = a JSON array of posts).
`responseCode` is `"1000"` on success, `"1001"` if the company wasn't found.
This pulls from LinkedIn's authenticated Voyager API rather than the DOM, so the
fields are clean and typed. (PDF generation — `pdf_path` — is a later addition.)

**Typed profile data** — `POST /profile` returns typed profile fields (name,
headline, location, about, `experience[] {company,title,employmentType,dateRange,location}`,
`education[] {school,degree,dates}`, `licenses[]`, `skills[]`, plus extra
sections) in a `LINKEDIN_PROFILE_PRO` envelope. Accepts a profile URL **or** a
bare vanity slug:

```bash
curl -s -X POST "$BASE/profile" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.linkedin.com/in/joaquim-medeiros/"}'
# or: -d '{"url": "joaquim-medeiros"}'
```

Unlike companies (whose Voyager REST API still works), LinkedIn deprecated the
clean profile API, so `/profile` reads the **SDUI component endpoints** the live
profile page uses and parses the typed fields out. Less granular than the old
profile API, but it works and avoids rotating GraphQL query IDs.

**Scrape a profile (raw DOM sections)** — `POST /extract` returns the sections as
clean text (`about`, `experience`, `education`, …) rather than typed fields:

```bash
curl -s -X POST "$BASE/extract" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.linkedin.com/in/joaquim-medeiros/"}'
```

**Scrape a company (single landing page):**

```bash
curl -s -X POST "$BASE/extract" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.linkedin.com/company/kruncher/"}'
```

**Scrape a company, full multi-tab** (`full: true` — about, posts, jobs, people,
insights, life — whichever the nav exposes):

```bash
curl -s -X POST "$BASE/extract" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.linkedin.com/company/kruncher/", "full": true}'
```

**Useful `jq` slices** (needs `jq`):

```bash
# one profile section's text
curl -s -X POST "$BASE/extract" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.linkedin.com/in/joaquim-medeiros/"}' | jq -r '.sections.experience.text'

# list which sections came back
curl -s -X POST "$BASE/extract" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.linkedin.com/in/joaquim-medeiros/"}' | jq -r '.sections | keys[]'

# save the raw rendered HTML to a file
curl -s -X POST "$BASE/extract" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.linkedin.com/in/joaquim-medeiros/"}' | jq -r '.html' > profile.html
```

Note: `/extract` returns `409` until login reaches `logged_in` — check
`/login/status` first. Interactive OpenAPI docs are also served at `$BASE/docs`.

## Why structured sections, not typed fields

LinkedIn's current profile/company pages are React-rendered with fully hashed
class names and **no `<ul>/<li>`** for entries — the only stable anchors are the
section `<h2>` titles. So the scraper keys on those and returns each section's
clean `innerText` plus its entity links. That's resilient to LinkedIn's dynamic
DOM. It does **not** emit typed fields (company, degree, dates as separate keys);
for those, run an LLM pass over `sections[*].text` or the raw `html`.

## Logs / debugging

- Every request logs to stdout and a rotating `scraper.log` (2MB × 3 backups).
- Each `/extract` logs a short request id, the requested URL, the **final URL**
  after navigation, the page **title**, the HTML/text char counts, and the
  discovered section keys.
- Redirects and `/login` · `/authwall` · `/checkpoint` hits are logged as warnings.
- If `final_url` lands somewhere other than the page you asked for, or `text_length`
  is tiny, the render missed — use the raw `html` field instead.

```bash
tail -f scraper.log
```

## Tests

```bash
venv/bin/python -m pytest -m "not e2e"   # fast, offline unit tests
venv/bin/python -m pytest -m e2e         # real headless scrape of kruncher
```

- Unit tests fake cloakbrowser, so they need no network or credentials.
- The e2e test (`tests/test_e2e.py`) launches cloakbrowser headless, logs in, and
  scrapes `https://www.linkedin.com/company/kruncher/`. It is **skipped** unless
  `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` are set, and skips itself if login can't
  complete headless (e.g. a confirmation code is required).

## Caveats

- **ToS & bans:** automated LinkedIn extraction violates LinkedIn's ToS and carries real account-ban risk.
- **IP reputation:** datacenter IPs are flagged quickly; a residential proxy (pass `proxy=...` to `launch_persistent_context` in `browser.py`) survives longer.
- **Session expiry:** if `/login/status` reports a non-`logged_in` state, restart the app (or re-run `bootstrap_login.py`) to re-establish the session.
- **Cookies are in-memory:** the live session's cookies are held in the running process (and on disk in `./profile` for restart reuse). There is no per-account / database persistence yet.
- **Concurrency:** by default requests are serialized by a single browser worker thread (one context is not thread-safe). An **opt-in scrape pool** (`SCRAPE_POOL_SIZE`, default `1`/off) runs N cookie-seeded browser contexts in parallel for higher throughput. When enabled, a bounded queue with `SCRAPE_QUEUE_TIMEOUT` (default `0` = wait forever; set e.g. `20` to return `503 + Retry-After` under pile-on instead of queuing indefinitely) and a hard per-scrape `SCRAPE_DEADLINE_SECONDS` (default `180`, → `504`) provide backpressure. The pool is **off by default** — start at 2 and watch for LinkedIn `/checkpoint` pushback before raising. See `docs/SCALING.md` for multi-replica scaling.
- **Other resilience knobs (on by default):** an in-memory TTL result cache + single-flight coalescing (`?force=1` to bypass), a session circuit breaker (`CB_THRESHOLD`/`CB_COOLDOWN_SECONDS` → `409` on a dead session, cached data still flows), async jobs (`?async=1` → `202 + job_id`, poll `GET /jobs/{id}` or stream `GET /jobs/{id}/events`), and forced Chromium reaping on every abandon path (no orphan/OOM under load). Inspect live state via `GET /health`.
- **Dynamic DOM:** LinkedIn rotates class names and the confirmation-code page markup; extraction keys on stable `<h2>`/`innerText` and the code input is matched against several candidate selectors.
