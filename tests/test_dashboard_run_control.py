"""Run control: launching, the cross-process lock, and exit-code handling.

Nothing here starts the real scraper. The launch path is exercised with an
injected ``Popen``, and the supervisor is exercised against a two-line stand-in
script - a dashboard test must never cost a live scrape.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta

import pytest

from dashboard import runner, services
from settings import Settings


@pytest.fixture()
def cfg(tmp_path) -> Settings:
    """Settings pointing every path at a temp directory."""
    return Settings(
        {
            "output": {
                "directory": str(tmp_path / "output"),
                "csv": "company_jobs.csv",
                "json": "company_jobs.json",
                "xlsx": "company_jobs.xlsx",
                "failures": "scraper_failures.csv",
            },
            "logging": {"file": str(tmp_path / "logs" / "scraper.log")},
            "hours_old": 168,
        },
        tmp_path / "settings.yaml",
    )


class FakeProcess:
    def __init__(self, pid: int = 4242):
        self.pid = pid


class RecordingPopen:
    """Stands in for ``subprocess.Popen`` and records how it was called."""

    def __init__(self, pid: int = 4242):
        self.calls: list[tuple[list[str], dict]] = []
        self.pid = pid

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), dict(kwargs)))
        return FakeProcess(self.pid)


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------

def test_run_button_launches_the_real_scraper_entry_point(cfg, monkeypatch):
    monkeypatch.setattr(services, "pid_alive", lambda pid: True)
    spawn = RecordingPopen()

    payload = services.start_run(cfg, popen=spawn)

    assert len(spawn.calls) == 1
    command, kwargs = spawn.calls[0]
    # An argument list, never a shell string.
    assert command[0] == sys.executable
    assert command[1:3] == ["-m", "dashboard.runner"]
    assert kwargs.get("shell") is None  # shell=False is Popen's default
    assert not any(isinstance(part, str) and " " in part and "--" in part for part in command[1:4])
    assert payload["pid"] == 4242


def test_a_dashboard_run_never_sends_email(cfg, monkeypatch):
    monkeypatch.setattr(services, "pid_alive", lambda pid: True)
    spawn = RecordingPopen()

    services.start_run(cfg, popen=spawn)

    command, kwargs = spawn.calls[0]
    assert "--no-email" in command
    # Belt and braces: settings.load_env_file never overrides an existing
    # variable, so this makes a local .env unable to turn delivery back on.
    assert kwargs["env"]["EMAIL_ENABLED"] == "false"
    assert kwargs["env"]["SCRAPER_SMTP_DRY_RUN"] == "1"


def test_dry_run_passes_the_existing_flag(cfg, monkeypatch):
    monkeypatch.setattr(services, "pid_alive", lambda pid: True)
    spawn = RecordingPopen()

    services.start_run(cfg, dry_run=True, popen=spawn)

    assert "--dry-run" in spawn.calls[0][0]
    assert json.loads(services.run_lock_path(cfg).read_text(encoding="utf-8"))["dry_run"] is True


def test_a_failed_spawn_releases_the_lock(cfg):
    def explode(command, **kwargs):
        raise OSError("no such executable")

    with pytest.raises(services.DashboardError):
        services.start_run(cfg, popen=explode)

    # Otherwise the dashboard would be wedged by a run that never existed.
    assert not services.run_lock_path(cfg).exists()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_a_second_run_is_refused_while_the_first_is_alive(cfg, monkeypatch):
    monkeypatch.setattr(services, "pid_alive", lambda pid: True)
    spawn = RecordingPopen()
    services.start_run(cfg, popen=spawn)

    with pytest.raises(services.RunAlreadyActive):
        services.start_run(cfg, popen=spawn)

    assert len(spawn.calls) == 1


def test_the_lock_is_claimed_before_anything_is_spawned(cfg, monkeypatch):
    """The claim must be atomic, or two tabs could both pass the check."""
    monkeypatch.setattr(services, "pid_alive", lambda pid: True)

    seen: list[bool] = []

    def spawn(command, **kwargs):
        seen.append(services.run_lock_path(cfg).exists())
        return FakeProcess()

    services.start_run(cfg, popen=spawn)
    assert seen == [True]


def test_is_run_active_tracks_the_owning_process(cfg, monkeypatch):
    monkeypatch.setattr(services, "pid_alive", lambda pid: True)
    services.start_run(cfg, popen=RecordingPopen())
    assert services.is_run_active(cfg) is True

    monkeypatch.setattr(services, "pid_alive", lambda pid: False)
    assert services.is_run_active(cfg) is False


# ---------------------------------------------------------------------------
# Stale locks
# ---------------------------------------------------------------------------

def test_a_dead_owner_makes_the_lock_stale_not_running(cfg, monkeypatch):
    monkeypatch.setattr(services, "pid_alive", lambda pid: True)
    services.start_run(cfg, popen=RecordingPopen())

    monkeypatch.setattr(services, "pid_alive", lambda pid: False)
    state, lock = services.lock_state(cfg)
    assert state == services.STATUS_STALE
    assert lock["pid"] == 4242


def test_a_stale_lock_blocks_a_new_run_until_it_is_cleared(cfg, monkeypatch):
    monkeypatch.setattr(services, "pid_alive", lambda pid: True)
    services.start_run(cfg, popen=RecordingPopen())
    monkeypatch.setattr(services, "pid_alive", lambda pid: False)

    with pytest.raises(services.RunAlreadyActive):
        services.start_run(cfg, popen=RecordingPopen())

    assert services.clear_stale_lock(cfg) is True
    assert not services.run_lock_path(cfg).exists()
    services.start_run(cfg, popen=RecordingPopen())  # now allowed


def test_a_live_lock_is_never_cleared(cfg, monkeypatch):
    monkeypatch.setattr(services, "pid_alive", lambda pid: True)
    services.start_run(cfg, popen=RecordingPopen())

    with pytest.raises(services.RunAlreadyActive):
        services.clear_stale_lock(cfg)
    assert services.run_lock_path(cfg).exists()


def test_a_lock_that_never_got_a_pid_times_out(cfg):
    services.output_dir(cfg).mkdir(parents=True, exist_ok=True)
    lock = services.run_lock_path(cfg)
    fresh = services.utcnow()
    lock.write_text(json.dumps({"pid": None, "started_at": fresh.isoformat()}), encoding="utf-8")

    assert services.lock_state(cfg, now=fresh)[0] == services.STATUS_RUNNING
    late = fresh + timedelta(seconds=services.STARTUP_GRACE_SECONDS + 1)
    assert services.lock_state(cfg, now=late)[0] == services.STATUS_STALE


def test_an_unreadable_lock_is_still_a_lock(cfg):
    services.output_dir(cfg).mkdir(parents=True, exist_ok=True)
    services.run_lock_path(cfg).write_text("{ not json", encoding="utf-8")

    state, lock = services.lock_state(cfg)
    assert state == services.STATUS_STALE
    assert lock["unreadable"] is True


def test_a_run_older_than_the_ceiling_is_stale_even_if_the_pid_lives(cfg, monkeypatch):
    """Guards against a PID the OS recycled onto an unrelated process."""
    monkeypatch.setattr(services, "pid_alive", lambda pid: True)
    services.output_dir(cfg).mkdir(parents=True, exist_ok=True)
    ancient = services.utcnow() - timedelta(seconds=services.MAX_RUN_SECONDS + 60)
    services.run_lock_path(cfg).write_text(
        json.dumps({"pid": 1, "started_at": ancient.isoformat()}), encoding="utf-8"
    )
    assert services.lock_state(cfg)[0] == services.STATUS_STALE


def test_pid_alive_says_no_for_a_pid_that_cannot_exist():
    assert services.pid_alive(0) is False
    assert services.pid_alive(None) is False
    assert services.pid_alive(-1) is False


def test_pid_alive_says_yes_for_this_process():
    import os

    assert services.pid_alive(os.getpid()) is True


# ---------------------------------------------------------------------------
# The supervisor: exit codes
# ---------------------------------------------------------------------------

def _stub_scraper(tmp_path, exit_code: int, message: str = "hello"):
    script = tmp_path / "stub_main.py"
    script.write_text(
        f"import sys\nprint({message!r})\nsys.exit({exit_code})\n", encoding="utf-8"
    )
    return script


def test_the_supervisor_records_a_nonzero_exit_code(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "MAIN_PY", _stub_scraper(tmp_path, 2, "boom"))
    services.output_dir(cfg).mkdir(parents=True, exist_ok=True)
    lock = services.run_lock_path(cfg)
    lock.write_text("{}", encoding="utf-8")

    code = runner.main([
        "--lock", str(lock),
        "--state", str(services.run_state_path(cfg)),
        "--log", str(services.run_log_path(cfg)),
        "--", "--no-email",
    ])

    assert code == 2
    state = json.loads(services.run_state_path(cfg).read_text(encoding="utf-8"))
    assert state["exit_code"] == 2
    assert "boom" in state["console_tail"]
    # The lock is released whatever the outcome, or the dashboard stays wedged.
    assert not lock.exists()


def test_the_supervisor_records_a_clean_exit(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "MAIN_PY", _stub_scraper(tmp_path, 0))
    services.output_dir(cfg).mkdir(parents=True, exist_ok=True)
    lock = services.run_lock_path(cfg)
    lock.write_text("{}", encoding="utf-8")

    assert runner.main([
        "--lock", str(lock),
        "--state", str(services.run_state_path(cfg)),
        "--log", str(services.run_log_path(cfg)),
        "--", "--no-email",
    ]) == 0
    assert json.loads(
        services.run_state_path(cfg).read_text(encoding="utf-8")
    )["exit_code"] == 0


def test_the_supervisor_releases_the_lock_when_the_spawn_itself_fails(
    cfg, tmp_path, monkeypatch
):
    def explode(*args, **kwargs):
        raise OSError("cannot spawn")

    monkeypatch.setattr(runner.subprocess, "Popen", explode)
    services.output_dir(cfg).mkdir(parents=True, exist_ok=True)
    lock = services.run_lock_path(cfg)
    lock.write_text("{}", encoding="utf-8")

    runner.main([
        "--lock", str(lock),
        "--state", str(services.run_state_path(cfg)),
        "--log", str(services.run_log_path(cfg)),
        "--", "--no-email",
    ])

    state = json.loads(services.run_state_path(cfg).read_text(encoding="utf-8"))
    assert state["exit_code"] == 1
    assert "cannot spawn" in state["error"]
    assert not lock.exists()


def test_the_supervisor_transcript_holds_only_the_current_run(cfg, tmp_path, monkeypatch):
    """Single current log, truncated per run - never an accumulating history."""
    transcript = services.run_log_path(cfg)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("output from a much older run\n", encoding="utf-8")

    monkeypatch.setattr(runner, "MAIN_PY", _stub_scraper(tmp_path, 0, "second run"))
    lock = services.run_lock_path(cfg)
    services.output_dir(cfg).mkdir(parents=True, exist_ok=True)
    lock.write_text("{}", encoding="utf-8")
    runner.main([
        "--lock", str(lock),
        "--state", str(services.run_state_path(cfg)),
        "--log", str(transcript),
        "--", "--no-email",
    ])

    text = transcript.read_text(encoding="utf-8")
    assert "second run" in text
    assert "much older run" not in text


def test_the_supervisor_invokes_main_py_without_a_shell(cfg, tmp_path, monkeypatch):
    captured: dict = {}

    class Recorded:
        def __init__(self, command, **kwargs):
            captured["command"] = list(command)
            captured["kwargs"] = kwargs

        def wait(self):
            return 0

    monkeypatch.setattr(runner.subprocess, "Popen", Recorded)
    services.output_dir(cfg).mkdir(parents=True, exist_ok=True)
    lock = services.run_lock_path(cfg)
    lock.write_text("{}", encoding="utf-8")

    runner.main([
        "--lock", str(lock),
        "--state", str(services.run_state_path(cfg)),
        "--log", str(services.run_log_path(cfg)),
        "--", "--no-email", "--dry-run",
    ])

    assert captured["command"][0] == sys.executable
    assert captured["command"][1].endswith("main.py")
    assert captured["command"][2:] == ["--no-email", "--dry-run"]
    assert "shell" not in captured["kwargs"]
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
