from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import shlex
import signal
import socket
import stat
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from veyro.bridges.base import BridgeContractError, validate_control_result
from veyro.models import (
    BridgeCapability,
    BridgeSource,
    BridgeStability,
    CapabilityAvailability,
    CapabilityDeclaration,
    CapabilitySet,
    ControlOutcome,
    ControlRequest,
    ControlResult,
    EventProvenance,
    SessionIdentity,
    SupervisionEvent,
    SupervisionEventType,
)
from veyro.supervision.broker import (
    BrokerStore,
    _create_private_file,
    _read_private_file,
    _replace_private_file,
)

CODEX_VERSION = "0.154.0"
BRIDGE_ID = "codex-hooks"
BRIDGE_VERSION = "1.0.0"
MAX_HOOK_BYTES = 4 * 1024 * 1024
MAX_FRAME_BYTES = 16 * 1024
HOOK_NAMES = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "Interrupt",
)
HookName = Literal[
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "Interrupt",
]


class CodexHookError(RuntimeError):
    pass


class HookMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: UUID
    cwd: str = Field(min_length=1, max_length=10000)
    hook_event_name: HookName
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    turn_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    tool_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    agent_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    tool_kind: Literal["shell", "file", "other"] | None = None


def sanitize_hook(raw: bytes) -> HookMetadata:
    if len(raw) > MAX_HOOK_BYTES:
        raise CodexHookError("hook input exceeds limit")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise CodexHookError("hook input must be an object")

    def digest(key: str) -> str | None:
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value or len(value) > 1000:
            raise CodexHookError("invalid hook identifier")
        return hashlib.sha256(value.encode()).hexdigest()

    tool_kind = None
    if "tool_name" in data:
        name = data["tool_name"]
        if not isinstance(name, str):
            raise CodexHookError("invalid tool name")
        tool_kind = (
            "shell"
            if name in {"shell", "shell_command", "exec_command"}
            else "file"
            if name in {"apply_patch", "read_file"}
            else "other"
        )
    return HookMetadata(
        session_id=data.get("session_id"),
        cwd=data.get("cwd"),
        hook_event_name=data.get("hook_event_name"),
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        turn_sha256=digest("turn_id"),
        tool_sha256=digest("tool_use_id"),
        agent_sha256=digest("agent_id"),
        tool_kind=tool_kind,
    )


def codex_hook_capabilities() -> CapabilitySet:
    observed = {
        BridgeCapability.OBSERVE_LIFECYCLE,
        BridgeCapability.OBSERVE_MESSAGES,
        BridgeCapability.OBSERVE_TOOLS,
        BridgeCapability.OBSERVE_APPROVALS,
    }
    return CapabilitySet(
        provider_id="codex",
        provider_version=CODEX_VERSION,
        bridge_id=BRIDGE_ID,
        bridge_version=BRIDGE_VERSION,
        declarations=tuple(
            CapabilityDeclaration(
                capability=capability,
                availability=(
                    CapabilityAvailability.SUPPORTED
                    if capability in observed
                    else CapabilityAvailability.UNSUPPORTED
                ),
                stability=BridgeStability.STABLE,
                evidence="Codex 0.154.0 hook schemas and implementations",
                detail=(
                    "Hook boundaries only, not execution outcomes or complete history. "
                    "No follow-up delivery, arbitrary steering, cancellation, approval replies, "
                    "or App Server RPC."
                ),
            )
            for capability in BridgeCapability
        ),
    )


async def _run_native(
    executable: Path,
    args: list[str],
    *,
    cwd: Path,
    codex_home: Path,
) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        str(executable),
        *args,
        cwd=cwd,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(5):
            assert process.stdout is not None
            try:
                await process.stdout.readexactly(MAX_FRAME_BYTES + 1)
            except asyncio.IncompleteReadError as error:
                output = error.partial
            else:
                raise CodexHookError("native response exceeds limit")
            return await process.wait(), output
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def verify_codex(executable: Path, repository: Path, codex_home: Path) -> None:
    status, output = await _run_native(
        executable,
        ["--version"],
        cwd=repository,
        codex_home=codex_home,
    )
    if status != 0 or output.strip() != f"codex-cli {CODEX_VERSION}".encode():
        raise CodexHookError("Codex version mismatch")


class CodexHookBridge:
    def __init__(self, store: BrokerStore) -> None:
        self.store = store
        self.identity = store.identity
        self.capabilities = store.capabilities
        self.closed = False
        self._changed = asyncio.Event()

    @property
    def last_event_sequence(self) -> int:
        return self.store.last_sequence

    def ingest(self, metadata: HookMetadata) -> SupervisionEvent:
        if self.closed:
            raise CodexHookError("bridge is closed")
        if str(metadata.session_id) != self.identity.provider_session_id or Path(
            metadata.cwd
        ).resolve() != Path(self.identity.repository):
            raise CodexHookError("hook identity mismatch")
        payload = metadata.model_dump(
            mode="json",
            exclude={
                "session_id",
                "cwd",
                "hook_event_name",
                "raw_sha256",
            },
            exclude_none=True,
        )
        payload["boundary_only"] = True
        # A hook boundary may be blocked by another hook or repeated after resume.
        event_type = SupervisionEventType.UNKNOWN
        if metadata.hook_event_name == "UserPromptSubmit" and metadata.agent_sha256 is None:
            event_type = SupervisionEventType.USER_PROMPT_SUBMITTED
        event = SupervisionEvent(
            session=self.identity,
            sequence=self.store.last_sequence + 1,
            event_type=event_type,
            payload=payload,
            provenance=EventProvenance(
                source=BridgeSource.HOOK,
                native_event_type=metadata.hook_event_name,
                raw_event_sha256=metadata.raw_sha256,
            ),
        )
        self.store.append_event(event)
        self._changed.set()
        return event

    async def events(self, *, after_sequence: int = 0) -> AsyncIterator[SupervisionEvent]:
        # This is local broker history, not native provider replay.
        while True:
            self._changed.clear()
            for event in self.store.replay_events(after_sequence=after_sequence, limit=64):
                after_sequence = event.sequence
                yield event
            if after_sequence < self.store.last_sequence:
                continue
            if self.closed:
                return
            await self._changed.wait()

    async def execute(self, request: ControlRequest) -> ControlResult:
        if request.session != self.identity:
            raise BridgeContractError("control session does not match bridge")
        result = ControlResult(
            veyro_session_id=request.session.veyro_session_id,
            provider_id=request.session.provider_id,
            command_id=request.command_id,
            action=request.intent.action,
            outcome=ControlOutcome.UNSUPPORTED,
            detail="Codex hook supervision is observation-only",
        )
        validate_control_result(request, result, self.capabilities)
        return result

    async def execute_if_current(
        self, request: ControlRequest, *, expected_sequence: int
    ) -> ControlResult | None:
        if self.last_event_sequence != expected_sequence:
            return None
        return await self.execute(request)

    async def close(self) -> None:
        self.closed = True
        self._changed.set()


class CodexHookListener:
    def __init__(self, repository: Path, codex_home: Path, executable: Path):
        self.repository = repository.resolve()
        self.codex_home = codex_home.resolve()
        self.executable = executable.resolve()
        self.capabilities = codex_hook_capabilities()
        self.bridges: dict[str, CodexHookBridge] = {}
        self._temporary: tempfile.TemporaryDirectory | None = None
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.Task] = set()
        self._writer_locks: list[int] = []
        self._token = secrets.token_urlsafe(32)
        self.connection_path: Path | None = None

    async def start(self) -> Path:
        if self._server is not None:
            raise CodexHookError("listener is already running")
        await verify_codex(self.executable, self.repository, self.codex_home)
        self._temporary = tempfile.TemporaryDirectory(prefix="fm-codex-", dir="/tmp")
        directory = Path(self._temporary.name)
        socket_path = directory / "hook.sock"
        try:
            self._server = await asyncio.start_unix_server(
                self._handle,
                path=socket_path,
                limit=MAX_FRAME_BYTES + 1,
            )
            socket_path.chmod(0o600)
            self.connection_path = directory / "connection.json"
            _create_private_file(
                self.connection_path,
                json.dumps(
                    {
                        "socket": str(socket_path),
                        "token": self._token,
                    }
                ).encode(),
            )
            return self.connection_path
        except BaseException:
            await self.close()
            raise

    def _ingest(self, metadata: HookMetadata) -> SupervisionEvent:
        if Path(metadata.cwd).resolve() != self.repository:
            raise CodexHookError("hook repository does not match listener")
        native_id = str(metadata.session_id)
        if native_id not in self.bridges:
            if len(self.bridges) >= 64:
                raise CodexHookError("listener session limit reached")
            scope = json.dumps([str(self.repository), str(self.codex_home), native_id])
            session_id = "codex-" + hashlib.sha256(scope.encode()).hexdigest()[:40]
            directory = self.repository / ".veyro/supervision" / session_id
            if directory.exists():
                store = BrokerStore.open(self.repository, session_id)
            else:
                identity = SessionIdentity(
                    veyro_session_id=session_id,
                    provider_id="codex",
                    provider_session_id=native_id,
                    repository=str(self.repository),
                    provider_version=CODEX_VERSION,
                    bridge_id=BRIDGE_ID,
                    bridge_version=BRIDGE_VERSION,
                )
                store = BrokerStore.create(self.repository, identity, self.capabilities)
            descriptor = os.open(
                store.directory / "hook-writer.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            try:
                lock_stat = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(lock_stat.st_mode)
                    or lock_stat.st_uid != os.getuid()
                    or lock_stat.st_mode & 0o077
                ):
                    raise CodexHookError("unsafe writer lock")
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                store = BrokerStore.open(self.repository, session_id)
                if (
                    store.identity.provider_session_id != native_id
                    or store.identity.provider_id != "codex"
                    or store.identity.bridge_version != BRIDGE_VERSION
                    or store.identity.provider_version != CODEX_VERSION
                    or store.identity.bridge_id != BRIDGE_ID
                    or store.identity.repository != str(self.repository)
                    or tuple(
                        d
                        for d in store.capabilities.declarations
                        if d.capability is not BridgeCapability.QUEUE_FOLLOW_UP
                    )
                    != tuple(
                        d
                        for d in self.capabilities.declarations
                        if d.capability is not BridgeCapability.QUEUE_FOLLOW_UP
                    )
                ):
                    raise CodexHookError("persisted hook session is incompatible")
                _replace_private_file(
                    store.capabilities_path,
                    self.capabilities.model_dump_json(indent=2).encode(),
                )
                store.capabilities = self.capabilities
            except BaseException:
                os.close(descriptor)
                raise
            self._writer_locks.append(descriptor)
            self.bridges[native_id] = CodexHookBridge(store)
        return self.bridges[native_id].ingest(metadata)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        if len(self._clients) >= 64:
            writer.close()
            return
        self._clients.add(task)
        try:
            async with asyncio.timeout(1):
                frame = await reader.readline()
                if len(frame) > MAX_FRAME_BYTES:
                    raise CodexHookError("frame too large")
                request = json.loads(frame)
                if not isinstance(request, dict) or not isinstance(request.get("token"), str):
                    raise CodexHookError("invalid frame")
                if not hmac.compare_digest(request["token"], self._token):
                    raise CodexHookError("unauthorized")
                event = self._ingest(HookMetadata.model_validate(request.get("metadata")))
                writer.write(json.dumps({"ok": True, "sequence": event.sequence}).encode() + b"\n")
                await writer.drain()
        except (ValueError, RuntimeError, OSError, TimeoutError):
            # Neither provider content nor authentication errors enter Codex's result channel.
            pass
        finally:
            writer.close()
            self._clients.discard(task)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        tasks = list(self._clients)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for bridge in self.bridges.values():
            await bridge.close()
        for descriptor in self._writer_locks:
            os.close(descriptor)
        self._writer_locks.clear()
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


def publish_hook(connection: Path, raw: bytes) -> None:
    metadata = sanitize_hook(raw)
    for path in (connection.parent, connection):
        mode = path.lstat()
        if path.is_symlink() or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
            raise CodexHookError("hook connection must be private and owned by this user")
    config = json.loads(_read_private_file(connection))
    socket_path = Path(config["socket"])
    mode = socket_path.lstat()
    if (
        socket_path.parent != connection.parent
        or not stat.S_ISSOCK(mode.st_mode)
        or mode.st_uid != os.getuid()
        or mode.st_mode & 0o077
    ):
        raise CodexHookError("unsafe hook socket")
    frame = json.dumps(
        {"token": config["token"], "metadata": metadata.model_dump(mode="json")}
    ).encode()
    if len(frame) > MAX_FRAME_BYTES - 1:
        raise CodexHookError("metadata frame too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(0.5)
        client.connect(str(socket_path))
        client.sendall(frame + b"\n")
        if not json.loads(client.recv(1024)).get("ok"):
            raise CodexHookError("hook publication failed")


def hook_configuration(connection: Path) -> dict:
    command = shlex.join(
        [
            sys.executable,
            "-I",
            "-m",
            "veyro.bridges.codex_hooks",
            "publish",
            "--connection",
            str(connection),
        ]
    )
    return {
        "hooks": {
            name: [{"hooks": [{"type": "command", "command": command, "timeout": 2}]}]
            for name in HOOK_NAMES
        }
    }


def _validate_journal_identity(identity: SessionIdentity) -> None:
    if (
        identity.provider_id != "codex"
        or identity.bridge_id != BRIDGE_ID
        or identity.bridge_version != BRIDGE_VERSION
        or identity.provider_version != CODEX_VERSION
    ):
        raise CodexHookError("unsupported Codex hook journal")
    UUID(identity.provider_session_id or "")


def _journal_candidate(identity: SessionIdentity):
    from veyro.models.attachment import SessionCandidate

    _validate_journal_identity(identity)
    return SessionCandidate(
        provider_id="codex",
        selector=identity.veyro_session_id,
        native_session_id=identity.provider_session_id,
        repository=identity.repository,
        provider_version=CODEX_VERSION,
        mode="local_hook_journal",
        native_liveness="unknown",
    )


def discover_hook_journals(repository: Path, *, limit: int = 100):
    from veyro.models.attachment import SessionDiscovery
    from veyro.supervision.journal import JournalError, ReadOnlyJournal, discover_journal_ids

    ids, truncated = discover_journal_ids(repository, limit=1000)
    sessions = []
    skipped = 0
    for session_id in ids:
        journal = None
        try:
            journal = ReadOnlyJournal.open(repository, session_id)
            if journal.identity.provider_id != "codex":
                continue
            sessions.append(_journal_candidate(journal.identity))
        except (ValueError, OSError, JournalError, CodexHookError):
            skipped += 1
        finally:
            if journal is not None:
                journal.close()
        if len(sessions) > limit:
            truncated = True
            break
    return SessionDiscovery(
        provider_id="codex",
        detail="Existing Veyro hook journals only; native liveness is unknown.",
        sessions=tuple(sessions[:limit]),
        skipped=skipped,
        truncated=truncated,
    )


class CodexJournalObservation:
    def __init__(self, repository: Path, session_id: str, *, follow: bool):
        from veyro.supervision.journal import ReadOnlyJournal

        self._journal = ReadOnlyJournal.open(repository, session_id)
        try:
            self.identity = self._journal.identity
            self.candidate = _journal_candidate(self.identity)
        except BaseException:
            self._journal.close()
            raise
        self.capabilities = codex_hook_capabilities()
        self._follow = follow
        self._closed = False

    async def seek_after(self, after_sequence: int) -> None:
        while self._journal.sequence < after_sequence:
            await asyncio.sleep(0)
            page = self._journal.read_page(limit=min(64, after_sequence - self._journal.sequence))
            if not page:
                raise CodexHookError("cursor exceeds complete journal history")
            for event in page:
                self._validate_event(event)

    @property
    def incomplete_tail(self) -> bool:
        return self._journal.incomplete_tail

    @staticmethod
    def _validate_event(event: SupervisionEvent) -> None:
        if event.provenance.source is not BridgeSource.HOOK:
            raise CodexHookError("non-hook record in Codex journal")
        payload = dict(event.payload)
        if payload.pop("boundary_only", None) is not True:
            raise CodexHookError("invalid Codex boundary record")
        if set(payload) - {"turn_sha256", "tool_sha256", "agent_sha256", "tool_kind"}:
            raise CodexHookError("unexpected Codex journal metadata")
        if event.provenance.native_event_id is not None:
            raise CodexHookError("unexpected Codex native event identifier")
        UUID(event.event_id)
        metadata = HookMetadata(
            session_id=event.session.provider_session_id,
            cwd=event.session.repository,
            hook_event_name=event.provenance.native_event_type,
            raw_sha256=event.provenance.raw_event_sha256,
            **payload,
        )
        expected = (
            SupervisionEventType.USER_PROMPT_SUBMITTED
            if metadata.hook_event_name == "UserPromptSubmit" and metadata.agent_sha256 is None
            else SupervisionEventType.UNKNOWN
        )
        if event.event_type is not expected:
            raise CodexHookError("Codex journal event has unsupported semantics")

    async def events(self) -> AsyncIterator[SupervisionEvent]:
        while not self._closed:
            await asyncio.sleep(0)
            page = self._journal.read_page(limit=64)
            for event in page:
                self._validate_event(event)
                yield event
            if page:
                continue
            if not self._follow:
                return
            await asyncio.sleep(0.1)

    async def close(self) -> None:
        self._closed = True
        self._journal.close()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Observe Codex hooks without changing native output"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("--connection", type=Path, required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--repository", type=Path, required=True)
    serve.add_argument("--codex-home", type=Path, required=True)
    serve.add_argument("--executable", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "publish":
        try:
            publish_hook(args.connection, sys.stdin.buffer.read(MAX_HOOK_BYTES + 1))
        except (ValueError, RuntimeError, OSError, KeyError):
            # Observe-only hook outages must never become provider feedback or denial.
            pass
        return 0

    async def run() -> None:
        listener = CodexHookListener(args.repository, args.codex_home, args.executable)
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stopped.set)
        try:
            connection = await listener.start()
            print(json.dumps(hook_configuration(connection), indent=2), flush=True)
            await stopped.wait()
        finally:
            await listener.close()

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
