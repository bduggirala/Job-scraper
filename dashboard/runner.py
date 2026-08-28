"""Supervisor for one dashboard-launched scraper run.

    python -m dashboard.runner --lock ... --state ... --log ... -- --no-email

Its only job is to make a run's *outcome* survive the dashboard. Streamlit is
restarted by every code edit and by every terminal that gets closed; if the
Streamlit process were the one holding the ``Popen`` handle, a restart mid-run
would lose the exit code, and the dashboard would have to guess from the files
whether the run finished or died. So a small, boring process sits between them:

* it spawns ``main.py`` with an argument list (never a shell string),
* it captures that run's console output to the single current dashboard log,
* on exit - clean, crashed or killed - it writes the real exit code to the
  state file and releases the run lock, in a ``finally``.

That is what lets the dashboard say "failed, exit code 2" instead of "a
process was started once". It deliberately holds no opinion about what the
scraper does; the scraper's own ``last_run.json`` remains the source of truth
for per-company outcomes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = PROJECT_ROOT / "main.py"

#: How much of the console transcript is kept in the state file, so a failure
#: can be explained without reading the log. Bounded on purpose - the state
#: file is a status record, not a second copy of the log.
TAIL_CHARS = 4000


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tail(path: Path, limit: int = TAIL_CHARS) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def _write_state(path: Path, payload: dict) -> None:
    """Write the terminal state, atomically.

    A dashboard polling this file must never read half of it, so it lands via
    a temp file and ``os.replace``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(temp, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dashboard.runner",
        description="Run main.py and record its exit code for the dashboard.",
    )
    parser.add_argument("--lock", required=True, help="Run lock to release when the run ends")
    parser.add_argument("--state", required=True, help="Where to write the run outcome")
    parser.add_argument("--log", required=True, help="Console transcript for this run")
    parser.add_argument("--started-at", default=None, help="ISO-8601 UTC start stamp")
    parser.add_argument(
        "scraper_args", nargs=argparse.REMAINDER,
        help="Flags for main.py, after a bare --",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    scraper_args = list(args.scraper_args)
    if scraper_args and scraper_args[0] == "--":
        scraper_args = scraper_args[1:]

    lock_path = Path(args.lock)
    state_path = Path(args.state)
    log_file = Path(args.log)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    started_at = args.started_at or _utcnow_iso()
    dry_run = "--dry-run" in scraper_args

    exit_code: int | None = None
    error = ""
    try:
        # mode="w": one transcript, this run only - the same single-current-log
        # policy logs/scraper.log follows.
        with log_file.open("w", encoding="utf-8", errors="replace") as handle:
            process = subprocess.Popen(
                [sys.executable, str(MAIN_PY), *scraper_args],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
            exit_code = process.wait()
    except BaseException as exc:  # noqa: BLE001 - the outcome must be recorded whatever happens
        error = f"{type(exc).__name__}: {exc}"
        if exit_code is None:
            exit_code = 1
    finally:
        _write_state(state_path, {
            "started_at": started_at,
            "finished_at": _utcnow_iso(),
            "exit_code": exit_code,
            "args": scraper_args,
            "dry_run": dry_run,
            "log": str(log_file),
            "error": error,
            "console_tail": _tail(log_file),
        })
        # Released last: while this file exists the dashboard considers a run
        # in flight, and the workbook stays read-only.
        try:
            lock_path.unlink()
        except OSError:
            pass

    return int(exit_code or 0)


if __name__ == "__main__":
    sys.exit(main())
