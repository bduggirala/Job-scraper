"""Structured logging for the company ATS scraper.

Writes UTF-8 logs to ``logs/scraper.log`` and a human-readable stream to the
console. Console output is reconfigured to UTF-8 so provider arrows ("->")
and non-ASCII company names do not blow up on Windows cp1252 terminals.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False

FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
CONSOLE_FORMAT = "[%(levelname)s] %(message)s"


def setup_logging(log_file: Path | str, level: str = "INFO", quiet: bool = False) -> logging.Logger:
    """Configure root logging once. Safe to call repeatedly."""
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

    file_handler = RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
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
