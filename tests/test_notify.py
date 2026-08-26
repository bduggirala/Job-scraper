"""Email digest: what it says, and the guards on when it says anything.

Three rules matter more than the formatting:

* nothing is sent when there is nothing new - a channel that mails you "0 new
  jobs" every run is a channel you stop reading;
* nothing is sent from an incomplete run, because a truncated scrape's "new"
  and "removed" sets are both untrustworthy;
* credentials come from the environment, never from settings.yaml.
"""



from notify import (
    EmailConfig,
    build_digest,
    load_email_config,
    should_send,
)


def _job(title="Senior Data Engineer", company="Acme", **kw):
    row = {
        "job_id": f"{company}:{title}", "company": company, "title": title,
        "location": "Plano, TX", "job_url": "https://x.test/1",
        "apply_url": None, "date_posted": "2026-08-24T00:00:00+00:00",
        "date_filter_status": "within_window", "remote_scope": "onsite",
        "location_match_type": "dfw", "is_new": True,
    }
    row.update(kw)
    return row


# --- when to send ----------------------------------------------------------

def test_nothing_is_sent_when_there_is_nothing_new():
    assert should_send(new_jobs=[], changed_jobs=[], run_complete=True) is False


def test_a_digest_is_sent_when_there_are_new_jobs():
    assert should_send(new_jobs=[_job()], changed_jobs=[], run_complete=True) is True


def test_a_digest_is_sent_for_changes_alone():
    assert should_send(new_jobs=[], changed_jobs=[_job()], run_complete=True) is True


def test_nothing_is_sent_from_an_incomplete_run():
    """A truncated scrape cannot be trusted to know what is new."""
    assert should_send(new_jobs=[_job()], changed_jobs=[], run_complete=False) is False


# --- what it says ----------------------------------------------------------

def test_the_digest_names_every_new_job():
    body = build_digest([_job(title="Senior Data Engineer")], [], {})
    assert "Senior Data Engineer" in body.text
    assert "Senior Data Engineer" in body.html


def test_the_digest_carries_the_apply_link():
    body = build_digest([_job(apply_url="https://x.test/apply/1")], [], {})
    assert "https://x.test/apply/1" in body.text


def test_the_digest_falls_back_to_the_posting_url_when_no_apply_link():
    body = build_digest([_job(job_url="https://x.test/job/9", apply_url=None)], [], {})
    assert "https://x.test/job/9" in body.text


def test_changed_jobs_say_what_changed():
    changed = [_job(title="Staff Data Engineer", changed_fields=["title"])]
    body = build_digest([], changed, {})
    assert "title" in body.text.lower()


def test_the_subject_counts_what_is_inside():
    body = build_digest([_job(), _job(title="Data Platform Engineer")], [], {})
    assert "2" in body.subject


def test_html_special_characters_in_a_scraped_title_are_escaped():
    """Titles come from third-party pages and land in an HTML email."""
    body = build_digest([_job(title='DE <script>alert(1)</script>')], [], {})
    assert "<script>" not in body.html
    assert "&lt;script&gt;" in body.html


# --- credentials -----------------------------------------------------------

def test_credentials_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("SCRAPER_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SCRAPER_SMTP_USER", "bot@example.com")
    monkeypatch.setenv("SCRAPER_SMTP_PASSWORD", "s3cret")

    cfg = load_email_config({"enabled": True, "to": "me@example.com"})

    assert isinstance(cfg, EmailConfig)
    assert cfg.host == "smtp.example.com"
    assert cfg.password == "s3cret"
    assert cfg.to == ["me@example.com"]


def test_a_missing_password_disables_sending_rather_than_crashing(monkeypatch):
    monkeypatch.delenv("SCRAPER_SMTP_PASSWORD", raising=False)
    monkeypatch.setenv("SCRAPER_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SCRAPER_SMTP_USER", "bot@example.com")

    cfg = load_email_config({"enabled": True, "to": "me@example.com"})

    assert cfg is None


def test_a_password_is_never_included_in_the_repr(monkeypatch):
    cfg = EmailConfig(
        host="smtp.example.com", port=587, user="bot@example.com",
        password="s3cret", to=["me@example.com"], sender="bot@example.com",
    )
    assert "s3cret" not in repr(cfg)
