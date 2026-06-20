export const meta = {
  name: 'build-linkedin-scraper',
  description: 'Explore CloakBrowser + trafilatura, then scaffold and verify a reusable Flask LinkedIn scraper (stealth-browser render -> raw HTML + trafilatura text)',
  whenToUse: 'Re-build or re-scaffold the linkedin-scraper app from scratch in a target directory. Pass {dir, python} via args to override defaults.',
  phases: [
    { title: 'Verify deps', detail: 'confirm cloakbrowser version + Python support, trafilatura' },
    { title: 'Scaffold', detail: 'write requirements, .gitignore, browser.py, bootstrap_login.py, app.py, README' },
    { title: 'Build & verify', detail: 'uv venv (pinned), install, download Chromium once, compile + import smoke test, self-heal' },
  ],
}

// ---- parameters -----------------------------------------------------------
const DIR = (args && args.dir) || '/home/asari/dojo/linkedin-scraper'
// cloakbrowser supports Python 3.9-3.13; pin away from a system 3.14.
const PY = (args && args.python) || '3.13'

// ---------------------------------------------------------------------------
phase('Verify deps')

const DEPS = {
  type: 'object',
  properties: {
    cloakbrowser_version: { type: 'string', description: 'latest cloakbrowser version on PyPI' },
    max_python: { type: 'string', description: 'highest Python minor version cloakbrowser supports' },
    trafilatura_ok: { type: 'boolean', description: 'trafilatura still accepts an HTML string in extract()' },
    notes: { type: 'string' },
  },
  required: ['cloakbrowser_version', 'max_python', 'trafilatura_ok'],
}

const deps = await agent(
  `Confirm the current dependency facts for a LinkedIn scraper build. Use WebFetch on https://pypi.org/pypi/cloakbrowser/json and the trafilatura docs.
   Return: the latest cloakbrowser version, the highest Python minor version it supports (Requires-Python), and whether trafilatura.extract() still accepts a raw HTML string (it should).
   This is a sanity check before scaffolding ${DIR}; keep it brief.`,
  { label: 'verify-deps', phase: 'Verify deps', schema: DEPS, agentType: 'Explore' }
)

log(`cloakbrowser ${deps.cloakbrowser_version} (Python <= ${deps.max_python}); building in ${DIR} with Python ${PY}`)

// ---------------------------------------------------------------------------
phase('Scaffold')

// Each entry: the exact file and a precise spec. Agents author the file to the
// spec, matching the locked design decisions (raw HTML primary, trafilatura
// fallback, manual-first login, persistent-context singleton + lock).
const FILES = [
  {
    path: 'requirements.txt',
    spec: `Three lines, exactly:
cloakbrowser==${deps.cloakbrowser_version || '0.3.31'}
trafilatura
flask`,
  },
  {
    path: '.gitignore',
    spec: `Lines: venv/  profile/  state.json  .env  __pycache__/  *.pyc  (one per line). These hold the venv, the persisted browser profile, the session backup, and secrets — none should be committed.`,
  },
  {
    path: 'browser.py',
    spec: `A module holding ONE long-lived cloakbrowser persistent context reused across all Flask requests. Requirements:
- imports: os, time, threading, pathlib.Path, cloakbrowser as cb
- BASE_DIR = Path(__file__).resolve().parent ; PROFILE_DIR = str(BASE_DIR/'profile') ; STATE_FILE = str(BASE_DIR/'state.json')
- module-level: _ctx = None ; _lock = threading.Lock()
- _get_context(): lazily create _ctx via cb.launch_persistent_context(PROFILE_DIR, headless=True, humanize=True) and cache it. Never logs in (that's bootstrap_login.py's job).
- fetch_html(url) -> str: acquire _lock; ctx = _get_context(); page = ctx.new_page(); page.goto(url, wait_until='domcontentloaded', timeout=60000); then 4 iterations of page.mouse.wheel(0,1800) + time.sleep(1.2) to trigger lazy sections — use NATIVE time.sleep, never page.wait_for_timeout (it emits CDP signals anti-bot detects); return page.content(); always page.close() in finally (keep the context alive).
- is_logged_in() -> bool: under _lock, open a page, goto 'https://www.linkedin.com/feed/' (wait_until='domcontentloaded', timeout=30000), return True only if neither '/login' nor '/authwall' is in page.url; page.close() in finally.
Add a short module docstring explaining the singleton + lock (one context is not thread-safe under Flask's threaded server).`,
  },
  {
    path: 'bootstrap_login.py',
    spec: `A ONE-TIME interactive login script (run once before the app). Requirements:
- imports: os, sys, time, cloakbrowser as cb, and  from browser import PROFILE_DIR, STATE_FILE
- main(): read LINKEDIN_EMAIL / LINKEDIN_PASSWORD from os.environ; if either missing, print a hint and return 1.
- launch a HEADED context: cb.launch_persistent_context(PROFILE_DIR, headless=False, humanize=True); new_page; goto 'https://www.linkedin.com/login'.
- human-like typing: page.type('#username', email, delay=80); short sleep; page.type('#password', password, delay=80); short sleep; page.click('button[type=submit]').
- then PRINT clear instructions and PAUSE with input('Press Enter once fully logged in... ') so the human clears any 2FA/CAPTCHA/checkpoint and lands on the feed.
- after the pause: ctx.storage_state(path=STATE_FILE); print where the session was persisted; ctx.close(); return 0.
- if __name__ == '__main__': sys.exit(main())
Docstring must state that automated unattended login is the biggest account-lock trigger, hence manual-first and run-once.`,
  },
  {
    path: 'app.py',
    spec: `A minimal Flask app wrapping browser.py. Requirements:
- imports: from datetime import datetime, timezone ; from urllib.parse import urlparse ; import trafilatura ; from flask import Flask, jsonify, request ; import browser
- app = Flask(__name__)
- _is_linkedin(url) helper: parse the host with urlparse; return True only if host == 'linkedin.com' or host endswith '.linkedin.com'. MUST reject spoofs like 'evil-linkedin.com.attacker.net'. Guard against ValueError.
- POST /extract: read JSON body; 400 if no 'url'; 400 if not _is_linkedin(url); call browser.fetch_html(url) inside try/except returning 502 with the error on failure; text = trafilatura.extract(html); return jsonify(url=url, html=html, text=text, fetched_at=datetime.now(timezone.utc).isoformat()). Comment that raw HTML is the source of truth and trafilatura text is a best-effort fallback (it flattens structured profile data).
- GET /health: return jsonify(status='ok', logged_in=browser.is_logged_in()); on exception return status='error' with detail, 500.
- if __name__ == '__main__': app.run(host='127.0.0.1', port=5000)`,
  },
  {
    path: 'README.md',
    spec: `A concise README in BULLET form (no tables, no ascii diagrams). Cover: what the app does (logged-in cloakbrowser renders a LinkedIn page; Flask returns raw HTML + best-effort trafilatura text); the three files (browser.py = reused persistent-context singleton, bootstrap_login.py = run-once login, app.py = Flask /extract + /health); that the ~200MB stealth Chromium downloads once to ~/.cloakbrowser and is shared across projects; setup commands (uv venv venv --python ${PY}; uv pip install --python venv/bin/python -r requirements.txt; ensure_binary); step 1 = export LINKEDIN_EMAIL/PASSWORD then venv/bin/python bootstrap_login.py (clear 2FA in the window, press Enter); step 2 = venv/bin/python app.py then curl examples for /health and POST /extract; caveats = LinkedIn ToS/ban risk, datacenter IPs flagged fast (residential proxy survives longer via the proxy= arg), session expiry -> re-run bootstrap, requests serialized by a lock, and that ~/dojo/linkedin-cli is a more reliable structured-data alternative.`,
  },
]

await parallel(FILES.map((f) => () =>
  agent(
    `Write the file ${DIR}/${f.path}. Create parent dirs if needed. Author it to EXACTLY this spec — no extra files, no speculative features, match a clean idiomatic style:\n\n${f.spec}`,
    { label: f.path, phase: 'Scaffold' }
  )
))

// ---------------------------------------------------------------------------
phase('Build & verify')

const REPORT = {
  type: 'object',
  properties: {
    venv_python: { type: 'string', description: 'python version actually used by the venv' },
    installed: { type: 'boolean' },
    binary_path: { type: 'string', description: 'path to the downloaded stealth Chromium' },
    compiles: { type: 'boolean' },
    imports: { type: 'boolean' },
    spoof_rejected: { type: 'boolean', description: 'host validation rejected evil-linkedin.com.attacker.net' },
    fixes_applied: { type: 'array', items: { type: 'string' }, description: 'any files edited to make checks pass' },
    summary: { type: 'string' },
  },
  required: ['installed', 'compiles', 'imports', 'spoof_rejected', 'summary'],
}

const report = await agent(
  `Build and verify the scaffolded app in ${DIR}. Run these steps with Bash and FIX any file (Edit/Write) that makes a check fail, then re-run that check:
1. uv venv venv --python ${PY}   (uv will fetch the toolchain if needed)
2. uv pip install --python venv/bin/python -r requirements.txt
3. Download the stealth Chromium ONCE:  venv/bin/python -c "import cloakbrowser; print(cloakbrowser.ensure_binary())"  (idempotent; ~200MB first time)
4. Compile:  venv/bin/python -m py_compile app.py browser.py bootstrap_login.py
5. Import smoke test (no server, no browser launch — the context is lazy and the server is behind __main__):  venv/bin/python -c "import app; print(sorted(r.rule for r in app.app.url_map.iter_rules() if r.endpoint!='static'))"
6. Host-validation test: assert app._is_linkedin('https://www.linkedin.com/in/x') is True and app._is_linkedin('https://evil-linkedin.com.attacker.net/x') is False.
Do NOT run the Flask server and do NOT attempt to log in. Report the venv python version, install success, binary path, compile/import/spoof results, and any fixes you applied.`,
  { label: 'build-verify', phase: 'Build & verify', schema: REPORT }
)

return { deps, report }
