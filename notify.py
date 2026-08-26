"""Email digest for newly discovered and changed jobs.

Three rules shape this module, and they matter more than the formatting:

* **Nothing is sent when there is nothing to say.** A channel that mails
  "0 new jobs" every run is one you stop opening, and then it is worse than no
  channel at all.
* **Nothing is sent from an incomplete run.** A truncated scrape's "new" set is
  untrustworthy for the same reason its "removed" set is - it never saw the
  pages it did not reach.
* **Credentials come from the environment only.** ``settings.yaml`` is checked
  into the repository; an SMTP password must never be able to land there.

Sending is deliberately best-effort: a mail failure is logged and the run still
succeeds, because the CSV on disk is the actual deliverable and losing it to a
transient SMTP error would be a poor trade.
"""

from __future__ import annotations

import html
import os
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

_TRUTHY = {"1", "true", "yes", "on"}


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

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"EmailConfig(host={self.host!r}, port={self.port}, "
            f"user={self.user!r}, to={self.to!r}, dry_run={self.dry_run}, "
            f"password=<hidden>)"
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
    if not section.get("enabled"):
        return None

    recipients = section.get("to")
    if isinstance(recipients, str):
        recipients = [recipients]
    recipients = [str(r).strip() for r in (recipients or []) if str(r).strip()]
    if not recipients:
        log.warning("Email notifications enabled but no recipient configured")
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

    preview = section.get("preview_dir")
    return EmailConfig(
        host=str(host or ""),
        port=int(os.environ.get(ENV_PORT) or section.get("port") or 587),
        user=str(user or ""),
        password=str(password or ""),
        to=recipients,
        sender=str(section.get("from") or user or "scraper@localhost"),
        dry_run=dry_run,
        preview_dir=Path(preview) if preview else None,
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

    A **budget truncation** leaves a hole we can describe. The walk is
    newest-first, so what was missed is the oldest postings, and nothing inside
    a 7-day freshness window sits behind it. Treating that as fatal would mean
    permanent silence for any employer too large to collect in full - CVS
    Health lists 19,246 postings against a provider serving ten per request,
    so it is truncated on every run and always will be. One known limitation
    should not cost all alerting.

    ``stop_reasons`` omitted keeps the strict behaviour, so a caller that has
    not been taught the distinction stays cautious.
    """
    if not (new_jobs or changed_jobs):
        return False

    if run_complete:
        return True

    from ats.base import STOP_BUDGET

    reasons = stop_reasons or set()
    if reasons and reasons <= {STOP_BUDGET}:
        log.info("Run truncated by the job budget only (newest-first, so the "
                 "gap is the oldest postings); sending the digest anyway")
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

    if summary:
        scanned = summary.get("companies_scanned", "?")
        collected = summary.get("jobs_collected", "?")
        text_lines += [
            "-" * 40,
            f"{scanned} companies scanned, {collected} jobs collected.",
        ]
        if summary.get("incomplete_companies"):
            text_lines.append(
                f"{summary['incomplete_companies']} company(ies) stopped short - "
                "their removals were not synced."
            )
        html_parts.append(
            f"<p style='color:#555;font-size:13px'>{scanned} companies scanned, "
            f"{collected} jobs collected.</p>"
        )

    html_parts.append("</div>")
    return Digest(subject, "\n".join(text_lines), "".join(html_parts))


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
