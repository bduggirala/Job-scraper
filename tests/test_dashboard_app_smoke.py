"""Startup smoke test: the dashboard imports and its page renders.

Streamlit is an optional dependency (``requirements-dashboard.txt``), so these
skip rather than fail when it is absent - a scraper deployment that never
installs the dashboard must still have a green suite.
"""

from __future__ import annotations

import pytest

streamlit = pytest.importorskip("streamlit", reason="dashboard extras not installed")


def test_the_services_layer_imports_without_streamlit():
    """The UI is the only part allowed to depend on Streamlit."""
    import sys

    from dashboard import services

    assert services.MAIN_PY.name == "main.py"
    module = sys.modules["dashboard.services"]
    assert "streamlit" not in getattr(module, "__dict__", {})


def test_the_app_module_imports_cleanly():
    from dashboard import app

    assert callable(app.main)
    assert app.REFRESH_SECONDS > 0


def test_importing_the_app_does_not_run_it(monkeypatch):
    """Import must be side-effect free, or a test run would render a page."""
    import importlib

    from dashboard import app

    called = []
    monkeypatch.setattr(app, "main", lambda: called.append(True))
    importlib.reload(app)
    assert called == []


def test_the_deploy_button_is_configured_away():
    """Deploy publishes to a public cloud - the wrong action for this app.

    The cloud has none of this machine's files, could not run the scraper, and
    the page has no authentication precisely because only localhost reaches it.
    Streamlit 1.62 has no switch for the Deploy button alone, so "viewer" is
    the setting, and the page carries its own Refresh button in exchange.
    """
    import tomllib

    from settings import PROJECT_ROOT

    config = PROJECT_ROOT / ".streamlit" / "config.toml"
    assert config.exists(), "the dashboard relies on .streamlit/config.toml being present"
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["client"]["toolbarMode"] == "viewer"
    assert data["browser"]["gatherUsageStats"] is False


def test_both_tabs_render_against_the_real_project(tmp_path):
    """Drive the whole page through Streamlit's own test harness."""
    AppTest = pytest.importorskip(
        "streamlit.testing.v1", reason="streamlit test harness unavailable"
    ).AppTest

    from settings import PROJECT_ROOT

    harness = AppTest.from_file(str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=120)
    harness.run()

    assert not harness.exception, [str(exc) for exc in harness.exception]
    labels = [tab.label for tab in harness.tabs] if hasattr(harness, "tabs") else []
    if labels:
        assert "Run Scraper" in labels
        assert "Manage Companies" in labels
    assert any("Company ATS Job Scraper" in str(title.value) for title in harness.title)
    # The Run Scraper button is the tab's whole point; it must exist.
    assert any("Run Scraper" in str(button.label) for button in harness.button)
    # Refresh replaces the hidden built-in Rerun, so both tabs must offer one.
    assert sum(str(button.label) == "Refresh" for button in harness.button) == 2
