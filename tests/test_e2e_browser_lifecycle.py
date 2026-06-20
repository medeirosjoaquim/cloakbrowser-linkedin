"""Real end-to-end tests for the browser lifecycle / OOM defense.

These launch REAL cloakbrowser (actual OS chromium processes) — NO fakes, NO
LinkedIn, NO credentials, NO network (about:blank only). They prove the core
guarantee this app was missing: every Chromium we launch is SIGKILL'd on every
abandon path, so a burst of work / deadline breaches / a shutdown can NEVER
strand orphaned chromium processes (the cause of the OOM-thrash).

Marked `e2e` so the default fast suite skips them (they launch real browsers,
~10-15s total). Unlike tests/test_e2e.py they do NOT need LinkedIn creds — they
exercise the lifecycle, not the scrape.

    venv/bin/python -m pytest -m e2e tests/test_e2e_browser_lifecycle.py -v

Self-skips on non-Linux (no /proc) or if the cloakbrowser binary isn't installed.
"""
import os
import shutil
import threading
import time

import pytest

import browser

CB_BIN = browser.cb.binary_info()["binary_path"]
_HAS_PROC = os.path.isdir("/proc")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not _HAS_PROC, reason="orphan accounting needs Linux /proc"),
    pytest.mark.skipif(not browser.cb.binary_info().get("installed"),
                       reason="cloakbrowser binary not installed"),
]


def _read_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "ignore")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return ""


def _read_exe(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return ""


def _count_chromium_for(profile_dir: str) -> int:
    """Count live chromium procs whose --user-data-dir == profile_dir.

    Counting by the context's UNIQUE profile dir (a fresh mkdtemp per pool
    worker) is precise: it can't match desktop Chrome, other containers, or —
    critically — this very test process (whose cmdline never carries the path).
    """
    n = 0
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        if profile_dir not in _read_cmdline(pid):
            continue
        if "chrom" in _read_exe(pid).lower():
            n += 1
    return n


def _count_all_chromium() -> int:
    """Total live cloakbrowser chromium processes (by exe). Used to assert the
    WHOLE process tree is gone after a full shutdown."""
    n = 0
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        if _read_exe(int(name)) == CB_BIN:
            n += 1
    return n


# ---------------------------------------------------------------------------
# 1. The primitive: _reap_chromium kills a freshly launched real context.
# ---------------------------------------------------------------------------
def test_e2e_reap_chromium_kills_real_browser(tmp_path):
    d = str(tmp_path / "profile")
    ctx = browser.cb.launch_persistent_context(d, headless=True, args=browser._launch_args())
    try:
        time.sleep(1.5)  # let the full process tree come up
        assert _count_chromium_for(d) >= 1, "context didn't launch chromium"

        killed = browser._reap_chromium(d)
        time.sleep(1.0)  # let the SIGKILLs settle

        assert killed >= 1, "_reap_chromium reported no kills"
        assert _count_chromium_for(d) == 0, "orphan chromium survived the reap"
    finally:
        try:
            ctx.close()
        except Exception:
            pass
        browser._reap_chromium(d)  # belt-and-suspenders
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. The money test: a deadline breach on a real worker SIGKILLs its chromium.
#    This is the exact scenario that used to leak ~200MB per breach → OOM.
# ---------------------------------------------------------------------------
def test_e2e_worker_deadline_reaps_orphan():
    worker = browser._ScrapeWorker("cb-e2e-deadline")
    release = threading.Event()

    def hang():
        release.wait(30)  # simulate a wedged browser op (>> deadline)

    try:
        worker.run(lambda: None, deadline=30)  # warm: launches real chromium
        assert worker._profile_dir is not None
        assert _count_chromium_for(worker._profile_dir) >= 1, "worker didn't launch chromium"

        with pytest.raises(browser.ScrapeDeadlineError):
            worker.run(hang, deadline=2.0)
        release.set()  # free the lingering fn thread promptly
        time.sleep(1.0)

        assert worker._poisoned is True
        assert _count_chromium_for(worker._profile_dir) == 0, (
            "wedged chromium was NOT reaped on deadline → OOM leak regression"
        )
    finally:
        release.set()
        worker._hard_reap()
        if worker._profile_dir:
            shutil.rmtree(worker._profile_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. close() reaps a real browser even when the graceful ctx.close() hangs.
# ---------------------------------------------------------------------------
def test_e2e_worker_close_reaps_even_if_graceful_hangs():
    worker = browser._ScrapeWorker("cb-e2e-close")
    try:
        worker.run(lambda: None, deadline=30)  # launch real chromium
        d = worker._profile_dir
        assert d and _count_chromium_for(d) >= 1

        # occupy the worker thread so the graceful-close task can't run within
        # close()'s 5s budget — mimicking a wedged browser that hangs on close.
        block = threading.Event()

        def occupy():
            block.wait(15)
        worker._ex.submit(occupy)

        t0 = time.monotonic()
        worker.close()  # must NOT hang, and must still force-reap
        elapsed = time.monotonic() - t0
        block.set()

        assert elapsed < 12, f"close() hung for {elapsed:.1f}s"
        assert _count_chromium_for(d) == 0, "close() left an orphan despite a hung graceful close"
    finally:
        block.set()
        worker._hard_reap()
        if worker._profile_dir:
            shutil.rmtree(worker._profile_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. Full shutdown: _reap_all_chromium reaps login + every pool worker.
# ---------------------------------------------------------------------------
def test_e2e_reap_all_chromium_clears_pool_and_login(monkeypatch, tmp_path):
    # Isolate the login profile so we don't touch the dev ./profile session.
    login_dir = str(tmp_path / "login-profile")
    monkeypatch.setattr(browser, "PROFILE_DIR", login_dir)

    # Build a 2-worker pool of REAL browsers and assign it as the module pool.
    pool = browser._ScrapePool(2)
    prev_pool = browser._scrape_pool
    monkeypatch.setattr(browser, "scrape_stats", lambda: {"enabled": True, "size": 2, "in_flight": 0})
    monkeypatch.setattr(browser, "_scrape_pool", pool)
    try:
        # Launch the login context + both workers so there's something to reap.
        login_ctx = browser.cb.launch_persistent_context(
            login_dir, headless=True, viewport=browser._VIEWPORT, args=browser._launch_args(),
        )
        for w in pool._workers:
            w.run(lambda: None, deadline=30)
        time.sleep(0.5)

        before = _count_all_chromium()
        assert before >= 3, f"expected login + 2 workers alive, got {before}"

        browser._reap_all_chromium()  # SIGKILL everything by profile dir
        time.sleep(1.0)

        assert _count_all_chromium() == 0, "shutdown left orphan chromium behind"
    finally:
        browser._reap_all_chromium()
        try:
            login_ctx.close()
        except Exception:
            pass
        pool.close()
        monkeypatch.setattr(browser, "_scrape_pool", prev_pool)
        monkeypatch.setattr(browser, "_ctx", None)
        shutil.rmtree(login_dir, ignore_errors=True)
