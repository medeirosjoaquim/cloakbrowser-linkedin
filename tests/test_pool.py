"""Unit tests for the opt-in scrape pool (SCRAPE_POOL_SIZE >= 2).

The default suite runs at pool size 1 (legacy single-worker path), so the pool
itself is untested there. These tests exercise it directly with stubbed workers
(no real browser) to prove: (1) N workers run concurrently, (2) overflow raises
ScrapeBusyError (→ HTTP 503), and (3) a worker re-seeds its context when the
cookie generation changes.
"""

import threading
import time

import pytest

import browser


# --- concurrency: two tasks overlap on a size-2 pool ------------------------

def test_pool_runs_concurrently(monkeypatch):
    # No real browser: _ensure just needs to set the thread-local ctx sentinel.
    monkeypatch.setattr(browser._ScrapeWorker, "_ensure", lambda self: None)
    pool = browser._ScrapePool(2)

    active = []
    lock = threading.Lock()
    max_seen = [0]

    def fn():
        with lock:
            active.append(1)
            max_seen[0] = max(max_seen[0], len(active))
        time.sleep(0.25)  # hold the slot so a 2nd task must run in parallel
        with lock:
            active.pop()
        return "ok"

    results = [None, None]

    def run(i):
        results[i] = pool.run(fn, timeout=5)

    ts = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert results == ["ok", "ok"]
    assert max_seen[0] == 2  # both ran at the same time → real parallelism


# --- backpressure: overflow → ScrapeBusyError --------------------------------

def test_pool_overflow_raises_scrape_busy(monkeypatch):
    monkeypatch.setattr(browser._ScrapeWorker, "_ensure", lambda self: None)
    pool = browser._ScrapePool(1)  # only one slot

    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return "done"

    bg_result = [None]

    def bg():
        bg_result[0] = pool.run(slow, timeout=5)

    t = threading.Thread(target=bg)
    t.start()
    assert started.wait(5), "slow task never started"

    # Pool is saturated → a 2nd request must give up fast (ScrapeBusyError),
    # NOT block forever. This is the change that turns pile-on into clean 503s.
    with pytest.raises(browser.ScrapeBusyError):
        pool.run(lambda: "x", timeout=0.2)

    release.set()
    t.join()
    assert bg_result[0] == "done"


# --- cookie re-seeding on generation change ----------------------------------

class _FakeCtx:
    def __init__(self):
        self.added = []
        self.cleared = 0

    def clear_cookies(self):
        self.cleared += 1

    def add_cookies(self, cookies):
        self.added.extend(cookies)

    def new_page(self):
        return None

    def close(self):
        pass


def test_worker_reseeds_when_session_changes(monkeypatch):
    fake = _FakeCtx()
    monkeypatch.setattr(
        browser.cb, "launch_persistent_context", lambda *a, **k: fake
    )
    # Seed the global cookie snapshot + gen (as _capture_cookies would).
    monkeypatch.setattr(browser, "_cookies", [{"name": "li_at", "value": "v1"}])
    monkeypatch.setattr(browser, "_cookie_gen", 1)

    worker = browser._ScrapeWorker("cb-scrape-test")
    worker.run(lambda: "first")

    assert fake.added == [{"name": "li_at", "value": "v1"}]
    assert fake.cleared == 1  # clears (a fresh, empty jar) then seeds

    # Session refreshed (re-login) → gen bumps, cookies change.
    monkeypatch.setattr(browser, "_cookies", [{"name": "li_at", "value": "v2"}])
    monkeypatch.setattr(browser, "_cookie_gen", 2)

    worker.run(lambda: "second")

    assert fake.cleared == 2  # stale cookies wiped before re-seed
    assert fake.added == [
        {"name": "li_at", "value": "v1"},
        {"name": "li_at", "value": "v2"},
    ]

    worker.close()


# --- scrape_stats reflects the pool ------------------------------------------

def test_scrape_stats_disabled_by_default(monkeypatch):
    monkeypatch.setattr(browser, "_scrape_pool", None)
    assert browser.scrape_stats() == {
        "enabled": False, "size": 1, "in_flight": 0, "queue_depth": 0,
    }


# --- hard deadline: a wedged worker is bounded (→ ScrapeDeadlineError) -------
# This is the guard that prevents the 46-minute unbounded hang: a worker whose
# browser op never returns still releases its caller in `deadline` seconds.

def test_worker_deadline_raises_and_poisons(monkeypatch):
    monkeypatch.setattr(browser._ScrapeWorker, "_ensure", lambda self: None)
    worker = browser._ScrapeWorker("cb-scrape-deadline")

    started = threading.Event()

    def hangs_forever():
        started.set()
        time.sleep(30)  # simulate a wedged browser op (>> deadline)
        return "never"

    with pytest.raises(browser.ScrapeDeadlineError):
        worker.run(hangs_forever, deadline=0.5)

    assert started.wait(5)            # the task did start
    assert worker._poisoned is True   # and is now flagged for replacement
    worker._ex.shutdown(wait=False, cancel_futures=True)


def test_pool_replaces_poisoned_worker(monkeypatch):
    """A worker that blew its deadline is swapped for a fresh one on next acquire,
    so the pool recovers its capacity instead of staying wedged forever."""
    monkeypatch.setattr(browser._ScrapeWorker, "_ensure", lambda self: None)
    pool = browser._ScrapePool(1)

    started = threading.Event()

    def hangs():
        started.set()
        time.sleep(30)

    with pytest.raises(browser.ScrapeDeadlineError):
        pool.run(hangs, timeout=5, deadline=0.5)
    assert started.wait(5)

    # The slot's worker is poisoned → the NEXT run must hand out a fresh worker
    # (not the wedged one) and return normally.
    assert pool.run(lambda: "recovered", timeout=5, deadline=5) == "recovered"


# --- OOM defense: a wedged browser is SIGKILL'd, never orphaned -------------
# This is the regression guard for the OOM leak: when a worker blows its
# deadline it must force-kill its Chromium (by profile dir) so a burst of
# breaches can't pile up orphaned ~200MB processes. verify_reap.py proves this
# against a REAL browser end-to-end; here we just assert the kill is invoked.

def test_deadline_breach_reaps_chromium(monkeypatch):
    monkeypatch.setattr(browser._ScrapeWorker, "_ensure", lambda self: None)
    reaped = []
    monkeypatch.setattr(browser, "_reap_chromium", lambda d: reaped.append(d) or 0)
    worker = browser._ScrapeWorker("cb-reap-test")
    worker._profile_dir = "/tmp/cb-scrape-fake-xyz"

    def hangs():
        time.sleep(30)

    with pytest.raises(browser.ScrapeDeadlineError):
        worker.run(hangs, deadline=0.4)

    assert reaped == ["/tmp/cb-scrape-fake-xyz"]  # chromium SIGKILL'd on deadline
    assert worker._poisoned is True


def test_close_always_reaps_even_if_graceful_hangs(monkeypatch):
    """close() must force-reap the Chromium even when the graceful ctx.close()
    hangs (the scenario that used to strand orphans)."""
    monkeypatch.setattr(browser._ScrapeWorker, "_ensure", lambda self: None)
    reaped = []
    monkeypatch.setattr(browser, "_reap_chromium", lambda d: reaped.append(d) or 0)
    worker = browser._ScrapeWorker("cb-close-test")
    worker._profile_dir = "/tmp/cb-scrape-fake-close"

    # graceful close task submitted to the worker's executor hangs forever
    import time as _t
    def hanging_close():
        _t.sleep(30)
    # occupy the worker thread so the graceful close can't run within the timeout
    worker._ex.submit(lambda: _t.sleep(30))
    worker.close()  # must not hang, and must still reap

    assert "/tmp/cb-scrape-fake-close" in reaped  # reaped despite the hang
