#!/usr/bin/env python3
"""Find the true backpressure threshold: fire N UNIQUE URLs concurrently so
single-flight can't coalesce. Reports 200/503/504/other + p95 latency of 200s +
data-quality (every 200 must be a valid envelope). Bounded via outer `timeout`.

Usage: timeout 300 python3 backpressure_probe.py
"""
import json, os, statistics, sys, time
import urllib.request as u
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5002")
KEY = os.environ.get("API_KEY") or sys.exit("set API_KEY env var (e.g. API_KEY=... python3 backpressure_probe.py)")
# Diverse URLs so single-flight can't coalesce (each is unique).
TARGETS = [
    ("/profile", "eugenevkim"), ("/profile", "simone-rizzetto"),
    ("/profile", "lisa-ezhergina"), ("/profile", "francescodeliva"),
    ("/profile", "williamhgates"), ("/profile", "satyanadella"),
    ("/company", "freda-ab"), ("/company", "kruncher"),
    ("/company", "openai"), ("/company", "microsoft"),
    ("/company", "google"), ("/company", "apple"),
    ("/company", "amazon"), ("/company", "tesla"),
    ("/company", "meta"), ("/company", "nvidia"),
]

def post(ep, slug):
    url = f"{BASE}{ep}?force=1"
    body = json.dumps({"url": slug}).encode()
    req = u.Request(url, data=body,
                    headers={"Content-Type": "application/json", "X-API-Key": KEY}, method="POST")
    t0 = time.monotonic()
    try:
        with u.urlopen(req, timeout=70) as r:
            raw = r.read().decode()
            el = time.monotonic() - t0
            return r.status, el, raw, None
    except u.HTTPError as e:
        el = time.monotonic() - t0
        try: body = e.read().decode()
        except Exception: body = ""
        return e.code, el, body, None
    except Exception as e:
        return None, time.monotonic()-t0, "", str(e)

def valid_envelope(ep, raw):
    """Data-quality gate: a 200 must be a well-formed envelope."""
    try: d = json.loads(raw)
    except Exception: return False, "non-json"
    if "array_content" not in d or "responseCode" not in d:
        return False, "missing envelope keys"
    content = d["array_content"].get("content") or []
    if ep == "/profile":
        ok = any(c.get("type") == "LINKEDIN_PROFILE_PRO" for c in content)
    else:
        ok = any(c.get("type") == "LINKEDIN_COMPANY_PRO" for c in content)
    return ok, ("no PRO content" if not ok else "ok")

def run(n):
    # pick first n unique targets
    targets = TARGETS[:n]
    results = []
    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = {ex.submit(post, ep, slug): (ep, slug) for ep, slug in targets}
        for fut in as_completed(futs):
            ep, slug = futs[fut]
            status, el, raw, err = fut.result()
            qok = True
            if status == 200:
                qok, _ = valid_envelope(ep, raw)
            results.append((slug, status, el, qok, err))
    return results

for n in (8, 12, 16):
    print(f"\n{'='*64}\nDIVERSE COLD BURST — {n} unique URLs at once (force=1)\n{'='*64}")
    t0 = time.monotonic()
    res = run(n)
    wall = time.monotonic() - t0
    n200 = [r for r in res if r[1] == 200]
    n503 = [r for r in res if r[1] == 503]
    n504 = [r for r in res if r[1] == 504]
    nother = [r for r in res if r[1] not in (200, 503, 504)]
    nbadq = [r for r in n200 if not r[3]]
    lats = sorted(r[2] for r in n200)
    p95 = lats[min(len(lats)-1, int(0.95*len(lats)))] if lats else float("nan")
    med = statistics.median(lats) if lats else float("nan")
    print(f"  200={len(n200)}  503(backpressure)={len(n503)}  504(deadline)={len(n504)}  other={len(nother)}")
    print(f"  data quality: {len(n200)-len(nbadq)}/{len(n200)} valid envelopes" + (f"  BAD: {[r[0] for r in nbadq]}" if nbadq else ""))
    if lats:
        print(f"  latency(200): min={lats[0]:.1f}s med={med:.1f}s max={lats[-1]:.1f}s p95={p95:.1f}s")
    print(f"  wall-time for the burst: {wall:.1f}s")
    for slug, status, el, qok, err in sorted(res, key=lambda r: r[2]):
        tag = "OK" if status == 200 and qok else ("503" if status == 503 else ("504" if status == 504 else f"ERR({status})"))
        print(f"    {tag:5} {el:6.1f}s  {slug}")
