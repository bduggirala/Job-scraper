"""``.env`` is read into the environment, and never over an explicit value.

Credentials and the digest recipient are read from the environment rather than
from ``config/settings.yaml``, because that file is in git and an SMTP password
must never be able to land in it. ``.env`` is the gitignored companion that
holds the real values - but nothing read it, so every variable ``.env.example``
documents had to be exported by hand before it took effect.

That gap had teeth: the tracked config carries ``to: you@example.com`` as a
placeholder and the real recipient lives in ``.env``, so an unread ``.env``
meant the digest was addressed to the placeholder.
"""

from __future__ import annotations

import os

import pytest

import settings as settings_module
from settings import load_env_file


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Clear the extra keys these tests invent.

    The suite-wide ``isolate_environment`` fixture in ``conftest.py`` already
    clears everything ``.env.example`` documents and stops the real ``.env``
    from being read.
    """
    for key in ("QUOTED", "SPACED", "EXPORTED", "EMPTY"):
        monkeypatch.delenv(key, raising=False)


def _write(tmp_path, body: str):
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_values_reach_the_environment(tmp_path):
    path = _write(tmp_path, "SCRAPER_EMAIL_TO=digest-recipient@example.com\nEMAIL_ENABLED=false\n")
    applied = load_env_file(path)

    assert applied["SCRAPER_EMAIL_TO"] == "digest-recipient@example.com"
    assert os.environ["SCRAPER_EMAIL_TO"] == "digest-recipient@example.com"
    assert os.environ["EMAIL_ENABLED"] == "false"


def test_an_existing_variable_always_wins(tmp_path, monkeypatch):
    """An operator who exported something was more deliberate than a file."""
    monkeypatch.setenv("SCRAPER_EMAIL_TO", "someone-else@example.com")
    path = _write(tmp_path, "SCRAPER_EMAIL_TO=from-the-file@example.com\n")

    applied = load_env_file(path)

    assert "SCRAPER_EMAIL_TO" not in applied
    assert os.environ["SCRAPER_EMAIL_TO"] == "someone-else@example.com"


def test_comments_and_blank_lines_are_ignored(tmp_path):
    path = _write(tmp_path, "\n# a comment\n\n  # indented comment\nEMAIL_ENABLED=true\n")
    applied = load_env_file(path)
    assert applied == {"EMAIL_ENABLED": "true"}


def test_quotes_are_stripped(tmp_path):
    path = _write(tmp_path, "QUOTED=\"quoted value\"\nSPACED = spaced \n")
    applied = load_env_file(path)
    assert applied["QUOTED"] == "quoted value"
    assert applied["SPACED"] == "spaced"


def test_export_prefix_is_tolerated(tmp_path):
    """The same file is often sourced by a shell."""
    path = _write(tmp_path, "export EXPORTED=yes\n")
    assert load_env_file(path)["EXPORTED"] == "yes"


def test_an_empty_value_is_kept(tmp_path):
    """A blank credential is 'explicitly unset', which the sender reports."""
    path = _write(tmp_path, "EMPTY=\n")
    applied = load_env_file(path)
    assert applied["EMPTY"] == ""
    assert os.environ["EMPTY"] == ""


def test_a_missing_file_is_not_an_error(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == {}


def test_a_malformed_line_is_skipped_not_fatal(tmp_path):
    path = _write(tmp_path, "this line has no equals sign\nEMAIL_ENABLED=true\n")
    assert load_env_file(path) == {"EMAIL_ENABLED": "true"}


def test_it_only_loads_once_per_process(tmp_path):
    path = _write(tmp_path, "EMAIL_ENABLED=true\n")
    assert load_env_file(path) == {"EMAIL_ENABLED": "true"}

    other = tmp_path / "second.env"
    other.write_text("SCRAPER_EMAIL_TO=second@example.com\n", encoding="utf-8")
    assert load_env_file(other) == {}, "a second call is a no-op"
    assert "SCRAPER_EMAIL_TO" not in os.environ

    assert load_env_file(other, force=True)["SCRAPER_EMAIL_TO"] == "second@example.com"


def test_load_settings_loads_the_env_file(tmp_path, monkeypatch):
    """Every entry point gets it without having to remember to ask."""
    called: list[bool] = []
    monkeypatch.setattr(
        settings_module, "load_env_file", lambda *a, **k: called.append(True) or {},
    )
    settings_module.load_settings()
    assert called, "load_settings() must load .env"


def test_the_digest_recipient_resolves_from_the_env_file(tmp_path):
    """End to end: an unread .env is what addressed the digest to a placeholder."""
    from notify import load_email_config

    path = _write(tmp_path, "EMAIL_ENABLED=true\nSCRAPER_EMAIL_TO=real@example.com\n"
                            "SCRAPER_SMTP_DRY_RUN=true\nSCRAPER_SMTP_HOST=smtp.example.invalid\n"
                            "SCRAPER_SMTP_USER=u\nSCRAPER_SMTP_PASSWORD=p\n")
    load_env_file(path)

    config = load_email_config({"enabled": False, "to": "you@example.com"})

    assert config is not None, "EMAIL_ENABLED in .env overrides the tracked config"
    assert config.to == ["real@example.com"]
    assert config.dry_run is True
