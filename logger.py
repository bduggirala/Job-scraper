"""Structured logging for the company ATS scraper.

Writes UTF-8 logs to ``logs/scraper.log`` and a human-readable stream to the
console. Console output is reconfigured to UTF-8 so provider arrows ("->")
and non-ASCII company names do not blow up on Windows cp1252 terminals.

**One log, for the current run only.** Each run truncates ``logs/scraper.log``
and writes into it as it goes, so the file always describes exactly one run and
nothing accumulates. That matters more than it sounds: the previous behaviour
appended, with three 5 MB rotated backups behind it, so diagnosing a run meant
finding where it started inside a file holding several - and the rotation could
silently discard the beginning of a long run to make room for its own end.

Writing incrementally (rather than buffering and dumping at the end) is what
keeps a *failed* run diagnosable: whatever it managed to log is on disk.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False

FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
CONSOLE_FORMAT = "[%(levelname)s] %(message)s"


def setup_logging(
    log_file: Path | str,
    level: str = "INFO",
    quiet: bool = False,
    *,
    fresh: bool = True,
) -> logging.Logger:
    """Configure root logging once. Safe to call repeatedly.

    Args:
        log_file: the single current log. Its directory is created if absent.
        level: console verbosity; the file always records DEBUG.
        quiet: silence the console without affecting the file.
        fresh: truncate ``log_file`` so it holds this run only. Pass False to
            append - used by the canary, which is not a run of its own and
            should not erase the log of the run being diagnosed.
    """
    global _CONFIGURED

    root = logging.getLogger()
    if _CONFIGURED:
        return logging.getLogger("scraper")

    # Windows consoles default to cp1252 and raise on non-ASCII output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # mode="w" truncates on open, which is the whole policy: one file, this
    # run. A plain FileHandler rather than a rotating one because rotation
    # exists to bound growth *across* runs, and truncation already does that -
    # keeping both would leave scraper.log.1..3 behind, which is precisely the
    # accumulation this replaces.
    file_handler = logging.FileHandler(
        log_path, mode="w" if fresh else "a", encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.CRITICAL if quiet else getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT))
    root.addHandler(console)

    # Third-party noise we never want at INFO.
    for noisy in ("urllib3", "requests", "asyncio", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    return logging.getLogger("scraper")


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced child logger."""
    return logging.getLogger(name)
