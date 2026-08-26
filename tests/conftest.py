"""Test-wide isolation from the developer's own environment.

``settings.load_settings()`` reads ``.env`` into ``os.environ`` on its first
call. That is right for a real run and wrong for a test suite: ``.env`` is
gitignored, so its contents differ per machine, and because the load happens
once per *process* the first test to call ``load_settings()`` leaves those
variables set for every test that follows. Five tests asserting on SMTP
configuration passed alone and failed in the full suite for exactly that
reason - the failure depended on test order and on whether the machine running
them happened to have a ``.env`` at all.

So the suite never reads one. Tests that want ``.env`` behaviour pass an
explicit path to :func:`settings.load_env_file` (see
``test_env_file_loading.py``), which still works because that path argument
bypasses the default entirely.
"""

from __future__ import annotations

import logging

import pytest

import logger as logger_module
import settings as settings_module

#: Every variable ``.env.example`` documents. Cleared before each test so a
#: developer's real ``.env`` - or a shell that exported one of these - cannot
#: change what the suite asserts.
_ENV_VARS = (
    "EMAIL_ENABLED",
    "SCRAPER_EMAIL_TO",
    "SCRAPER_SMTP_DRY_RUN",
    "SCRAPER_SMTP_HOST",
    "SCRAPER_SMTP_PORT",
    "SCRAPER_SMTP_USER",
    "SCRAPER_SMTP_PASSWORD",
    "SCRAPER_SMTP_FROM",
    "SCRAPER_SMTP_USE_TLS",
)


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch, tmp_path):
    """Give every test a clean environment and no ``.env`` to read."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    # Point the default at a path that cannot exist, and reset the once-only
    # flag, so a load triggered mid-suite is a harmless no-op rather than a
    # read of the developer's file.
    monkeypatch.setattr(settings_module, "DEFAULT_ENV_PATH", tmp_path / "absent.env")
    monkeypatch.setattr(settings_module, "_env_loaded", False)
    yield


@pytest.fixture(autouse=True)
def never_touch_the_production_log(monkeypatch, tmp_path):
    """A test run must not write - or truncate - ``logs/scraper.log``.

    Since each run now opens the log with ``mode="w"``, a stray
    ``setup_logging()`` from a test would not merely add noise: it would
    *erase* the log of the run someone is currently diagnosing. So the path is
    redirected into ``tmp_path`` for every test, and the module's once-only
    flag is reset so the redirect actually takes effect.
    """
    real_setup = logger_module.setup_logging

    def redirected(log_file, level="INFO", quiet=True, **kwargs):
        return real_setup(tmp_path / "test-scraper.log", level, quiet=True, **kwargs)

    monkeypatch.setattr(logger_module, "_CONFIGURED", False)
    monkeypatch.setattr(logger_module, "setup_logging", redirected)
    yield
    # Release the temp file so Windows can delete tmp_path, and leave the
    # module ready for the next test to configure from scratch.
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            handler.close()
            root.removeHandler(handler)
    logger_module._CONFIGURED = False
