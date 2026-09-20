from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from veyro.agents import AgentId, agent_definition
from veyro.native_session import ManagedNativeSession, workspace_snapshot


class FakeProcess:
    def __init__(self, exit_code: int = 0, wait_seconds: float = 0.02) -> None:
        self.pid = 43210
        self.returncode = None
        self.exit_code = exit_code
        self.wait_seconds = wait_seconds
        self.signals: list[int] = []

    def wait(self) -> int:
        time.sleep(self.wait_seconds)
        self.returncode = self.exit_code
        return self.exit_code

    def poll(self):
        return self.returncode

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)


def session(tmp_path: Path, *, on_started=None) -> ManagedNativeSession:
    return ManagedNativeSession(
        definition=agent_definition(AgentId.CLAUDE),
        repository=tmp_path,
        command=["/bin/claude", "inspect this"],
        executable="/bin/claude",
        agent_version="2.1.228",
        prompt="inspect this",
        snapshot_interval_seconds=0.005,
        on_started=on_started,
    )


def test_managed_session_inherits_terminal_and_persists_lifecycle(monkeypatch, tmp_path) -> None:
    calls = {}
    process = FakeProcess()

    def popen(command, **kwargs):
        calls.update(command=command, **kwargs)
        return process

    snapshots = iter(
        [
            {"head": "abc", "changed_paths": 0, "state_sha256": "one"},
            {"head": "abc", "changed_paths": 1, "state_sha256": "two"},
        ]
    )
    monkeypatch.setattr("veyro.native_session._foreground_terminal", lambda: None)
    monkeypatch.setattr("veyro.native_session.subprocess.Popen", popen)
    monkeypatch.setattr(
        "veyro.native_session.workspace_snapshot",
        lambda repository: next(
            snapshots, {"head": "abc", "changed_paths": 1, "state_sha256": "two"}
        ),
    )

    started = []
    result = session(tmp_path, on_started=lambda record: started.append(record.status)).run()

    assert result.exit_code == 0
    assert started == ["running"]
    assert calls["command"] == ["/bin/claude", "inspect this"]
    assert calls["cwd"] == tmp_path.resolve()
    assert calls["start_new_session"] is True
    assert calls["env"]["VEYRO_SESSION_ID"] == result.session_id
    assert calls["env"]["VEYRO_SESSION_FILE"] == str(result.record_path)
    assert "stdin" not in calls and "stdout" not in calls and "stderr" not in calls

    record = json.loads(result.record_path.read_text())
    assert record["status"] == "completed"
    assert record["agent_id"] == "claude"
    assert record["agent_pid"] == 43210
    assert (result.record_path.parent.stat().st_mode & 0o777) == 0o700
    assert record["prompt_sha256"]
    assert "inspect this" not in result.record_path.read_text()

    events = [
        json.loads(line)
        for line in result.record_path.with_name("events.jsonl").read_text().splitlines()
    ]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert events[0]["event_type"] == "session.started"
    assert events[-1]["event_type"] == "session.finished"
    assert any(event["event_type"] == "workspace.changed" for event in events)


def test_managed_session_preserves_native_failure_status(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("veyro.native_session._foreground_terminal", lambda: None)
    monkeypatch.setattr(
        "veyro.native_session.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(exit_code=7, wait_seconds=0),
    )
    monkeypatch.setattr("veyro.native_session.workspace_snapshot", lambda repository: None)

    result = session(tmp_path).run()
    record = json.loads(result.record_path.read_text())

    assert result.exit_code == 7
    assert record["status"] == "failed"
    assert record["exit_code"] == 7


def test_managed_session_records_launch_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("veyro.native_session._foreground_terminal", lambda: None)

    def fail(*args, **kwargs):
        raise OSError("cannot launch")

    monkeypatch.setattr("veyro.native_session.subprocess.Popen", fail)
    managed = session(tmp_path)

    with pytest.raises(OSError, match="cannot launch"):
        managed.run()

    record = json.loads(managed.store.record_path.read_text())
    assert record["status"] == "failed"
    events = managed.store.events_path.read_text()
    assert "session.launch_failed" in events


def test_workspace_snapshot_records_state_without_file_contents(tmp_path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True
    )
    (tmp_path / "tracked.txt").write_text("initial")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)
    (tmp_path / "tracked.txt").write_text("private changed content")

    snapshot = workspace_snapshot(tmp_path)

    assert snapshot is not None
    assert snapshot["changed_paths"] == 1
    assert len(str(snapshot["state_sha256"])) == 64
    assert "private changed content" not in json.dumps(snapshot)


def test_terminal_session_hands_foreground_to_native_process_and_restores_it(
    monkeypatch, tmp_path
) -> None:
    managed = session(tmp_path)
    process = FakeProcess(wait_seconds=0)
    groups = []
    monkeypatch.setattr("veyro.native_session.os.getpgrp", lambda: 111)
    monkeypatch.setattr(
        "veyro.native_session.os.tcsetpgrp", lambda fd, group: groups.append((fd, group))
    )
    monkeypatch.setattr("veyro.native_session.os.waitpid", lambda pid, options: (process.pid, 0))
    monkeypatch.setattr("veyro.native_session.signal.signal", lambda signum, handler: "previous")

    result = managed._wait_with_terminal(process, 0)

    assert result == 0
    assert process.returncode == 0
    assert groups == [(0, process.pid), (0, 111)]
