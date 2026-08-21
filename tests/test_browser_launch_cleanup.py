"""A failed Chromium launch must not leak the started Playwright driver.

Regression test for the cascade seen in the canary: when ``chromium.launch()``
fails (e.g. missing OS libs), ``_get_browser`` used to leave the Playwright
driver it had just started running but unreferenced. Its event loop then
lingered on the thread, so the *next* ``_get_browser`` on that thread raised
"Sync API inside the asyncio loop" instead of retrying cleanly. One launch
failure thereby poisoned a worker for the rest of the run.
"""

import pytest

import browser.playwright_scraper as ps


class _FakeChromium:
    def launch(self, **kwargs):
        raise RuntimeError("cannot launch: missing shared library libatk-1.0.so.0")


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakeStealthManager:
    """Stands in for the playwright-stealth context manager."""

    def __init__(self, playwright):
        self._playwright = playwright
        self.exited = False

    def __exit__(self, *exc_info):
        self.exited = True
        self._playwright.stop()


def _clear_thread_local():
    for attr in ("browser", "playwright", "manager"):
        setattr(ps._thread_local, attr, None)


def test_failed_launch_tears_down_started_playwright(monkeypatch):
    _clear_thread_local()
    created = []

    def fake_start(use_stealth):
        pw = _FakePlaywright()
        manager = _FakeStealthManager(pw)
        created.append((pw, manager))
        return pw, manager

    monkeypatch.setattr(ps, "_start_playwright", fake_start)

    with pytest.raises(RuntimeError):
        ps._get_browser()

    pw, manager = created[0]
    assert manager.exited, "stealth manager was not exited after a failed launch"
    assert pw.stopped, "playwright driver was not stopped after a failed launch"
    # The dead instance must not be retained on the thread.
    assert getattr(ps._thread_local, "playwright", None) is None
    assert getattr(ps._thread_local, "browser", None) is None


def test_failed_launch_without_stealth_stops_playwright(monkeypatch):
    _clear_thread_local()
    created = []

    def fake_start(use_stealth):
        pw = _FakePlaywright()
        created.append(pw)
        return pw, None  # no stealth manager

    monkeypatch.setattr(ps, "_start_playwright", fake_start)

    with pytest.raises(RuntimeError):
        ps._get_browser()

    assert created[0].stopped, "playwright driver was not stopped after a failed launch"
