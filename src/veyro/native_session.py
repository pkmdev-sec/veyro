from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from veyro.agents import AgentDefinition


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _foreground_terminal() -> int | None:
    if not os.isatty(0):
        return None
    try:
        return 0 if os.tcgetpgrp(0) == os.getpgrp() else None
    except OSError:
        return None


def _run_git(repository: Path, *args: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def workspace_snapshot(repository: Path) -> dict[str, object] | None:
    head = _run_git(repository, "rev-parse", "HEAD")
    status = _run_git(repository, "status", "--porcelain=v1", "-z")
    if head is None or status is None:
        return None
    paths = [entry for entry in status.split(b"\0") if entry]
    return {
        "head": head.decode("ascii", errors="replace").strip(),
        "changed_paths": len(paths),
        "state_sha256": hashlib.sha256(status).hexdigest(),
    }


SessionStatus = Literal["starting", "running", "completed", "failed", "terminated"]


@dataclass(slots=True)
class NativeSessionRecord:
    schema_version: int
    session_id: str
    agent_id: str
    repository: str
    executable: str
    agent_version: str | None
    veyro_pid: int
    agent_pid: int | None
    status: SessionStatus
    started_at: str
    updated_at: str
    finished_at: str | None = None
    exit_code: int | None = None
    termination_signal: int | None = None
    prompt_sha256: str | None = None
    event_count: int = 0


class NativeSessionStore:
    def __init__(self, repository: Path, session_id: str) -> None:
        self.directory = repository / ".veyro" / "native-sessions" / session_id
        self.record_path = self.directory / "session.json"
        self.events_path = self.directory / "events.jsonl"
        self._lock = threading.Lock()
        self._sequence = 0

    def save(self, record: NativeSessionRecord) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        temporary = self.record_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(record), indent=2, sort_keys=True) + "\n")
        os.replace(temporary, self.record_path)

    def append(self, event_type: str, payload: dict[str, object]) -> int:
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
            self._sequence += 1
            event = {
                "schema_version": 1,
                "sequence": self._sequence,
                "timestamp": _now(),
                "event_type": event_type,
                "payload": payload,
            }
            with self.events_path.open("a") as stream:
                stream.write(json.dumps(event, sort_keys=True, default=str) + "\n")
            return self._sequence


@dataclass(frozen=True, slots=True)
class ManagedNativeResult:
    session_id: str
    record_path: Path
    exit_code: int
    autonomy_directory: Path | None = None


class ManagedNativeSession:
    """Keep Veyro alive beside a native TUI without reading or rewriting its terminal output."""

    def __init__(
        self,
        *,
        definition: AgentDefinition,
        repository: Path,
        command: list[str],
        executable: str,
        agent_version: str | None,
        prompt: str | None,
        snapshot_interval_seconds: float = 2.0,
        on_started: Callable[[NativeSessionRecord], None] | None = None,
        environment: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        completion_check: Callable[[], bool] | None = None,
        termination_grace_seconds: float = 2.0,
    ) -> None:
        self.definition = definition
        self.repository = repository.resolve()
        self.command = command
        self.executable = executable
        self.agent_version = agent_version
        self.snapshot_interval_seconds = snapshot_interval_seconds
        self.on_started = on_started
        self.environment = environment or {}
        self.timeout_seconds = timeout_seconds
        self.completion_check = completion_check
        self.termination_grace_seconds = termination_grace_seconds
        self._timed_out = False
        self._kill_timer: threading.Timer | None = None
        session_id = uuid4().hex[:12]
        now = _now()
        self.record = NativeSessionRecord(
            schema_version=1,
            session_id=session_id,
            agent_id=definition.agent_id.value,
            repository=str(self.repository),
            executable=executable,
            agent_version=agent_version,
            veyro_pid=os.getpid(),
            agent_pid=None,
            status="starting",
            started_at=now,
            updated_at=now,
            prompt_sha256=(hashlib.sha256(prompt.encode()).hexdigest() if prompt else None),
        )
        self.store = NativeSessionStore(self.repository, session_id)
        self._stop_monitor = threading.Event()
        self._state_lock = threading.Lock()
        self._received_signal: int | None = None
        self._process: subprocess.Popen[Any] | None = None
        self._owns_process_group = False

    def _record_event(self, event_type: str, payload: dict[str, object]) -> None:
        with self._state_lock:
            self.record.event_count = self.store.append(event_type, payload)
            self.record.updated_at = _now()
            self.store.save(self.record)

    def _monitor_workspace(self) -> None:
        previous: dict[str, object] | None | object = object()
        while not self._stop_monitor.is_set():
            snapshot = workspace_snapshot(self.repository)
            if snapshot != previous:
                self._record_event("workspace.changed", {"snapshot": snapshot})
                previous = snapshot
            self._stop_monitor.wait(self.snapshot_interval_seconds)

    def _forward_signal(self, signum: int, _frame: object) -> None:
        self._received_signal = signum
        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            if self._owns_process_group:
                os.killpg(process.pid, signum)
            else:
                process.send_signal(signum)
        except ProcessLookupError:
            pass

    def _kill_after_timeout(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _deadline_reached(self) -> None:
        if self.completion_check is not None and self.completion_check():
            return
        self._timed_out = True
        self._forward_signal(signal.SIGTERM, None)
        self._kill_timer = threading.Timer(self.termination_grace_seconds, self._kill_after_timeout)
        self._kill_timer.daemon = True
        self._kill_timer.start()

    def _wait_with_terminal(self, process: subprocess.Popen[Any], terminal_fd: int) -> int:
        parent_group = os.getpgrp()
        previous_ttou = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        try:
            os.tcsetpgrp(terminal_fd, process.pid)
            while True:
                _, status = os.waitpid(process.pid, os.WUNTRACED)
                if os.WIFSTOPPED(status):
                    os.tcsetpgrp(terminal_fd, parent_group)
                    os.kill(os.getpid(), signal.SIGTSTP)
                    os.tcsetpgrp(terminal_fd, process.pid)
                    os.killpg(process.pid, signal.SIGCONT)
                    continue
                exit_code = os.waitstatus_to_exitcode(status)
                process.returncode = exit_code
                return exit_code
        finally:
            os.tcsetpgrp(terminal_fd, parent_group)
            signal.signal(signal.SIGTTOU, previous_ttou)

    def run(self) -> ManagedNativeResult:
        self.store.save(self.record)
        terminal_fd = _foreground_terminal()
        popen_options: dict[str, object]
        if terminal_fd is None:
            popen_options = {"start_new_session": True}
        else:
            popen_options = {"process_group": 0}
        try:
            child_environment = os.environ.copy()
            child_environment.update(self.environment)
            child_environment.update(
                {
                    "VEYRO_SESSION_ID": self.record.session_id,
                    "VEYRO_SESSION_DIR": str(self.store.directory),
                    "VEYRO_SESSION_FILE": str(self.store.record_path),
                }
            )
            process = subprocess.Popen(
                self.command,
                cwd=self.repository,
                env=child_environment,
                **popen_options,
            )
        except OSError as error:
            self.record.status = "failed"
            self.record.finished_at = _now()
            self._record_event("session.launch_failed", {"error": str(error)})
            raise
        self._process = process
        self._owns_process_group = True
        self.record.agent_pid = process.pid
        self.record.status = "running"
        self._record_event(
            "session.started",
            {
                "agent_id": self.definition.agent_id.value,
                "agent_pid": process.pid,
                "native_terminal": terminal_fd is not None,
            },
        )
        if self.on_started is not None:
            self.on_started(self.record)
        monitor = threading.Thread(
            target=self._monitor_workspace,
            name=f"veyro-native-{self.record.session_id}",
            daemon=True,
        )
        monitor.start()
        watched_signals = (
            (signal.SIGTERM, signal.SIGHUP)
            if terminal_fd is not None
            else (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGHUP)
        )
        previous_handlers = {
            signum: signal.signal(signum, self._forward_signal) for signum in watched_signals
        }
        deadline = None
        if self.timeout_seconds is not None:
            deadline = threading.Timer(self.timeout_seconds, self._deadline_reached)
            deadline.daemon = True
            deadline.start()
        try:
            exit_code = (
                self._wait_with_terminal(process, terminal_fd)
                if terminal_fd is not None
                else process.wait()
            )
        finally:
            if deadline is not None:
                deadline.cancel()
                deadline.join()
            if self._kill_timer is not None:
                self._kill_timer.cancel()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            self._stop_monitor.set()
            monitor.join(timeout=max(1.0, self.snapshot_interval_seconds + 0.5))

        if self._timed_out:
            exit_code = 124
        self.record.exit_code = exit_code
        self.record.finished_at = _now()
        if self._received_signal is not None:
            self.record.status = "terminated"
            self.record.termination_signal = self._received_signal
        elif exit_code == 0:
            self.record.status = "completed"
        else:
            self.record.status = "failed"
        self._record_event(
            "session.finished",
            {
                "exit_code": exit_code,
                "termination_signal": self.record.termination_signal,
            },
        )
        return ManagedNativeResult(
            session_id=self.record.session_id,
            record_path=self.store.record_path,
            exit_code=exit_code,
        )
