"""Unit tests for the result cache (#1) + single-flight coalescing (#2).

The cache and single-flight machinery in browser.py is module-level state, so
these tests stub out the underlying _scrape (no browser) and assert the
cache/coalescing contract directly: repeat hits are free, concurrent same-key
requests share ONE scrape, failures aren't cached, force bypasses the cache,
and reset clears it.
"""

import threading
import time

import pytest

import browser


@pytest.fixture(autouse=True)
def clean_cache():
    browser._cache_clear()
    with browser._inflight_lock:
        browser._inflight.clear()
    with browser._runtime_lock:
        browser._runtime.update(
            cache_hits=0, cache_misses=0, cache_coalesced=0,
            last_scrape_ok=None, last_scrape_at=None, consecutive_failures=0,
        )
    with browser._state_lock:
        browser._wall_hits = 0
        browser._cb_open = False
        browser._cb_open_until = 0.0
    yield
    browser._cache_clear()
    with browser._inflight_lock:
        browser._inflight.clear()


def _stub(impl):
    """Replace _scrape with `impl` so no real browser runs."""
    saved = browser._scrape
    browser._scrape = impl
    return saved


def test_cache_hit_returns_cached_without_scraping():
    calls = []

    def scrape(impl, url):
        calls.append(url)
        return {"url": url, "n": len(calls)}

    saved = _stub(scrape)
    try:
        a = browser.fetch_html("https://www.linkedin.com/in/x/")
        b = browser.fetch_html("https://www.linkedin.com/in/x/")  # same url → cache hit
    finally:
        browser._scrape = saved

    assert a == b
    assert len(calls) == 1, "second hit must NOT re-scrape (it's cached)"
    stats = browser.runtime_stats()
    assert stats["cache_hits"] == 1
    assert stats["cache_misses"] == 1


def test_force_bypasses_cache():
    calls = []

    def scrape(impl, url):
        calls.append(url)
        return {"v": len(calls)}

    saved = _stub(scrape)
    try:
        browser.fetch_html("https://www.linkedin.com/in/x/")          # miss → scrape
        browser.fetch_html("https://www.linkedin.com/in/x/")          # hit
        browser.fetch_html("https://www.linkedin.com/in/x/", force=True)  # bypass → scrape
    finally:
        browser._scrape = saved

    assert len(calls) == 2, "force must re-scrape even on a cached key"


def test_single_flight_one_scrape_for_concurrent_callers():
    """N concurrent requests for the SAME url do exactly ONE scrape and all get
    the same result (the whole point of single-flight)."""
    started = threading.Event()
    release = threading.Event()
    scrapes = [0]
    lock = threading.Lock()

    def slow_scrape(impl, url):
        with lock:
            scrapes[0] += 1
        started.set()
        release.wait(5)              # hold the leader so followers pile up
        return {"url": url, "shared": True}

    saved = _stub(slow_scrape)
    results = [None] * 6
    threads = []

    def go(i):
        results[i] = browser.fetch_html("https://www.linkedin.com/in/same/")

    try:
        for i in range(6):
            t = threading.Thread(target=go, args=(i,))
            threads.append(t)
            t.start()
        assert started.wait(5)
        time.sleep(0.2)              # let the other 5 attach as followers
        release.set()               # release the leader
        for t in threads:
            t.join()
    finally:
        browser._scrape = saved

    assert scrapes[0] == 1, f"single-flight must do ONE scrape, did {scrapes[0]}"
    assert all(r == {"url": "https://www.linkedin.com/in/same/", "shared": True} for r in results)
    stats = browser.runtime_stats()
    assert stats["cache_coalesced"] == 5  # 1 leader + 5 coalesced followers


def test_failure_is_not_cached_and_shared_with_followers():
    """A scrape that raises must NOT be cached, so a later request re-scrapes
    instead of serving a frozen failure. (Followers that arrive WHILE the leader
    is in-flight share its error — that path is covered by the single-flight test
    with a held-open scrape; here we assert the cache contract directly.)"""
    attempts = [0]

    def flaky(impl, url):
        attempts[0] += 1
        raise browser.ScrapeDeadlineError("boom")

    saved = _stub(flaky)
    try:
        for _ in range(3):
            with pytest.raises(browser.ScrapeDeadlineError):
                browser.fetch_company_api("https://www.linkedin.com/company/x/")
    finally:
        browser._scrape = saved

    assert attempts[0] == 3, "failed scrape must never be cached → every call re-scrapes"
    assert len(browser._cache) == 0
    stats = browser.runtime_stats()
    assert stats["consecutive_failures"] == 3
    assert stats["last_scrape_ok"] is False


def test_reset_clears_cache():
    def scrape(impl, url):
        return {"url": url}

    saved = _stub(scrape)
    try:
        browser.fetch_html("https://www.linkedin.com/in/x/")
        assert len(browser._cache) == 1
        browser._cache_clear()  # reset_login calls this
        assert len(browser._cache) == 0
        # next fetch must re-scrape (nothing cached)
        n_before = browser.runtime_stats()["cache_misses"]
        browser.fetch_html("https://www.linkedin.com/in/x/")
        assert browser.runtime_stats()["cache_misses"] == n_before + 1
    finally:
        browser._scrape = saved


def test_cache_size_is_bounded():
    saved = _stub(lambda impl, url: {"u": url})
    # shrink the cap so the test is fast
    orig_max = browser._CACHE_MAX
    browser._CACHE_MAX = 3
    try:
        for i in range(10):
            browser.fetch_html(f"https://www.linkedin.com/in/p{i}/")
        assert len(browser._cache) <= 3, "cache must respect the size cap"
    finally:
        browser._CACHE_MAX = orig_max
        browser._scrape = saved


# --- circuit breaker (#8) ---------------------------------------------------
# When the session dies, wall-hits pile up; after CB_THRESHOLD the breaker opens
# and new scrapes fail FAST (SessionDegradedError → 409) instead of each burning
# a full goto. Cached data still flows. A clean page resets it.

def _reset_breaker():
    with browser._state_lock:
        browser._wall_hits = 0
        browser._cb_open = False
        browser._cb_open_until = 0.0


def test_breaker_trips_after_threshold_wall_hits(monkeypatch):
    monkeypatch.setattr(browser, "start_login", lambda: {"state": "logging_in"})
    _reset_breaker()
    # below threshold: breaker stays closed
    for _ in range(browser._CB_THRESHOLD - 1):
        browser._record_wall("https://www.linkedin.com/authwall")
    assert browser.breaker_state()["open"] is False

    # at threshold: it opens (and would kick a background re-login)
    browser._record_wall("https://www.linkedin.com/authwall")
    assert browser.breaker_state()["open"] is True

    # now any new (uncached) scrape fast-fails
    browser._scrape = lambda impl, url: {"x": 1}  # would-be scrape
    try:
        with pytest.raises(browser.SessionDegradedError):
            browser.fetch_profile_api("https://www.linkedin.com/in/x/")
    finally:
        del browser._scrape
    _reset_breaker()


def test_breaker_resets_on_clean_page():
    _reset_breaker()
    for _ in range(browser._CB_THRESHOLD):
        browser._record_wall("https://www.linkedin.com/authwall")
    assert browser.breaker_state()["open"] is True

    browser._record_authed()  # a signed-in page
    assert browser.breaker_state()["open"] is False
    assert browser.breaker_state()["wall_hits"] == 0


def test_cached_data_flows_while_breaker_open():
    """The breaker blocks NEW scrapes but cached results still succeed — so a
    transient re-login barely affects callers."""
    _reset_breaker()
    calls = []
    browser._scrape = lambda impl, url: calls.append(url) or {"u": url}
    try:
        browser.fetch_html("https://www.linkedin.com/in/x/")  # fill cache
        # trip the breaker
        for _ in range(browser._CB_THRESHOLD):
            browser._record_wall("https://www.linkedin.com/authwall")
        assert browser.breaker_state()["open"] is True
        # same url → served from cache (no new scrape, no 409)
        r = browser.fetch_html("https://www.linkedin.com/in/x/")
        assert r == {"u": "https://www.linkedin.com/in/x/"}
        assert len(calls) == 1, "cache hit must not re-scrape even with breaker open"
        # a NEW url while breaker open → 409 fast-fail
        with pytest.raises(browser.SessionDegradedError):
            browser.fetch_html("https://www.linkedin.com/in/y/")
    finally:
        del browser._scrape
        _reset_breaker()
