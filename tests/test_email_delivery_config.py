"""Getting the run's spreadsheet to a mailbox, without ever needing one to test.

The digest already renders and the dry-run mode already proves it renders. What
was missing is everything around that:

* **No off switch that survives a checked-in file.** ``notifications.email
  .enabled`` lived only in ``settings.yaml``, so "is this run allowed to mail a
  human" was answered by a file in git. An operator cloning the repo inherited
  whatever the last commit said.
* **The recipient was only in that same file**, so pointing a run at a different
  mailbox meant editing tracked config.
* **Attachment selection was ``[p for k, p in paths.items() if k == "xlsx"]``**,
  which silently mails a digest with no spreadsheet at all when the workbook
  could not be written - the one case where the attachment matters most.
* **TLS was inferred from the port number alone**, so a provider on 587 without
  STARTTLS, or on a non-standard port with it, had no way to say so.

Credentials still come from the environment only and are never logged; these
tests assert that too.
"""

import os
from pathlib import Path

import pytest

import notify
from notify import (
    ENV_ENABLED,
    ENV_FROM,
    ENV_TO,
    ENV_USE_TLS,
    Digest,
    EmailConfig,
    load_email_config,
    send_digest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Every variable this module reads, cleared before each test so a developer's
#: real SMTP settings can never leak into an assertion - or into a send.
_ENV_KEYS = (
    "SCRAPER_SMTP_HOST", "SCRAPER_SMTP_PORT", "SCRAPER_SMTP_USER",
    "SCRAPER_SMTP_PASSWORD", "SCRAPER_SMTP_DRY_RUN",
    ENV_ENABLED, ENV_TO, ENV_FROM, ENV_USE_TLS,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _section(**over):
    section = {"enabled": True, "to": "someone@example.test", "attach_spreadsheet": True}
    section.update(over)
    return section


def _credentials(monkeypatch):
    monkeypatch.setenv("SCRAPER_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SCRAPER_SMTP_USER", "sender@example.test")
    monkeypatch.setenv("SCRAPER_SMTP_PASSWORD", "not-a-real-password")


# --- the enable gate -------------------------------------------------------

def test_email_is_off_when_the_config_says_so(monkeypatch):
    _credentials(monkeypatch)
    assert load_email_config(_section(enabled=False)) is None


def test_email_enabled_false_in_the_environment_overrides_an_enabled_config(monkeypatch):
    """The switch an operator can reach without editing a tracked file."""
    _credentials(monkeypatch)
    monkeypatch.setenv(ENV_ENABLED, "false")

    assert load_email_config(_section(enabled=True)) is None, (
        f"{ENV_ENABLED}=false did not stop a config that says enabled"
    )


def test_email_enabled_true_in_the_environment_overrides_a_disabled_config(monkeypatch):
    _credentials(monkeypatch)
    monkeypatch.setenv(ENV_ENABLED, "1")

    config = load_email_config(_section(enabled=False))

    assert config is not None
    assert config.host == "smtp.example.test"


# --- recipient and sender --------------------------------------------------

def test_the_recipient_can_come_from_the_environment(monkeypatch):
    _credentials(monkeypatch)
    monkeypatch.setenv(ENV_TO, "you@example.com")

    config = load_email_config(_section(to="stale@example.test"))

    assert config.to == ["you@example.com"]


def test_several_recipients_can_be_given_as_one_variable(monkeypatch):
    _credentials(monkeypatch)
    monkeypatch.setenv(ENV_TO, "a@example.test, b@example.test")

    config = load_email_config(_section())

    assert config.to == ["a@example.test", "b@example.test"]


def test_the_sender_can_come_from_the_environment(monkeypatch):
    _credentials(monkeypatch)
    monkeypatch.setenv(ENV_FROM, "jobs@example.test")

    assert load_email_config(_section()).sender == "jobs@example.test"


def test_the_sender_still_falls_back_to_the_smtp_user(monkeypatch):
    _credentials(monkeypatch)
    assert load_email_config(_section()).sender == "sender@example.test"


# --- transport -------------------------------------------------------------

def test_tls_is_on_by_default(monkeypatch):
    _credentials(monkeypatch)
    assert load_email_config(_section()).use_tls is True


def test_tls_can_be_turned_off_explicitly(monkeypatch):
    _credentials(monkeypatch)
    monkeypatch.setenv(ENV_USE_TLS, "false")
    assert load_email_config(_section()).use_tls is False


def test_starttls_is_skipped_when_tls_is_off(monkeypatch):
    """Port 587 previously implied STARTTLS with no way to say otherwise."""
    calls: list[str] = []

    class FakeSMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, *a, **kw):
            calls.append("starttls")

        def login(self, *a, **kw):
            calls.append("login")

        def send_message(self, *a, **kw):
            calls.append("send")

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    config = EmailConfig(host="h", port=587, user="u", password="p",
                         to=["x@example.test"], use_tls=False)

    assert send_digest(config, Digest("s", "t", "<p>t</p>")) is True
    assert calls == ["login", "send"], f"unexpected SMTP sequence: {calls}"


# --- missing credentials are a skip, never a crash -------------------------

def test_missing_credentials_disable_the_send_rather_than_failing(monkeypatch, caplog):
    monkeypatch.setenv(ENV_ENABLED, "1")

    with caplog.at_level("WARNING"):
        assert load_email_config(_section()) is None

    assert "SCRAPER_SMTP_HOST" in caplog.text


def test_no_password_value_is_ever_logged(monkeypatch, caplog):
    _credentials(monkeypatch)
    monkeypatch.setenv(ENV_ENABLED, "1")

    with caplog.at_level("DEBUG"):
        config = load_email_config(_section())

    assert "not-a-real-password" not in caplog.text
    assert "not-a-real-password" not in repr(config)


def test_a_dry_run_needs_no_credentials(monkeypatch, tmp_path):
    """So the whole digest path stays verifiable without a real mailbox."""
    monkeypatch.setenv(ENV_ENABLED, "1")
    monkeypatch.setenv("SCRAPER_SMTP_DRY_RUN", "1")

    config = load_email_config(_section(preview_dir=str(tmp_path)))

    assert config is not None and config.dry_run is True
    assert send_digest(config, Digest("s", "t", "<p>t</p>")) is True
    assert (tmp_path / "digest.html").exists()


# --- attachment selection --------------------------------------------------

def test_the_spreadsheet_is_the_attachment(tmp_path):
    from pipeline import select_attachments

    xlsx, csv = tmp_path / "jobs.xlsx", tmp_path / "jobs.csv"
    xlsx.write_bytes(b"x")
    csv.write_text("a,b\n")

    assert select_attachments({"csv": csv, "xlsx": xlsx}, attach=True) == [xlsx]


def test_the_csv_stands_in_when_no_spreadsheet_was_written(tmp_path):
    """A digest with no attachment at all is the worst of the three outcomes."""
    from pipeline import select_attachments

    csv = tmp_path / "jobs.csv"
    csv.write_text("a,b\n")

    assert select_attachments({"csv": csv}, attach=True) == [csv]


def test_nothing_is_attached_when_the_config_says_not_to(tmp_path):
    from pipeline import select_attachments

    xlsx = tmp_path / "jobs.xlsx"
    xlsx.write_bytes(b"x")

    assert select_attachments({"xlsx": xlsx}, attach=False) == []


def test_a_path_that_was_never_written_is_not_offered_as_an_attachment(tmp_path):
    from pipeline import select_attachments

    assert select_attachments({"xlsx": tmp_path / "missing.xlsx"}, attach=True) == []


# --- the example configuration ---------------------------------------------

def test_the_example_env_file_exists_and_disables_email_by_default():
    example = PROJECT_ROOT / ".env.example"
    assert example.exists(), ".env.example is the documented way to configure a send"

    text = example.read_text(encoding="utf-8")
    assert "EMAIL_ENABLED=false" in text, "the example must not arrive armed"
    assert "you@example.com" in text
    for key in ("SCRAPER_SMTP_HOST", "SCRAPER_SMTP_USER",
                "SCRAPER_SMTP_PASSWORD", "SCRAPER_EMAIL_TO"):
        assert key in text, f"{key} is undocumented"


def test_the_example_env_file_carries_no_secret_value():
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith(("SCRAPER_SMTP_PASSWORD", "SCRAPER_SMTP_USER")):
            _, _, value = line.partition("=")
            assert not value.strip(), f"{line!r} ships a value"


def test_the_env_example_is_not_shadowed_by_a_committed_env_file():
    """A real .env would carry credentials; it must never be in the tree."""
    assert not (PROJECT_ROOT / ".env").exists() or ".env" in (
        PROJECT_ROOT / ".gitignore"
    ).read_text(encoding="utf-8")
