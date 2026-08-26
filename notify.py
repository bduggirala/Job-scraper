"""Email digest for newly discovered and changed jobs.

Four rules shape this module, and they matter more than the formatting:

* **Delivery is opt-in.** ``EMAIL_ENABLED`` gates every send and overrides the
  config file in both directions. "May this run mail a human" is not a question
  a file in git should answer on its own.
* **Nothing is sent when there is nothing to say.** A channel that mails
  "0 new jobs" every run is one you stop opening, and then it is worse than no
  channel at all.
* **Nothing is sent from a run whose gaps have unknown shape.** A failed page
  or a provider contradicting its own total leaves a hole we cannot describe,
  so the "new" set is untrustworthy for the same reason the "removed" set is.
  A ceiling *we* set on a newest-first walk is different: the gap is the oldest
  postings, and suppressing on it would mean permanent silence.
* **Credentials come from the environment only.** ``settings.yaml`` is checked
  into the repository; an SMTP password must never be able to land there.

Sending is deliberately best-effort: a mail failure is logged and the run still
succeeds, because the CSV on disk is the actual deliverable and losing it to a
transient SMTP error would be a poor trade.
"""

from __future__ import annotations

import html
import os
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable

from logger import get_logger

log = get_logger("notify")

#: Environment variables holding the SMTP credentials.
ENV_HOST = "SCRAPER_SMTP_HOST"
ENV_PORT = "SCRAPER_SMTP_PORT"
ENV_USER = "SCRAPER_SMTP_USER"
ENV_PASSWORD = "SCRAPER_SMTP_PASSWORD"
#: Set to 1/true/yes to render the digest to disk instead of sending it. A
#: safety switch that needs no edit to checked-in config, so a verification run
#: cannot mail a real recipient by accident.
ENV_DRY_RUN = "SCRAPER_SMTP_DRY_RUN"

#: The master switch. ``settings.yaml`` is checked into git, so "may this run
#: mail a human" was previously answered by whatever the last commit said.
#: Set explicitly, this wins in both directions.
ENV_ENABLED = "EMAIL_ENABLED"
#: Recipient(s), comma- or semicolon-separated. Overrides the config file so a
#: run can be pointed at a different mailbox without editing tracked config.
ENV_TO = "SCRAPER_EMAIL_TO"
#: Envelope sender, when it differs from the authenticating account.
ENV_FROM = "SCRAPER_SMTP_FROM"
#: STARTTLS on a non-SSL port. Defaults on; set false only for a relay that
#: does not offer it (a local MTA on 25, typically).
ENV_USE_TLS = "SCRAPER_SMTP_USE_TLS"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _env_flag(name: str) -> bool | None:
    """A tri-state environment flag: True, False, or "not set"."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = raw.strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    log.warning("%s=%r is not a boolean; ignoring it", name, raw)
    return None


def _split_recipients(value: Any) -> list[str]:
    """Recipients from a string (comma/semicolon separated) or a list."""
    if isinstance(value, str):
        parts: Iterable[Any] = re.split(r"[,;]", value)
    else:
        parts = value or []
    return [str(part).strip() for part in parts if str(part).strip()]


@dataclass
class EmailConfig:
    """Where and how to send. The password never appears in ``repr``."""

    host: str
    port: int
    user: str
    password: str = field(repr=False)
    to: list[str] = field(default_factory=list)
    sender: str = ""
    #: Render the digest to ``preview_dir`` and report success without opening
    #: an SMTP connection. Makes the whole notification path testable without
    #: credentials aimed at a real inbox.
    dry_run: bool = False
    #: Where a dry run writes its preview.
    preview_dir: Path | None = None
    #: Issue STARTTLS on a non-SSL port. Port 465 is implicit SSL and ignores
    #: this; every other port previously assumed STARTTLS with no way to say
    #: otherwise.
    use_tls: bool = True

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"EmailConfig(host={self.host!r}, port={self.port}, "
            f"user={self.user!r}, to={self.to!r}, dry_run={self.dry_run}, "
            f"use_tls={self.use_tls}, password=<hidden>)"
        )


@dataclass
class Digest:
    subject: str
    text: str
    html: str


def load_email_config(section: dict[str, Any] | None) -> EmailConfig | None:
    """Build an :class:`EmailConfig`, or None when sending is not possible.

    Returns None rather than raising when the section is disabled, has no
    recipient, or the environment is missing credentials - a misconfigured
    mailer must not fail a scrape that otherwise worked.
    """
    section = section or {}

    # The environment wins in both directions: it is the only switch an
    # operator has that does not mean editing a file tracked in git.
    enabled = _env_flag(ENV_ENABLED)
    if enabled is None:
        enabled = bool(section.get("enabled"))
    if not enabled:
        log.info("Email delivery is disabled (%s unset or false); the run's "
                 "outputs are still written to disk", ENV_ENABLED)
        return None

    recipients = _split_recipients(os.environ.get(ENV_TO)) or \
        _split_recipients(section.get("to"))
    if not recipients:
        log.warning("Email notifications enabled but no recipient configured "
                    "(set %s or notifications.email.to)", ENV_TO)
        return None

    host = os.environ.get(ENV_HOST) or section.get("host")
    user = os.environ.get(ENV_USER) or section.get("user")
    password = os.environ.get(ENV_PASSWORD)

    dry_run = (
        os.environ.get(ENV_DRY_RUN, "").strip().lower() in _TRUTHY
        or bool(section.get("dry_run"))
    )

    # A dry run never opens a connection, so requiring credentials for one
    # would leave the digest verifiable only by mailing a real person.
    if not dry_run:
        missing = [
            name for name, value in
            ((ENV_HOST, host), (ENV_USER, user), (ENV_PASSWORD, password))
            if not value
        ]
        if missing:
            log.warning(
                "Email notifications enabled but %s not set; skipping send. "
                "Credentials are read from the environment only.", ", ".join(missing),
            )
            return None

    use_tls = _env_flag(ENV_USE_TLS)
    if use_tls is None:
        use_tls = bool(section.get("use_tls", True))

    preview = section.get("preview_dir")
    return EmailConfig(
        host=str(host or ""),
        port=int(os.environ.get(ENV_PORT) or section.get("port") or 587),
        user=str(user or ""),
        password=str(password or ""),
        to=recipients,
        sender=str(
            os.environ.get(ENV_FROM) or section.get("from") or user
            or "scraper@localhost"
        ),
        dry_run=dry_run,
        preview_dir=Path(preview) if preview else None,
        use_tls=use_tls,
    )


def should_send(
    *,
    new_jobs: list[dict],
    changed_jobs: list[dict],
    run_complete: bool,
    stop_reasons: set[str] | None = None,
) -> bool:
    """Whether this run has something worth mailing.

    Incompleteness comes in two kinds and they deserve opposite treatment.

    A **failed page** leaves a hole of unknown shape: what looks new may just
    be what we happened to reach this time, so the digest is suppressed.

    A **ceiling we imposed ourselves** leaves a hole we can describe. Both the
    job budget and the 500-page ceiling stop a newest-first walk, so what was
    missed is the oldest postings, and nothing inside a 7-day freshness window
    sits behind it. Treating that as fatal would mean permanent silence for any
    employer too large to collect in full - CVS Health and Signify Health each
    list 19,254 postings against a provider serving ten per request, so both
    trip the page ceiling on every run and always will. One known limitation
    should not cost all alerting.

    ``page_ceiling`` was added to the vocabulary after this rule was written and
    was not added to it, which is how a full-workbook run came to log
    "suppressing the digest" with those two companies as the only reason.

    A provider **contradicting its own reported total** stays fatal, alongside
    a failed page: it said 679 and served 140, and nothing says which 539 are
    missing or whether they are old.

    ``stop_reasons`` omitted keeps the strict behaviour, so a caller that has
    not been taught the distinction stays cautious.
    """
    if not (new_jobs or changed_jobs):
        return False

    if run_complete:
        return True

    from ats.base import STOP_BUDGET, STOP_MORE_AVAILABLE, STOP_PAGE_CEILING

    #: Truncations whose shape we know, because we chose where to stop.
    #:
    #: ``more_results_available`` joins them for a different reason than the
    #: other two, and the difference is worth stating. It is not newest-first,
    #: so the gap is not merely "the oldest postings" - a single-GET tier stuck
    #: on page one may never see a recent posting sitting on page two. What
    #: makes it safe here is that the gap is *stable*: the same tier fetches the
    #: same page every run, so the rows it does see are a consistent set and a
    #: genuinely new posting among them is genuinely new. The failure mode is a
    #: miss, never a false alarm - and the run summary names every such company
    #: with its shortfall, so the miss is visible rather than silent.
    #:
    #: Excluding it would mean permanent silence, which is the same trap
    #: ``page_ceiling`` fell into: a handful of teaser careers pages are
    #: incomplete on every run and always will be, and one known limitation
    #: should not cost all alerting.
    describable = {STOP_BUDGET, STOP_PAGE_CEILING, STOP_MORE_AVAILABLE}

    reasons = stop_reasons or set()
    if reasons and reasons <= describable:
        log.info("Run truncated only by ceilings we set (%s); the walk is "
                 "newest-first, so the gap is the oldest postings - sending "
                 "the digest anyway", ", ".join(sorted(reasons)))
        return True

    log.info("Run incomplete for reasons that make 'new' untrustworthy (%s); "
             "suppressing the digest", ", ".join(sorted(reasons)) or "unknown")
    return False


def _line(job: dict[str, Any]) -> str:
    link = job.get("apply_url") or job.get("job_url") or ""
    location = job.get("location") or "-"
    return f"{job.get('company', '?')} - {job.get('title', '?')} ({location})\n    {link}"


def _safe_link(value: Any) -> str:
    """An ``href`` value safe to emit, or empty.

    Escaping made the link *text* safe but said nothing about the scheme, and
    ``job_url`` is scraped from a third-party page. A ``javascript:`` or
    ``data:text/html`` URL stayed clickable - inert in most mail clients, but
    the dry-run preview is an HTML file opened in a browser, where it is not.
    Only http(s) is allowed through.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    scheme = text.split(":", 1)[0].lower() if ":" in text else ""
    if scheme and scheme not in ("http", "https"):
        log.debug("Dropping a job link with an unsupported scheme: %r", scheme)
        return ""
    return html.escape(text, quote=True)


def _row_html(job: dict[str, Any]) -> str:
    # Every field here is scraped from a third-party page and is going into an
    # HTML email, so all of it is escaped.
    link = _safe_link(job.get("apply_url") or job.get("job_url"))
    return (
        "<tr>"
        f"<td style='padding:6px 10px'>{html.escape(str(job.get('company') or '?'))}</td>"
        f"<td style='padding:6px 10px'><a href='{link}'>"
        f"{html.escape(str(job.get('title') or '?'))}</a></td>"
        f"<td style='padding:6px 10px'>{html.escape(str(job.get('location') or '-'))}</td>"
        f"<td style='padding:6px 10px'>{html.escape(str(job.get('remote_scope') or '-'))}</td>"
        "</tr>"
    )


def build_digest(
    new_jobs: list[dict[str, Any]],
    changed_jobs: list[dict[str, Any]],
    summary: dict[str, Any],
) -> Digest:
    """Render the digest in both plain text and HTML."""
    parts = []
    if new_jobs:
        parts.append(f"{len(new_jobs)} new")
    if changed_jobs:
        parts.append(f"{len(changed_jobs)} changed")
    subject = f"Data engineering jobs: {', '.join(parts) or 'no changes'}"

    text_lines: list[str] = []
    html_parts: list[str] = [
        "<div style='font-family:system-ui,-apple-system,Segoe UI,sans-serif'>"
    ]

    if new_jobs:
        text_lines += [f"NEW ({len(new_jobs)})", "=" * 40]
        text_lines += [_line(j) for j in new_jobs]
        text_lines.append("")
        html_parts.append(f"<h2>New ({len(new_jobs)})</h2><table>")
        html_parts += [_row_html(j) for j in new_jobs]
        html_parts.append("</table>")

    if changed_jobs:
        text_lines += [f"CHANGED ({len(changed_jobs)})", "=" * 40]
        for job in changed_jobs:
            fields = ", ".join(job.get("changed_fields") or []) or "updated"
            text_lines.append(f"{_line(job)}\n    changed: {fields}")
        text_lines.append("")
        html_parts.append(f"<h2>Changed ({len(changed_jobs)})</h2><table>")
        for job in changed_jobs:
            fields = html.escape(", ".join(job.get("changed_fields") or []) or "updated")
            html_parts.append(
                _row_html(job).replace(
                    "</tr>", f"<td style='padding:6px 10px'>{fields}</td></tr>"
                )
            )
        html_parts.append("</table>")

    if not (new_jobs or changed_jobs):
        # Reachable on a summary-only send. A body that is just a horizontal
        # rule reads as a broken template rather than as good news.
        text_lines += ["No new or changed matching jobs this run.", ""]
        html_parts.append(
            "<p>No new or changed matching jobs this run.</p>"
        )

    if summary:
        text_lines.append("-" * 40)
        for label, value in _summary_rows(summary):
            text_lines.append(f"{label:<26}{value}")
        html_parts.append(_summary_html(summary))

    html_parts.append("</div>")
    return Digest(subject, "\n".join(text_lines), "".join(html_parts))


def _count(summary: dict[str, Any], key: str) -> Any:
    value = summary.get(key)
    return f"{value:,}" if isinstance(value, int) else value


def _summary_rows(summary: dict[str, Any]) -> list[tuple[str, Any]]:
    """The run's own numbers, in the order a reader needs them.

    Four company outcomes rather than two: 3 new jobs out of a clean sweep and
    3 new jobs out of the 40 companies that did not time out are very different
    runs, and the old two-number block could not tell them apart.
    """
    rows: list[tuple[str, Any]] = []
    if summary.get("run_id"):
        rows.append(("Run", summary["run_id"]))
    rows.append(("Companies attempted", _count(summary, "companies_scanned")))
    for label, key in (
        ("Success", "companies_successful"),
        ("Partial", "companies_partial"),
        ("Failed", "companies_failed"),
        ("Blocked", "companies_blocked"),
    ):
        if summary.get(key) is not None:
            rows.append((f"  {label}", _count(summary, key)))
    rows.append(("Jobs fetched", _count(summary, "jobs_collected")))
    for label, key in (("New matching jobs", "new_jobs"),
                       ("Changed jobs", "changed_jobs")):
        if summary.get(key) is not None:
            rows.append((label, _count(summary, key)))

    # Partial companies are the ones whose absent jobs were never confirmed
    # absent, so their removals were deliberately not applied. Saying so is the
    # difference between a trustworthy removal count and a silent one.
    stopped_short = summary.get("companies_partial")
    if stopped_short is None:
        stopped_short = summary.get("incomplete_companies")
    if stopped_short:
        rows.append((
            "Note",
            f"{stopped_short} company(ies) stopped short - "
            f"their removals were not synced.",
        ))
    return rows


def _summary_html(summary: dict[str, Any]) -> str:
    cells = "".join(
        f"<tr><td style='padding:2px 12px 2px 0;color:#555'>"
        f"{html.escape(str(label).strip())}</td>"
        f"<td style='padding:2px 0'>{html.escape(str(value))}</td></tr>"
        for label, value in _summary_rows(summary)
    )
    return (
        "<h3 style='margin-top:24px;font-size:14px;color:#333'>This run</h3>"
        f"<table style='font-size:13px;color:#555'>{cells}</table>"
    )


def _write_preview(config: EmailConfig, digest: Digest, message: EmailMessage) -> bool:
    """Render a dry run's digest to disk and report success.

    Success is the honest answer: the digest was produced and delivered as far
    as this mode goes. The caller's own dry-run check is what stops these jobs
    being marked as announced, so a preview never suppresses a later real send.
    """
    directory = Path(config.preview_dir or Path("output") / "digest_preview")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "digest.txt").write_text(
            f"Subject: {digest.subject}\nTo: {', '.join(config.to)}\n\n{digest.text}",
            encoding="utf-8",
        )
        (directory / "digest.html").write_text(digest.html, encoding="utf-8")
        (directory / "digest.eml").write_text(message.as_string(), encoding="utf-8")
    except Exception as exc:
        log.error("Could not write the digest preview: %s", exc)
        return False

    log.info("DRY RUN: digest for %s written to %s (nothing was sent)",
             ", ".join(config.to), directory)
    return True


def send_digest(
    config: EmailConfig,
    digest: Digest,
    attachments: Iterable[Path] = (),
) -> bool:
    """Send the digest. Returns True only when the SMTP handoff succeeded.

    The caller must not mark jobs as notified unless this returns True -
    otherwise a failed send silently suppresses those jobs forever.
    """
    message = EmailMessage()
    message["Subject"] = digest.subject
    message["From"] = config.sender
    message["To"] = ", ".join(config.to)
    message.set_content(digest.text or "(no details)")
    message.add_alternative(digest.html, subtype="html")

    for path in attachments:
        path = Path(path)
        if not path.exists():
            log.warning("Attachment missing, skipping: %s", path)
            continue
        try:
            message.add_attachment(
                path.read_bytes(),
                maintype="application",
                subtype="octet-stream",
                filename=path.name,
            )
        except Exception as exc:
            log.warning("Could not attach %s: %s", path.name, exc)

    if config.dry_run:
        return _write_preview(config, digest, message)

    try:
        context = ssl.create_default_context()
        if config.port == 465:
            with smtplib.SMTP_SSL(config.host, config.port, context=context, timeout=60) as server:
                server.login(config.user, config.password)
                server.send_message(message)
        else:
            with smtplib.SMTP(config.host, config.port, timeout=60) as server:
                if config.use_tls:
                    server.starttls(context=context)
                server.login(config.user, config.password)
                server.send_message(message)
    except Exception as exc:
        # Best effort by design: the CSV on disk is the real deliverable, and
        # losing a run to a transient SMTP error would be a poor trade.
        log.error("Could not send the digest: %s", exc)
        return False

    log.info("Digest sent to %s", ", ".join(config.to))
    return True
