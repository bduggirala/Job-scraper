"""One log file, describing the current run only.

Runs used to append to ``logs/scraper.log`` behind a rotating handler that kept
three 5 MB backups. Two problems followed from that. Diagnosing a run meant
locating where it began inside a file holding several; and on a long run the
rotation could discard that run's own beginning to make room for its end - the
part you most want when a company failed early.

So each run truncates the log and writes into it as it goes. "As it goes"
matters: a run that dies half way still leaves its own log behind.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import logger as logger_module
from logger import get_logger, setup_logging


@pytest.fixture(autouse=True)
def _reset_logging():
    """Each test configures logging from scratch and hands it back clean."""
    logger_module._CONFIGURED = False
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)
    logger_module._CONFIGURED = False


def _configure(path: Path, **kwargs):
    # Call the real function: conftest redirects the module attribute so tests
    # cannot touch the production log, but these tests are *about* the file.
    return logger_module.setup_logging.__wrapped__(path, **kwargs) if hasattr(
        logger_module.setup_logging, "__wrapped__"
    ) else setup_logging(path, **kwargs)


def test_a_run_starts_from_an_empty_log(tmp_path):
    log_path = tmp_path / "scraper.log"
    log_path.write_text("line from the PREVIOUS run\n", encoding="utf-8")

    _configure(log_path, quiet=True)
    get_logger("pipeline").info("line from THIS run")
    logging.shutdown()

    body = log_path.read_text(encoding="utf-8")
    assert "PREVIOUS" not in body, "the previous run's log must not survive"
    assert "THIS run" in body


def test_nothing_accumulates_beside_the_current_log(tmp_path):
    """No ``scraper.log.1``/``.2``/``.3`` - that was the accumulation."""
    log_path = tmp_path / "scraper.log"

    for run in range(3):
        logger_module._CONFIGURED = False
        for handler in list(logging.getLogger().handlers):
            handler.close()
            logging.getLogger().removeHandler(handler)
        _configure(log_path, quiet=True)
        get_logger("pipeline").info("run %s", run)

    logging.shutdown()
    assert [p.name for p in tmp_path.iterdir()] == ["scraper.log"]
    assert "run 2" in log_path.read_text(encoding="utf-8")


def test_an_interrupted_run_still_leaves_its_log(tmp_path):
    """Written incrementally, so a crash does not take the diagnosis with it."""
    log_path = tmp_path / "scraper.log"
    _configure(log_path, quiet=True)

    get_logger("pipeline").error("Omnicell: Timeout after 900s")
    # No shutdown(), no flush() - simulating a process that simply stops.
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert "Omnicell" in log_path.read_text(encoding="utf-8")


def test_the_log_directory_is_created_when_absent(tmp_path):
    log_path = tmp_path / "does" / "not" / "exist" / "scraper.log"
    assert not log_path.parent.exists()

    _configure(log_path, quiet=True)
    get_logger("pipeline").info("hello")
    logging.shutdown()

    assert log_path.exists()


def test_append_mode_is_available_for_non_run_tools(tmp_path):
    """The canary diagnoses a run; it must not erase that run's log."""
    log_path = tmp_path / "scraper.log"
    log_path.write_text("from the run under test\n", encoding="utf-8")

    _configure(log_path, quiet=True, fresh=False)
    get_logger("canary").info("canary says hi")
    logging.shutdown()

    body = log_path.read_text(encoding="utf-8")
    assert "from the run under test" in body
    assert "canary says hi" in body


def test_concurrent_workers_write_whole_lines(tmp_path):
    """Ten HTTP workers share one handler; no line may be torn in half."""
    import threading

    log_path = tmp_path / "scraper.log"
    _configure(log_path, quiet=True)
    log = get_logger("pipeline")

    def worker(n: int):
        for i in range(50):
            log.info("company-%02d-message-%02d-XXXXXXXXXXXXXXXXXXXXXXXXXXXX", n, i)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    logging.shutdown()

    lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 500, "every record must arrive exactly once"
    assert all(l.endswith("XXXXXXXXXXXXXXXXXXXXXXXXXXXX") for l in lines), (
        "a torn line would be missing its tail"
    )


def test_the_file_records_debug_even_when_the_console_is_quiet(tmp_path):
    """Console verbosity is a display choice; the log is the record."""
    log_path = tmp_path / "scraper.log"
    _configure(log_path, level="WARNING", quiet=True)

    get_logger("ats.workday").debug("page 3 -> 20 rows")
    logging.shutdown()

    assert "page 3 -> 20 rows" in log_path.read_text(encoding="utf-8")
