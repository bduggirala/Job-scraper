"""Notification bookkeeping: announce once, but announce every change.

Two problems, opposite in direction.

The notifications table is keyed ``(job_id, kind)`` with ``kind`` being the
literal string "changed". A posting that moves city in March and is retitled in
June therefore produces one alert ever - the June change is filtered out as
already announced, permanently. "Announce each job once per kind" was meant to
stop a digest repeating itself, not to cap a job at one change for its
lifetime.

And there was no way to exercise any of this without real SMTP credentials
pointed at a real inbox, so the digest path could only ever be verified up to
the handoff. A dry run renders and records nothing, which is what makes the
rest of this file possible.
"""

import pytest

from database import JobDatabase
from notify import Digest, EmailConfig, load_email_config, send_digest


@pytest.fixture
def db(tmp_path):
    with JobDatabase(tmp_path / "jobs.db") as database:
        yield database


def _job(job_id="acme:1", title="Data Engineer", location="Dallas, TX"):
    return {"job_id": job_id, "company": "Acme", "title": title,
            "location": location, "job_url": "https://acme.test/jobs/1"}


# --- announce once ---------------------------------------------------------

def test_a_new_job_is_announced_once(db):
    job = _job()
    assert db.filter_unnotified([job], kind="new") == [job]
    db.record_notified([job], kind="new")
    assert db.filter_unnotified([job], kind="new") == []


def test_a_job_announced_as_new_can_still_be_announced_when_it_changes(db):
    job = _job()
    db.record_notified([job], kind="new")
    assert db.filter_unnotified([job], kind="changed") == [job]


# --- but announce every change ---------------------------------------------

def test_a_second_distinct_change_is_announced_again(db):
    """The bug: one 'changed' key per job, so change two was silent forever."""
    first = _job(location="Dallas, TX")
    db.record_notified([first], kind="changed")
    assert db.filter_unnotified([first], kind="changed") == []

    second = _job(location="Plano, TX")          # same job, moved again
    assert db.filter_unnotified([second], kind="changed") == [second], (
        "a job that changed twice was only ever announced once"
    )


def test_the_same_change_reported_twice_is_not_announced_twice(db):
    """Re-running a run must not re-announce an identical change."""
    job = _job(title="Senior Data Engineer")
    db.record_notified([job], kind="changed")
    assert db.filter_unnotified([dict(job)], kind="changed") == []


def test_a_job_reverting_to_a_previously_announced_state_is_not_re_announced(db):
    """A title flapping A -> B -> A should not alert on every flap."""
    db.record_notified([_job(title="Data Engineer")], kind="changed")
    db.record_notified([_job(title="Data Engineer II")], kind="changed")
    assert db.filter_unnotified([_job(title="Data Engineer")], kind="changed") == []


# --- untrusted content in the digest ---------------------------------------

def test_scraped_text_cannot_inject_markup_into_the_digest():
    from notify import build_digest

    digest = build_digest([{
        "company": "Acme",
        "title": "<img src=x onerror=alert(1)>",
        "location": "</td><script>alert('xss')</script>",
        "job_url": "https://acme.test/jobs/1",
    }], [], {})

    assert "<script>" not in digest.html
    assert "<img src=x" not in digest.html
    assert "&lt;img src=x" in digest.html, "the text should still be readable"


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "vbscript:msgbox(1)",
    " javascript:alert(1)",
])
def test_a_dangerous_link_scheme_is_not_emitted_as_an_href(url):
    """``job_url`` comes from a third-party page and lands in an ``href``.

    Escaping made the *text* safe but left the scheme alone, so a scraped
    ``javascript:`` URL stayed clickable - and the dry-run preview is an HTML
    file opened in a browser, where that executes.
    """
    from notify import build_digest

    digest = build_digest(
        [{"company": "Acme", "title": "Data Engineer", "job_url": url}], [], {})

    assert "javascript:" not in digest.html.lower()
    assert "vbscript:" not in digest.html.lower()
    assert "data:text/html" not in digest.html.lower()


@pytest.mark.parametrize("url", [
    "https://acme.test/jobs/1",
    "http://acme.test/jobs/1",
    "https://acme.test/jobs?job=1&x=2",
])
def test_an_ordinary_link_still_works(url):
    from notify import build_digest

    digest = build_digest(
        [{"company": "Acme", "title": "Data Engineer", "job_url": url}], [], {})
    assert url.replace("&", "&amp;") in digest.html


# --- dry run ---------------------------------------------------------------

def test_a_dry_run_config_is_built_without_smtp_credentials(monkeypatch):
    """The point: verifiable without credentials pointed at a real inbox."""
    for var in ("SCRAPER_SMTP_HOST", "SCRAPER_SMTP_USER", "SCRAPER_SMTP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    config = load_email_config({"enabled": True, "to": "someone@example.com",
                                "dry_run": True})

    assert config is not None, "a dry run still needs no credentials"
    assert config.dry_run is True


def test_a_dry_run_writes_the_digest_instead_of_sending_it(monkeypatch, tmp_path):
    import smtplib

    def _forbidden(*args, **kwargs):
        raise AssertionError("a dry run must not open an SMTP connection")

    monkeypatch.setattr(smtplib, "SMTP", _forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _forbidden)

    config = EmailConfig(host="", port=587, user="", password="",
                         to=["someone@example.com"], dry_run=True,
                         preview_dir=tmp_path)
    digest = Digest("Subject line", "plain body", "<p>html body</p>")

    assert send_digest(config, digest) is True

    written = sorted(p.name for p in tmp_path.iterdir())
    assert written, "the dry run left nothing to inspect"
    combined = "".join(p.read_text(encoding="utf-8") for p in tmp_path.iterdir())
    assert "plain body" in combined and "html body" in combined


def test_the_environment_can_force_a_dry_run(monkeypatch):
    """A safety switch that does not require editing checked-in config."""
    monkeypatch.setenv("SCRAPER_SMTP_DRY_RUN", "1")
    monkeypatch.setenv("SCRAPER_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SCRAPER_SMTP_USER", "me@example.com")
    monkeypatch.setenv("SCRAPER_SMTP_PASSWORD", "hunter2")

    config = load_email_config({"enabled": True, "to": "someone@example.com"})

    assert config is not None and config.dry_run is True


def test_a_real_send_is_still_the_default(monkeypatch):
    monkeypatch.delenv("SCRAPER_SMTP_DRY_RUN", raising=False)
    monkeypatch.setenv("SCRAPER_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SCRAPER_SMTP_USER", "me@example.com")
    monkeypatch.setenv("SCRAPER_SMTP_PASSWORD", "hunter2")

    config = load_email_config({"enabled": True, "to": "someone@example.com"})

    assert config is not None and config.dry_run is False
