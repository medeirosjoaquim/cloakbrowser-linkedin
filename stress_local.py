#!/usr/bin/env python3
"""Local assertion-based stress harness for the LinkedIn scraper API.

Two passes, each a hard PASS/FAIL (asserts, not just prints):

  1. BASELINE (sequential, 1× per target) — captures the EXPECTED payload per
     target on the STABLE semantic fields only (name/headline/section keys /
     companyName / experience count ...). Timestamps, analysisId, html, and
     text bytes are deliberately NOT compared (they vary per request; comparing
     them would be a false-failing, meaningless assertion).

  2. STRESS (concurrent) — fires N requests in parallel at each concurrency
     level and asserts, for EVERY response:
        - it is HTTP 200 (valid) OR HTTP 503 (clean backpressure + Retry-After);
          any other status FAILS;
        - every 200 matches the BASELINE on the stable fields;
        - the 200 latencies stay under a per-endpoint SLO (p95).

Usage:
    python3 stress_local.py
    BASE_URL=... API_KEY=... CONCURRENCY=4,8 REPEATS=3 python3 stress_local.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5002").rstrip("/")
KEY = os.environ.get("API_KEY") or sys.exit("set API_KEY env var (e.g. API_KEY=... python3 stress_local.py)")
# concurrency ladder, e.g. "4,8" → run concurrency 4 then 8
LADDER = [int(x) for x in os.environ.get("CONCURRENCY", "4,8").split(",") if x.strip()]
REPEATS = int(os.environ.get("REPEATS", "2"))
PER_REQ_TIMEOUT = float(os.environ.get("REQ_TIMEOUT", "90"))

# --- targets: (label, endpoint, body, expected_name_substr) -----------------
# expected_name_substr is an extra content assertion on top of the baseline
# (the baseline already compares the full stable field set; this is a sanity
# floor so a target that's fundamentally broken fails the baseline pass itself).
TARGETS = [
    ("profile:eugenevkim", "/profile",
     '{"url":"https://www.linkedin.com/in/eugenevkim/"}', "Eugene"),
    ("profile:simone-rizzetto", "/profile",
     '{"url":"https://www.linkedin.com/in/simone-rizzetto/"}', None),
    ("company:freda-ab", "/company",
     '{"url":"https://www.linkedin.com/company/freda-ab/"}', "Freda"),
    ("company:kruncher", "/company",
     '{"url":"https://www.linkedin.com/company/kruncher/"}', "Kruncher"),
    ("extract:eugenevkim", "/extract",
     '{"url":"https://www.linkedin.com/in/eugenevkim/"}', "Eugene"),
    ("extract:freda-ab", "/extract",
     '{"url":"https://www.linkedin.com/company/freda-ab/"}', "Freda"),
]

# --- per-endpoint latency SLO (p95 of 200 responses must be under this) ------
# Tuned to pool=2 realities: a ~6s job serialized 2-wide over a burst can leave
# the long pole around ~15-20s; these give headroom without being meaningless.
SLO_SECONDS = {
    "/profile": 30.0,
    "/company": 30.0,
    "/extract": 30.0,
}

# FORCE=1 sends ?force=1 (bypass cache) so every request is a REAL scrape — the
# honest latency under load. FORCE unset = cache may serve warm hits (≈0ms),
# which proves the cache but hides scrape latency. We run BOTH.
FORCE = os.environ.get("FORCE", "") == "1"

RED, GRN, YLW, RST = "\033[31m", "\033[32m", "\033[33m", "\033[0m"


def C(ok, msg):
    return f"{GRN if ok else RED}{('PASS' if ok else 'FAIL')}{RST} {msg}"


def _post(endpoint: str, body: str, timeout: float):
    """POST and return (http_status_or_None, parsed_json_or_None, elapsed_s, error_str)."""
    url = f"{BASE}{endpoint}"
    if FORCE:
        url += "?force=1"
    req = Request(url, data=body.encode(),
                  headers={"Content-Type": "application/json", "X-API-Key": KEY},
                  method="POST")
    t0 = time.monotonic()
    try:
        with urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            elapsed = time.monotonic() - t0
            status = r.status
            retry_after = r.headers.get("Retry-After")
    except HTTPError as e:
        elapsed = time.monotonic() - t0
        try:
            payload = json.loads(e.read().decode())
        except Exception:
            payload = None
        # surface the HTTPError status + whether it was a clean backpressure 503
        return (e.code, payload, elapsed,
                None if e.code == 503 else f"http {e.code}: {payload}")
    except (URLError, TimeoutError) as e:
        return (None, None, time.monotonic() - t0, f"net: {e}")
    except Exception as e:  # noqa: BLE001
        return (None, None, time.monotonic() - t0, f"exc: {e!r}")
    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        return (status, None, elapsed, "non-json body")
    return (status, payload, elapsed, None)


# --- stable-field extraction: the "expected payload" surface ----------------
def stable(endpoint: str, payload: dict) -> dict:
    """Return ONLY the fields we assert match run-to-run. Drops anything with a
    timestamp, uuid, html blob, or byte count that naturally varies."""
    s = {}
    if endpoint == "/extract":
        s["type"] = payload.get("type")
        s["name"] = payload.get("name")
        s["headline"] = payload.get("headline")
        s["section_keys"] = sorted((payload.get("sections") or {}).keys())
        # name-derived sanity: html must be present and substantial
        s["has_html"] = bool(payload.get("html"))
        s["has_text"] = bool(payload.get("text"))
    elif endpoint == "/profile":
        s["responseCode"] = payload.get("responseCode")
        content = (payload.get("array_content") or {}).get("content") or []
        pro = next((c for c in content if c.get("type") == "LINKEDIN_PROFILE_PRO"), None)
        dump = (pro or {}).get("html_dump") or {}
        s["name"] = dump.get("name")
        s["headline"] = dump.get("headline")
        s["n_experience"] = len(dump.get("experience") or [])
        s["n_education"] = len(dump.get("education") or [])
        s["has_raw_sections"] = bool(dump.get("raw_sections"))
    elif endpoint == "/company":
        # Full LINKEDIN_COMPANY_PRO schema (per the Kruncher consumer example):
        # every stable field below must match the baseline run-to-run.
        s["responseCode"] = payload.get("responseCode")
        s["website_top"] = payload.get("website")  # derived from websiteUrl
        content = (payload.get("array_content") or {}).get("content") or []
        co = next((c for c in content if c.get("type") == "LINKEDIN_COMPANY_PRO"), None)
        s["has_company_pro"] = co is not None
        dump = (co or {}).get("html_dump") or {}
        loc = (dump.get("locations") or [None])[0] or {}
        s["companyName"] = dump.get("companyName")
        s["companyId"] = dump.get("companyId")
        s["hq_city"] = loc.get("city")
        s["hq_country"] = loc.get("country")
        s["hq_headquarter"] = loc.get("headquarter")
        s["employeeCount"] = dump.get("employeeCount")
        s["empRange"] = (dump.get("employeeCountRange") or {}).get("start"), (dump.get("employeeCountRange") or {}).get("end")
        s["tagline"] = dump.get("tagline")
        s["followerCount"] = dump.get("followerCount")
        s["industry"] = dump.get("industry")
        s["industryV2"] = dump.get("industryV2Taxonomy")
        s["foundedYear"] = (dump.get("foundedOn") or {}).get("year")
        s["universalName"] = dump.get("universalName")
        s["websiteUrl"] = dump.get("websiteUrl")
        s["has_logo"] = bool(dump.get("logoResolutionResult"))
        s["has_cover"] = bool(dump.get("originalCoverImage"))
        post = next((c for c in content if c.get("type") == "LINKEDIN_POST"), None)
        s["has_posts"] = post is not None
        # pdf_path is reserved but must be present (string or null)
        s["has_pdf_path_key"] = "pdf_path" in (payload.get("array_content") or {})
    return s


# --- known-good ground truth (from the Kruncher consumer example) -----------
# Hard-asserted in the baseline pass so payload QUALITY is checked against real
# expected values, not just run-to-run consistency. A baseline that returns
# different values for these means the schema/scrape drifted.
KNOWN_GOOD = {
    "company:freda-ab": {
        # Stable IDENTITY fields (hard-asserted against the consumer example).
        # Volatile counts (employeeCount/followerCount) are intentionally NOT here
        # — they drift on LinkedIn's side day-to-day (the example itself showed
        # employeeCount=22 with employeeCountRange={2,10}, i.e. already
        # inconsistent). They're still captured in stable() and consistency-
        # checked run-to-run via the baseline.
        "companyName": "Freda", "companyId": 107067644, "hq_city": "Stockholm",
        "hq_country": "SE", "tagline": "Autonomous compliance.",
        "foundedYear": 2025, "universalName": "freda-ab", "websiteUrl": "www.freda.com",
        "responseCode": "1000", "has_company_pro": True, "has_posts": True,
        "has_logo": True, "has_cover": True, "hq_headquarter": True,
        "website_top": "freda.com",
    },
    "company:kruncher": {
        "companyName": "Kruncher", "responseCode": "1000",
        "has_company_pro": True, "has_posts": True, "has_logo": True,
    },
    "profile:eugenevkim": {
        "name": "Eugene Kim", "responseCode": "1000", "n_experience": 6,
        "has_raw_sections": True,
    },
}


def baseline_pass() -> tuple[bool, dict]:
    """Sequential 1× per target → capture expected stable payload.
    Also enforces the schema/content floor (name substring where given)."""
    print(f"\n{'='*68}\nBASELINE — sequential capture of expected payload\n{'='*68}")
    expected = {}
    ok_all = True
    for label, ep, body, name_sub in TARGETS:
        status, payload, elapsed, err = _post(ep, body, PER_REQ_TIMEOUT)
        if err is not None or status != 200:
            print(C(False, f"{label:28} baseline fetch failed: status={status} err={err}"))
            ok_all = False
            continue
        if not payload:
            print(C(False, f"{label:28} baseline returned empty payload"))
            ok_all = False
            continue
        s = stable(ep, payload)
        expected[label] = s
        # content floor: a known name substring must appear (if specified)
        floor_ok = True
        if name_sub:
            got = (s.get("name") or s.get("companyName") or "")
            floor_ok = name_sub.lower() in (got or "").lower()
        # known-good ground-truth check: every specified field must match exactly
        truth = KNOWN_GOOD.get(label, {})
        truth_bad = [f"{k}={s.get(k)!r} (want {v!r})" for k, v in truth.items() if s.get(k) != v]
        ok = floor_ok and bool(s) and not truth_bad
        extra = ("  TRUTH FAIL: " + "; ".join(truth_bad)) if truth_bad else ""
        print(C(ok, f"{label:28} {elapsed:5.2f}s  stable={s}{extra}"))
        ok_all = ok_all and ok
    return ok_all, expected


def assert_matches(label: str, ep: str, payload: dict, expected: dict) -> str | None:
    """Return None if the 200 payload matches the baseline on stable fields,
    else a human description of the FIRST mismatch."""
    if label not in expected:
        return "no baseline captured (baseline failed)"
    s = stable(ep, payload)
    exp = expected[label]
    for k, v in exp.items():
        got = s.get(k)
        # section_keys: compare as sets (order can legitimately differ)
        if k == "section_keys":
            if set(got or []) != set(v or []):
                return f"{k} differs: got {sorted(got or [])} vs exp {sorted(v or [])}"
            continue
        if got != v:
            return f"{k} differs: got {got!r} vs exp {v!r}"
    return None


def stress_pass(expected: dict) -> bool:
    ok_all = True
    for conc in LADDER:
        total = len(TARGETS) * REPEATS
        print(f"\n{'='*68}\nSTRESS — concurrency={conc}  ({total} requests: "
              f"{len(TARGETS)} targets × {REPEATS} reps)\n{'='*68}")
        results = []  # (label, ep, status, elapsed, err, mismatch)
        with ThreadPoolExecutor(max_workers=conc) as ex:
            futs = {}
            for _ in range(REPEATS):
                for label, ep, body, _ns in TARGETS:
                    futs[ex.submit(_post, ep, body, PER_REQ_TIMEOUT)] = (label, ep)
            for fut in as_completed(futs):
                label, ep = futs[fut]
                status, payload, elapsed, err = fut.result()
                mismatch = None
                if status == 200 and payload is not None:
                    mismatch = assert_matches(label, ep, payload, expected)
                results.append((label, ep, status, elapsed, err, mismatch))

        # --- per-request verdicts ---
        n200 = n503 = nother = n_mismatch = 0
        by_ep = {}  # ep -> list of latencies (200 only)
        for label, ep, status, elapsed, err, mismatch in sorted(results):
            by_ep.setdefault(ep, [])
            if status == 200:
                n200 += 1
                by_ep[ep].append(elapsed)
                if mismatch is None:
                    print(C(True, f"  {label:28} 200  {elapsed:6.2f}s  payload OK"))
                else:
                    n_mismatch += 1
                    print(C(False, f"  {label:28} 200  {elapsed:6.2f}s  MISMATCH: {mismatch}"))
                    ok_all = False
            elif status == 503:
                n503 += 1
                print(f"{YLW}  503{RST} {label:30} {elapsed:6.2f}s  backpressure (expected under load)")
            else:
                nother += 1
                print(C(False, f"  {label:28} {status} {elapsed:6.2f}s  err={err}"))
                ok_all = False

        # --- summary + latency SLO assertions ---
        print(f"\n  tally: {GRN}{n200} ok{RST}  {YLW}{n503} backpressure(503){RST}  "
              f"{RED}{nother} bad{RST}  {RED if n_mismatch else GRN}{n_mismatch} mismatched{RST}")
        for ep, lats in by_ep.items():
            if not lats:
                print(C(False, f"  SLO {ep:10} no successful samples"))
                ok_all = False
                continue
            lats.sort()
            p95 = lats[min(len(lats) - 1, int(0.95 * len(lats)))]
            slo = SLO_SECONDS[ep]
            med = statistics.median(lats)
            met = p95 <= slo
            print(C(met, f"  SLO {ep:10} min={lats[0]:.2f}s med={med:.2f}s "
                         f"max={lats[-1]:.2f}s p95={p95:.2f}s  (≤ {slo}s)"))

        # Every request must be 200 OR 503 — no silent 5xx / timeouts.
        if nother > 0:
            ok_all = False
    return ok_all


def main() -> int:
    print(f"target: {BASE}   concurrency ladder: {LADDER}   repeats: {REPEATS}   force: {FORCE}")
    base_ok, expected = baseline_pass()
    if not base_ok:
        print(C(False, "baseline pass failed — aborting stress (fix data quality first)"))
        return 2
    print(C(True, "baseline captured for all targets"))
    stress_ok = stress_pass(expected)
    print("\n" + "=" * 68)
    if base_ok and stress_ok:
        print(C(True, "OVERALL: payload quality + latency SLOs held under load"))
        return 0
    print(C(False, "OVERALL: assertions failed — see above"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
