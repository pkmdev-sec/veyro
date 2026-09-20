from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from veyro.bridges.base import required_capability, validate_connection, validate_control_result
from veyro.models import (
    BridgeCapability,
    BridgeSource,
    BridgeStability,
    CapabilityAvailability,
    CapabilityDeclaration,
    CapabilitySet,
    ControlAction,
    ControlOutcome,
    ControlRequest,
    ControlResult,
    EventProvenance,
    EventSensitivity,
    SessionIdentity,
    SupervisionEvent,
    SupervisionEventType,
)
from veyro.models.attachment import AttachmentHistory, SessionCandidate, SessionDiscovery

PRIME_AGENT_VERSION = "0.9.5"
DAEMON_PROTOCOL_NAME = "prime-agent.daemon"
DAEMON_PROTOCOL_VERSION = 7
DAEMON_SCHEMA_REVISION = 29
DAEMON_SCHEMA_ID = "protocol-7-schema-29-a5c9d20f8b13"
BRIDGE_ID = "prime-agent-daemon"
BRIDGE_VERSION = f"{DAEMON_PROTOCOL_VERSION}.{DAEMON_SCHEMA_REVISION}"
_CLIENT_CAPABILITIES = [
    "attach_snapshot",
    "event_sequence",
    "slim_attach",
    "chunked_snapshot",
]
MAX_DAEMON_LINE_BYTES = 4 * 1024 * 1024


class PrimeDaemonError(RuntimeError):
    pass


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _assert_private_socket(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode):
        raise PrimeDaemonError(f"Prime Agent daemon path is not a socket: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PrimeDaemonError("Prime Agent daemon socket is not owned by the current user")
    if info.st_mode & 0o077:
        raise PrimeDaemonError("Prime Agent daemon socket permits group or other access")


class PrimeDaemonClient:
    def __init__(self, socket_path: Path, *, timeout: float = 30.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self.hello: dict[str, Any] | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._outbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._request_number = 0
        self._client_id = f"veyro:{uuid4().hex}"

    async def connect(self) -> dict[str, Any]:
        _assert_private_socket(self.socket_path)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self.socket_path, limit=MAX_DAEMON_LINE_BYTES),
            self.timeout,
        )
        hello_line = await asyncio.wait_for(self._reader.readline(), self.timeout)
        if not hello_line:
            raise PrimeDaemonError("Prime Agent daemon closed before its hello")
        try:
            hello = json.loads(hello_line)
        except json.JSONDecodeError as error:
            raise PrimeDaemonError("Prime Agent daemon sent an invalid hello") from error
        self._validate_hello(hello)
        self.hello = hello
        self._reader_task = asyncio.create_task(self._read_messages())
        return hello

    @staticmethod
    def _validate_hello(hello: object) -> None:
        if not isinstance(hello, dict) or hello.get("type") != "daemon_hello":
            raise PrimeDaemonError("Prime Agent daemon did not send daemon_hello")
        if hello.get("protocol") != {
            "name": DAEMON_PROTOCOL_NAME,
            "version": DAEMON_PROTOCOL_VERSION,
        }:
            raise PrimeDaemonError("unsupported Prime Agent daemon protocol")
        if hello.get("schemaRevision") != DAEMON_SCHEMA_REVISION:
            raise PrimeDaemonError("unsupported Prime Agent daemon schema revision")
        if hello.get("schemaId") != DAEMON_SCHEMA_ID:
            raise PrimeDaemonError("unsupported Prime Agent daemon schema identity")
        if hello.get("appVersion") != PRIME_AGENT_VERSION:
            raise PrimeDaemonError("unsupported Prime Agent version")

    async def request(self, command: Mapping[str, object]) -> dict[str, Any]:
        if self._writer is None or self.hello is None:
            raise PrimeDaemonError("Prime Agent daemon client is not connected")
        self._request_number += 1
        command_id = f"veyro_{self._request_number}"
        body = {**command, "id": command_id}
        envelope = {
            "type": "command",
            "id": command_id,
            "protocol": {"name": DAEMON_PROTOCOL_NAME, "version": DAEMON_PROTOCOL_VERSION},
            "clientId": self._client_id,
            "command": body,
        }
        future = asyncio.get_running_loop().create_future()
        self._pending[command_id] = future
        self._writer.write(json.dumps(envelope, separators=(",", ":")).encode() + b"\n")
        await self._writer.drain()
        try:
            response = await asyncio.wait_for(future, self.timeout)
        finally:
            self._pending.pop(command_id, None)
        if not response.get("success"):
            raise PrimeDaemonError(str(response.get("error", "daemon command failed")))
        if command.get("type") not in {
            "attach",
            "get_state",
            "get_connection_state",
            "get_messages",
            "list",
            "wait_for_idle",
        }:
            await self._acknowledge(command_id)
        return response

    async def discover(self, repository: Path, *, limit: int = 100) -> SessionDiscovery:
        if not 1 <= limit <= 1000:
            raise PrimeDaemonError("invalid discovery limit")
        repository = await asyncio.to_thread(repository.resolve)
        response = await self.request(
            {
                "type": "list",
                "cwd": str(repository),
                "includeClientOwned": True,
            }
        )
        data = response.get("data")
        sessions = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(sessions, list):
            raise PrimeDaemonError("invalid Prime session list")
        candidates = []
        skipped = 0
        for row in sessions:
            try:
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get("cwd"), str)
                    or not Path(row["cwd"]).is_absolute()
                ):
                    raise ValueError("invalid session metadata")
                if await asyncio.to_thread(Path(row["cwd"]).resolve) != repository:
                    continue
                candidates.append(
                    SessionCandidate(
                        provider_id="prime-agent",
                        selector=row.get("activeSessionId"),
                        native_session_id=row.get("activeSessionId"),
                        repository=str(repository),
                        provider_version=PRIME_AGENT_VERSION,
                        mode="native_daemon",
                        native_liveness="loaded",
                    )
                )
            except (ValueError, OSError):
                skipped += 1
            if len(candidates) > limit:
                break
        return SessionDiscovery(
            provider_id="prime-agent",
            detail="Loaded daemon sessions only; saved sessions are not resumed.",
            sessions=tuple(candidates[:limit]),
            truncated=len(candidates) > limit,
            skipped=skipped,
        )

    async def next_outbound(self) -> dict[str, Any]:
        message = await self._outbound.get()
        if message is None:
            raise PrimeDaemonError("Prime Agent observation stream closed")
        return message

    async def _acknowledge(self, command_id: str) -> None:
        if self._writer is None:
            return
        self._request_number += 1
        ack_id = f"veyro_ack_{self._request_number}"
        command = {"id": ack_id, "type": "ack_result", "commandId": command_id}
        envelope = {
            "type": "command",
            "id": ack_id,
            "protocol": {"name": DAEMON_PROTOCOL_NAME, "version": DAEMON_PROTOCOL_VERSION},
            "clientId": self._client_id,
            "command": command,
        }
        self._writer.write(json.dumps(envelope, separators=(",", ":")).encode() + b"\n")
        await self._writer.drain()

    async def _read_messages(self) -> None:
        assert self._reader is not None
        error = PrimeDaemonError("Prime Agent daemon connection closed")
        try:
            while line := await self._reader.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("daemon message must be a JSON object")
                request_id = message.get("id")
                if isinstance(request_id, str) and message.get("type") == "response":
                    pending = self._pending.get(request_id)
                    if pending is not None and not pending.done():
                        pending.set_result(message)
                        continue
                await self._outbound.put(message)
        except (OSError, ValueError) as cause:
            error = PrimeDaemonError(f"Prime Agent daemon transport failed: {type(cause).__name__}")
        finally:
            writer = self._writer
            self._writer = None
            if writer is not None:
                writer.close()
            self._outbound.put_nowait(None)
            for pending in self._pending.values():
                if not pending.done():
                    pending.set_exception(error)
            if writer is not None:
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None


def _declaration(
    capability: BridgeCapability,
    availability: CapabilityAvailability,
    evidence: str,
) -> CapabilityDeclaration:
    return CapabilityDeclaration(
        capability=capability,
        availability=availability,
        stability=BridgeStability.INTERNAL
        if availability is CapabilityAvailability.SUPPORTED
        else None,
        evidence=evidence,
    )


def prime_daemon_capabilities() -> CapabilitySet:
    supported = {
        BridgeCapability.OBSERVE_LIFECYCLE,
        BridgeCapability.OBSERVE_MESSAGES,
        BridgeCapability.OBSERVE_TOOLS,
        BridgeCapability.REPLAY_EVENTS,
        BridgeCapability.QUEUE_FOLLOW_UP,
        BridgeCapability.STEER_ACTIVE_TURN,
        BridgeCapability.INTERRUPT_TURN,
        BridgeCapability.STOP_SESSION,
        BridgeCapability.ATTACH_EXISTING,
    }
    evidence = (
        f"Prime Agent {PRIME_AGENT_VERSION} daemon protocol "
        f"{DAEMON_PROTOCOL_VERSION}, schema {DAEMON_SCHEMA_REVISION}"
    )
    return CapabilitySet(
        provider_id="prime-agent",
        provider_version=PRIME_AGENT_VERSION,
        bridge_id=BRIDGE_ID,
        bridge_version=BRIDGE_VERSION,
        declarations=tuple(
            _declaration(
                capability,
                CapabilityAvailability.SUPPORTED
                if capability in supported
                else CapabilityAvailability.UNSUPPORTED,
                evidence
                if capability in supported
                else "daemon schema has no provider-neutral approval reply",
            )
            for capability in BridgeCapability
        ),
    )


class PrimeAgentDaemonBridge:
    def __init__(
        self,
        *,
        client: PrimeDaemonClient,
        identity: SessionIdentity,
        active_session_id: str,
    ) -> None:
        self._client = client
        self._identity = identity
        self._active_session_id = active_session_id
        self._capabilities = prime_daemon_capabilities()
        validate_connection(identity, self._capabilities)
        self._events: list[SupervisionEvent] = []
        self._changed = asyncio.Event()
        self._closed = False
        self._pump: asyncio.Task[None] | None = None
        self._active_turn_id: str | None = None
        self.attachment_history = AttachmentHistory(
            mode="snapshot_metadata_only",
            detail="No transcript or native replay requested.",
        )

    @classmethod
    async def attach(
        cls,
        *,
        socket_path: Path,
        active_session_id: str,
        veyro_session_id: str,
        repository: Path,
        resume_cursor: Mapping[str, object] | None = None,
    ) -> PrimeAgentDaemonBridge:
        client = PrimeDaemonClient(socket_path)
        try:
            await client.connect()
            return await cls.attach_connected(
                client=client,
                active_session_id=active_session_id,
                veyro_session_id=veyro_session_id,
                repository=repository,
                resume_cursor=resume_cursor,
            )
        except BaseException:
            await client.close()
            raise

    @classmethod
    async def attach_connected(
        cls,
        *,
        client: PrimeDaemonClient,
        active_session_id: str,
        veyro_session_id: str,
        repository: Path,
        resume_cursor: Mapping[str, object] | None = None,
    ) -> PrimeAgentDaemonBridge:
        if client.hello is None:
            raise PrimeDaemonError("Prime Agent daemon client is not connected")
        resolved_repository = await asyncio.to_thread(repository.resolve)
        identity = SessionIdentity(
            veyro_session_id=veyro_session_id,
            provider_id="prime-agent",
            provider_session_id=active_session_id,
            repository=str(resolved_repository),
            provider_version=PRIME_AGENT_VERSION,
            bridge_id=BRIDGE_ID,
            bridge_version=BRIDGE_VERSION,
        )
        bridge = cls(client=client, identity=identity, active_session_id=active_session_id)
        command: dict[str, object] = {
            "type": "attach",
            "activeSessionId": active_session_id,
            "capabilities": _CLIENT_CAPABILITIES,
        }
        if resume_cursor is not None:
            command["resumeCursor"] = dict(resume_cursor)
        response = await client.request(command)
        data = response.get("data")
        if not isinstance(data, dict) or data.get("activeSessionId") != active_session_id:
            raise PrimeDaemonError("Prime Agent attach returned the wrong session")
        snapshot = data.get("snapshot")
        summary = snapshot.get("summary") if isinstance(snapshot, dict) else None
        directory = summary.get("cwd") if isinstance(summary, dict) else None
        if (
            not isinstance(directory, str)
            or not Path(directory).is_absolute()
            or await asyncio.to_thread(Path(directory).resolve) != resolved_repository
        ):
            raise PrimeDaemonError("Prime Agent attach repository mismatch")
        count = summary.get("messageCount")
        sequence = data.get("lastEventSequence")
        replay = data.get("replay")
        replay_status = replay.get("status") if isinstance(replay, dict) else None
        bridge.attachment_history = AttachmentHistory(
            mode="snapshot_metadata_only",
            detail=(
                "Snapshot counts only; transcript content is discarded. "
                + (
                    "Native replay cursor requested."
                    if resume_cursor is not None
                    else "Native replay not requested."
                )
            ),
            snapshot_message_count=count if type(count) is int and count >= 0 else None,
            native_sequence=sequence if type(sequence) is int and sequence >= 0 else None,
            native_replay_status=(
                replay_status
                if replay_status in {"complete", "partial", "unavailable"}
                else "unknown"
            ),
        )
        bridge._append_event(
            native_type="session_attached",
            event_type=SupervisionEventType.SESSION_STARTED,
            raw=response,
            replayed=False,
            payload={"attached": True},
        )
        bridge._pump = asyncio.create_task(bridge._pump_outbound())
        return bridge

    @property
    def last_event_sequence(self) -> int:
        return len(self._events)

    @property
    def identity(self) -> SessionIdentity:
        return self._identity

    @property
    def capabilities(self) -> CapabilitySet:
        return self._capabilities

    async def events(self, *, after_sequence: int = 0) -> AsyncIterator[SupervisionEvent]:
        cursor = after_sequence
        while True:
            available = [event for event in self._events if event.sequence > cursor]
            for event in available:
                cursor = event.sequence
                yield event
            if self._closed:
                return
            self._changed.clear()
            if any(event.sequence > cursor for event in self._events):
                continue
            await self._changed.wait()

    async def execute(self, request: ControlRequest) -> ControlResult:
        if self._closed:
            return self._result(
                request, ControlOutcome.FAILED, "Prime Agent observation stream is closed"
            )
        if request.session != self.identity:
            return self._result(
                request, ControlOutcome.REJECTED, "control belongs to another session"
            )
        capability = required_capability(request.intent.action)
        if not self.capabilities.supports(capability):
            result = self._result(request, ControlOutcome.UNSUPPORTED, "control is not supported")
            validate_control_result(request, result, self.capabilities)
            return result
        command: dict[str, object]
        if request.intent.action is ControlAction.QUEUE_FOLLOW_UP:
            command = {
                "type": "follow_up",
                "activeSessionId": self._active_session_id,
                "message": request.intent.message,
            }
        elif request.intent.action is ControlAction.STEER_ACTIVE_TURN:
            command = {
                "type": "steer",
                "activeSessionId": self._active_session_id,
                "message": request.intent.message,
            }
        elif request.intent.action is ControlAction.INTERRUPT_TURN:
            command = {"type": "abort", "activeSessionId": self._active_session_id}
        elif request.intent.action is ControlAction.STOP_SESSION:
            command = {"type": "kill", "activeSessionId": self._active_session_id}
        else:
            result = self._result(
                request, ControlOutcome.UNSUPPORTED, "approval replies are not supported"
            )
            validate_control_result(request, result, self.capabilities)
            return result
        try:
            response = await self._client.request(command)
        except PrimeDaemonError as error:
            result = self._result(request, ControlOutcome.FAILED, str(error))
        else:
            result = self._result(
                request,
                ControlOutcome.EXECUTED,
                provider_command_id=response.get("id")
                if isinstance(response.get("id"), str)
                else None,
            )
        validate_control_result(request, result, self.capabilities)
        return result

    def _result(
        self,
        request: ControlRequest,
        outcome: ControlOutcome,
        detail: str = "",
        provider_command_id: str | None = None,
    ) -> ControlResult:
        return ControlResult(
            veyro_session_id=self.identity.veyro_session_id,
            provider_id=self.identity.provider_id,
            command_id=request.command_id,
            action=request.intent.action,
            outcome=outcome,
            detail=detail,
            provider_command_id=provider_command_id,
        )

    async def _pump_outbound(self) -> None:
        try:
            while True:
                message = await self._client.next_outbound()
                if message.get("activeSessionId") != self._active_session_id:
                    continue
                self._normalize(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._append_event(
                native_type="bridge_error",
                event_type=SupervisionEventType.NORMALIZATION_FAILED,
                raw={"error_type": type(error).__name__},
                replayed=False,
                payload={"error_type": type(error).__name__},
            )
        finally:
            self._closed = True
            self._changed.set()

    def _normalize(self, message: dict[str, Any]) -> None:
        outer_type = str(message.get("type", "unknown"))
        native = message.get("event") if outer_type == "session_event" else message
        native_type = (
            str(native.get("type", outer_type)) if isinstance(native, dict) else outer_type
        )
        event_type, payload = _map_native_event(outer_type, native_type, native)
        meta = message.get("meta")
        native_event_id = (
            meta.get("id") if isinstance(meta, dict) and isinstance(meta.get("id"), str) else None
        )
        if event_type is SupervisionEventType.TURN_STARTED:
            self._active_turn_id = native_event_id or f"prime-turn-{len(self._events) + 1}"
            payload["turn_id"] = self._active_turn_id
        elif event_type is SupervisionEventType.TURN_COMPLETED:
            payload["turn_id"] = self._active_turn_id or native_event_id or "prime-turn-unknown"
            self._active_turn_id = None
        replayed = isinstance(meta, dict) and meta.get("replayed") is True
        self._append_event(
            native_type=native_type,
            event_type=event_type,
            raw=message,
            replayed=replayed,
            native_event_id=native_event_id,
            payload=payload,
        )

    def _append_event(
        self,
        *,
        native_type: str,
        event_type: SupervisionEventType,
        raw: object,
        replayed: bool,
        payload: dict[str, object],
        native_event_id: str | None = None,
    ) -> None:
        self._events.append(
            SupervisionEvent(
                session=self.identity,
                sequence=len(self._events) + 1,
                event_type=event_type,
                payload=payload,
                provenance=EventProvenance(
                    source=BridgeSource.DAEMON,
                    native_event_type=native_type,
                    native_event_id=native_event_id,
                    observed_at=datetime.now(UTC),
                    raw_event_sha256=_canonical_digest(raw),
                    replayed=replayed,
                ),
                sensitivity=EventSensitivity.METADATA,
            )
        )
        self._changed.set()

    async def close(self) -> None:
        self._closed = True
        if self._pump is not None:
            self._pump.cancel()
            await asyncio.gather(self._pump, return_exceptions=True)
        try:
            await asyncio.wait_for(
                self._client.request(
                    {"type": "detach", "activeSessionId": self._active_session_id}
                ),
                timeout=2,
            )
        except (TimeoutError, PrimeDaemonError, OSError):
            pass
        await self._client.close()
        self._changed.set()


def _map_native_event(
    outer_type: str, native_type: str, native: object
) -> tuple[SupervisionEventType, dict[str, object]]:
    data = native if isinstance(native, dict) else {}
    if outer_type == "session_closed":
        reason = str(data.get("reason", "unknown"))
        event_type = (
            SupervisionEventType.SESSION_COMPLETED
            if reason == "completed"
            else SupervisionEventType.SESSION_FAILED
        )
        return event_type, {"reason": reason}
    mapping = {
        "agent_end": SupervisionEventType.SESSION_IDLE,
        "turn_start": SupervisionEventType.TURN_STARTED,
        "turn_end": SupervisionEventType.TURN_COMPLETED,
        "tool_execution_start": SupervisionEventType.TOOL_STARTED,
        "tool_execution_end": SupervisionEventType.TOOL_COMPLETED,
        "bash_start": SupervisionEventType.TOOL_STARTED,
        "bash_end": SupervisionEventType.TOOL_COMPLETED,
    }
    if native_type == "message_end":
        message = data.get("message")
        role = message.get("role") if isinstance(message, dict) else None
        if role == "user":
            return SupervisionEventType.USER_PROMPT_SUBMITTED, {"role": "user"}
        if role == "assistant":
            return SupervisionEventType.AGENT_MESSAGE_COMPLETED, {"role": "assistant"}
    payload: dict[str, object] = {}
    if native_type.startswith("tool_execution_"):
        tool_call_id = data.get("toolCallId")
        if isinstance(tool_call_id, str) and tool_call_id:
            payload["tool_call_id"] = tool_call_id
        if isinstance(data.get("toolName"), str):
            payload["tool_name"] = data["toolName"]
        if native_type == "tool_execution_end":
            payload["success"] = data.get("isError") is not True
    elif native_type.startswith("bash_"):
        run_id = data.get("runId")
        payload["tool_call_id"] = run_id if isinstance(run_id, str) and run_id else "bash"
        payload["tool_name"] = "bash"
        if native_type == "bash_end":
            payload["success"] = data.get("exitCode") == 0
    return mapping.get(native_type, SupervisionEventType.UNKNOWN), payload
