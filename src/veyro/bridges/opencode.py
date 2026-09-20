from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from veyro.bridges.base import required_capability, validate_connection, validate_control_result
from veyro.models import (
    ApprovalDecision,
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
from veyro.models.attachment import SessionCandidate, SessionDiscovery

OPENCODE_VERSION = "1.18.30"
OPENCODE_API_VERSION = "1.0.0"
BRIDGE_ID = "opencode-server"
BRIDGE_VERSION = OPENCODE_API_VERSION
MAX_HTTP_BODY_BYTES = 4 * 1024 * 1024
MAX_SSE_LINE_BYTES = 4 * 1024 * 1024


class OpenCodeError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str):
        return None


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validated_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "http":
        raise OpenCodeError("OpenCode server URL must use HTTP on loopback")
    if parsed.username is not None or parsed.password is not None:
        raise OpenCodeError("OpenCode server URL must not contain credentials")
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError as error:
        raise OpenCodeError("OpenCode server URL must use a numeric loopback address") from error
    if not address.is_loopback:
        raise OpenCodeError("OpenCode server URL must use a loopback address")
    if parsed.port is None:
        raise OpenCodeError("OpenCode server URL must include a port")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise OpenCodeError("OpenCode server URL must not include a path, query, or fragment")
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"http://{host}:{parsed.port}"


class OpenCodeClient:
    def __init__(
        self,
        base_url: str,
        *,
        username: str,
        password: str,
        timeout: float = 30.0,
    ) -> None:
        if not username or not password:
            raise OpenCodeError("OpenCode server credentials must be nonempty")
        if ":" in username:
            raise OpenCodeError("OpenCode server username must not contain ':'")
        self.base_url = _validated_base_url(base_url)
        self.timeout = timeout
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._authorization = f"Basic {token}"
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._stream: Any | None = None

    @classmethod
    def from_environment(cls, server: str, *, timeout: float = 5) -> OpenCodeClient:
        username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
        password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
        if not password:
            raise OpenCodeError("OPENCODE_SERVER_PASSWORD is required in the environment")
        return cls(server, username=username, password=password, timeout=timeout)

    async def health(self) -> dict[str, object]:
        value = await self.request_json("GET", "/global/health")
        if not isinstance(value, dict):
            raise OpenCodeError("OpenCode health response is not an object")
        return value

    async def session(self, session_id: str, *, directory: Path) -> dict[str, object]:
        path = f"/session/{urllib.parse.quote(session_id, safe='')}"
        value = await self.request_json("GET", path, directory=directory)
        if not isinstance(value, dict):
            raise OpenCodeError("OpenCode session response is not an object")
        return value

    async def discover(self, repository: Path, *, limit: int = 100) -> SessionDiscovery:
        if not 1 <= limit <= 1000:
            raise OpenCodeError("invalid discovery limit")
        if await self.health() != {"healthy": True, "version": OPENCODE_VERSION}:
            raise OpenCodeError("unsupported OpenCode server version")
        repository = await asyncio.to_thread(repository.resolve)
        sessions = await self.request_json(
            "GET", f"/session?limit={limit + 1}", directory=repository
        )
        if not isinstance(sessions, list):
            raise OpenCodeError("invalid OpenCode session list")
        candidates = []
        skipped = 0
        for row in sessions[: limit + 1]:
            try:
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get("directory"), str)
                    or not Path(row["directory"]).is_absolute()
                ):
                    raise ValueError("invalid session metadata")
                if await asyncio.to_thread(Path(row["directory"]).resolve) != repository:
                    continue
                if row.get("version") != OPENCODE_VERSION:
                    raise ValueError("unsupported session version")
                candidates.append(
                    SessionCandidate(
                        provider_id="opencode",
                        selector=row.get("id"),
                        native_session_id=row.get("id"),
                        repository=str(repository),
                        provider_version=OPENCODE_VERSION,
                        mode="native_server",
                        native_liveness="unknown",
                    )
                )
            except (ValueError, OSError):
                skipped += 1
        return SessionDiscovery(
            provider_id="opencode",
            detail="Server session metadata; active native TUI status is unknown.",
            sessions=tuple(candidates[:limit]),
            truncated=len(sessions) > limit,
            skipped=skipped,
        )

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        body: object | None = None,
        directory: Path | None = None,
        expected_status: tuple[int, ...] = (200,),
    ) -> object | None:
        return await asyncio.to_thread(
            self._request_json,
            method,
            path,
            body,
            directory,
            expected_status,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        body: object | None,
        directory: Path | None,
        expected_status: tuple[int, ...],
    ) -> object | None:
        request = self._request(method, path, body=body, directory=directory)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status not in expected_status:
                    raise OpenCodeError(f"OpenCode returned HTTP {response.status}")
                raw = response.read(MAX_HTTP_BODY_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise OpenCodeError(f"OpenCode returned HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise OpenCodeError("OpenCode server request failed") from error
        if len(raw) > MAX_HTTP_BODY_BYTES:
            raise OpenCodeError("OpenCode response exceeded the size limit")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OpenCodeError("OpenCode returned invalid JSON") from error

    async def events(self, *, directory: Path) -> AsyncIterator[dict[str, Any]]:
        request = self._request("GET", "/event", directory=directory, accept="text/event-stream")
        try:
            response = await asyncio.to_thread(self._opener.open, request, None, self.timeout)
        except urllib.error.HTTPError as error:
            raise OpenCodeError(f"OpenCode event stream returned HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise OpenCodeError("OpenCode event stream connection failed") from error
        self._stream = response
        try:
            if response.status != 200:
                raise OpenCodeError(f"OpenCode event stream returned HTTP {response.status}")
            content_type = response.headers.get_content_type()
            if content_type != "text/event-stream":
                raise OpenCodeError("OpenCode event stream returned the wrong content type")
            while True:
                line = await asyncio.to_thread(response.readline, MAX_SSE_LINE_BYTES + 1)
                if not line:
                    raise OpenCodeError("OpenCode event stream closed")
                if len(line) > MAX_SSE_LINE_BYTES:
                    raise OpenCodeError("OpenCode SSE record exceeded the size limit")
                if not line.startswith(b"data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise OpenCodeError("OpenCode sent an invalid SSE event") from error
                if not isinstance(event, dict):
                    raise OpenCodeError("OpenCode SSE event is not an object")
                yield event
        finally:
            await asyncio.to_thread(response.close)
            if self._stream is response:
                self._stream = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: object | None = None,
        directory: Path | None = None,
        accept: str = "application/json",
    ) -> urllib.request.Request:
        if not path.startswith("/") or path.startswith("//"):
            raise OpenCodeError("OpenCode request path must be absolute")
        query = urllib.parse.urlencode({"directory": str(directory)}) if directory else ""
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}{'&' if '?' in path else '?'}{query}"
        data = None
        headers = {"Accept": accept, "Authorization": self._authorization}
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        return urllib.request.Request(url, data=data, headers=headers, method=method)

    async def close(self) -> None:
        if self._stream is not None:
            await asyncio.to_thread(self._stream.close)
            self._stream = None


def _declaration(
    capability: BridgeCapability,
    availability: CapabilityAvailability,
    evidence: str,
    *,
    stability: BridgeStability | None = None,
) -> CapabilityDeclaration:
    return CapabilityDeclaration(
        capability=capability,
        availability=availability,
        stability=stability if availability is CapabilityAvailability.SUPPORTED else None,
        evidence=evidence,
    )


def opencode_server_capabilities() -> CapabilitySet:
    supported = {
        BridgeCapability.OBSERVE_LIFECYCLE,
        BridgeCapability.OBSERVE_MESSAGES,
        BridgeCapability.OBSERVE_TOOLS,
        BridgeCapability.OBSERVE_APPROVALS,
        BridgeCapability.QUEUE_FOLLOW_UP,
        BridgeCapability.INTERRUPT_TURN,
        BridgeCapability.REPLY_TO_APPROVAL,
        BridgeCapability.ATTACH_EXISTING,
    }
    experimental = {
        BridgeCapability.QUEUE_FOLLOW_UP,
        BridgeCapability.REPLY_TO_APPROVAL,
    }
    evidence = f"OpenCode {OPENCODE_VERSION} HTTP API {OPENCODE_API_VERSION}"
    unsupported = {
        BridgeCapability.REPLAY_EVENTS: "OpenCode SSE does not expose a replay cursor",
        BridgeCapability.STEER_ACTIVE_TURN: (
            "OpenCode has no distinct active-turn steering endpoint"
        ),
        BridgeCapability.STOP_SESSION: (
            "OpenCode exposes abort and delete, but no non-destructive stop endpoint"
        ),
    }
    return CapabilitySet(
        provider_id="opencode",
        provider_version=OPENCODE_VERSION,
        bridge_id=BRIDGE_ID,
        bridge_version=BRIDGE_VERSION,
        declarations=tuple(
            _declaration(
                capability,
                CapabilityAvailability.SUPPORTED
                if capability in supported
                else CapabilityAvailability.UNSUPPORTED,
                evidence if capability in supported else unsupported[capability],
                stability=(
                    BridgeStability.EXPERIMENTAL
                    if capability in experimental
                    else BridgeStability.STABLE
                ),
            )
            for capability in BridgeCapability
        ),
    )


def attached_tui_command(
    *,
    executable: Path,
    base_url: str,
    repository: Path,
    session_id: str | None = None,
) -> tuple[str, ...]:
    url = _validated_base_url(base_url)
    command = [str(executable), "attach", url, "--dir", str(repository)]
    if session_id is not None:
        command.extend(("--session", session_id))
    return tuple(command)


class OpenCodeServerBridge:
    def __init__(
        self,
        *,
        client: OpenCodeClient,
        identity: SessionIdentity,
        repository: Path,
    ) -> None:
        self._client = client
        self._identity = identity
        self._repository = repository
        self._capabilities = opencode_server_capabilities()
        validate_connection(identity, self._capabilities)
        self._events: list[SupervisionEvent] = []
        self._pending_approvals: set[str] = set()
        self._active_tools: set[str] = set()
        self._active_turn_id: str | None = None
        self._changed = asyncio.Event()
        self._closed = False
        self._pump: asyncio.Task[None] | None = None
        self._observing = asyncio.Event()
        self._observation_failed = False

    @classmethod
    async def attach(
        cls,
        *,
        base_url: str,
        username: str,
        password: str,
        provider_session_id: str,
        veyro_session_id: str,
        repository: Path,
    ) -> OpenCodeServerBridge:
        client = OpenCodeClient(base_url, username=username, password=password)
        try:
            return await cls.attach_connected(
                client=client,
                provider_session_id=provider_session_id,
                veyro_session_id=veyro_session_id,
                repository=repository,
            )
        except BaseException:
            await client.close()
            raise

    @classmethod
    async def attach_connected(
        cls,
        *,
        client: OpenCodeClient,
        provider_session_id: str,
        veyro_session_id: str,
        repository: Path,
    ) -> OpenCodeServerBridge:
        health = await client.health()
        if health != {"healthy": True, "version": OPENCODE_VERSION}:
            raise OpenCodeError("unsupported OpenCode server version")
        resolved_repository = await asyncio.to_thread(repository.resolve)
        session = await client.session(provider_session_id, directory=resolved_repository)
        if session.get("id") != provider_session_id:
            raise OpenCodeError("OpenCode returned the wrong session")
        if session.get("version") != OPENCODE_VERSION:
            raise OpenCodeError("OpenCode session uses an unsupported version")
        directory = session.get("directory")
        if not isinstance(directory, str) or not Path(directory).is_absolute():
            raise OpenCodeError("OpenCode session has no absolute directory")
        resolved_directory = await asyncio.to_thread(Path(directory).resolve)
        if resolved_directory != resolved_repository:
            raise OpenCodeError("OpenCode session belongs to a different repository")
        identity = SessionIdentity(
            veyro_session_id=veyro_session_id,
            provider_id="opencode",
            provider_session_id=provider_session_id,
            repository=str(resolved_repository),
            provider_version=OPENCODE_VERSION,
            bridge_id=BRIDGE_ID,
            bridge_version=BRIDGE_VERSION,
        )
        bridge = cls(client=client, identity=identity, repository=resolved_repository)
        bridge._append_event(
            native_type="session.attached",
            event_type=SupervisionEventType.SESSION_STARTED,
            raw={"session_id": provider_session_id},
            payload={"attached": True},
        )
        bridge._pump = asyncio.create_task(bridge._pump_events())
        try:
            await asyncio.wait_for(bridge._observing.wait(), timeout=5)
            if bridge._observation_failed:
                raise OpenCodeError("OpenCode observation stream is unavailable")
        except BaseException:
            await bridge.close()
            raise
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
        if request.session != self.identity:
            return self._result(
                request, ControlOutcome.REJECTED, "control belongs to another session"
            )
        capability = required_capability(request.intent.action)
        if not self.capabilities.supports(capability):
            result = self._result(request, ControlOutcome.UNSUPPORTED, "control is not supported")
            validate_control_result(request, result, self.capabilities)
            return result
        session_id = self.identity.provider_session_id
        assert session_id is not None
        path: str
        body: object | None = None
        expected_status = (200,)
        provider_command_id: str | None = None
        if request.intent.action is ControlAction.QUEUE_FOLLOW_UP:
            path = f"/session/{urllib.parse.quote(session_id, safe='')}/prompt_async"
            body = {"parts": [{"type": "text", "text": request.intent.message}]}
            expected_status = (204,)
        elif request.intent.action is ControlAction.INTERRUPT_TURN:
            path = f"/session/{urllib.parse.quote(session_id, safe='')}/abort"
        elif request.intent.action is ControlAction.REPLY_TO_APPROVAL:
            approval_id = request.intent.approval_id
            if approval_id not in self._pending_approvals:
                result = self._result(
                    request,
                    ControlOutcome.REJECTED,
                    "approval request was not observed for this session",
                )
                validate_control_result(request, result, self.capabilities)
                return result
            path = f"/permission/{urllib.parse.quote(approval_id, safe='')}/reply"
            reply = "once" if request.intent.decision is ApprovalDecision.APPROVE else "reject"
            body = {"reply": reply}
            provider_command_id = approval_id
        else:
            result = self._result(request, ControlOutcome.UNSUPPORTED, "control is not supported")
            validate_control_result(request, result, self.capabilities)
            return result
        try:
            response = await self._client.request_json(
                "POST",
                path,
                body=body,
                directory=self._repository,
                expected_status=expected_status,
            )
            if (
                request.intent.action
                in {
                    ControlAction.INTERRUPT_TURN,
                    ControlAction.REPLY_TO_APPROVAL,
                }
                and response is not True
            ):
                raise OpenCodeError("OpenCode rejected the control")
        except OpenCodeError as error:
            result = self._result(request, ControlOutcome.FAILED, str(error))
        else:
            if request.intent.action is ControlAction.REPLY_TO_APPROVAL:
                self._pending_approvals.discard(request.intent.approval_id)
            result = self._result(
                request,
                ControlOutcome.EXECUTED,
                provider_command_id=provider_command_id,
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

    async def _pump_events(self) -> None:
        try:
            async for native in self._client.events(directory=self._repository):
                self._observing.set()
                self._normalize(native)
            raise OpenCodeError("OpenCode observation stream ended")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._observation_failed = True
            self._observing.set()
            self._append_event(
                native_type="bridge.error",
                event_type=SupervisionEventType.NORMALIZATION_FAILED,
                raw={"error_type": type(error).__name__},
                payload={"error_type": type(error).__name__},
            )

    def _normalize(self, native: dict[str, Any]) -> None:
        if _native_session_id(native) != self.identity.provider_session_id:
            return
        native_type = str(native.get("type", "unknown"))
        mapped = _map_native_event(native_type, native)
        if mapped is None:
            return
        event_type, payload = mapped
        if event_type is SupervisionEventType.TURN_STARTED:
            turn_id = payload.get("turn_id")
            if not isinstance(turn_id, str) or self._active_turn_id is not None:
                return
            self._active_turn_id = turn_id
        elif event_type is SupervisionEventType.TURN_COMPLETED:
            turn_id = payload.get("turn_id")
            if turn_id != self._active_turn_id:
                return
            self._active_turn_id = None
        elif event_type is SupervisionEventType.TOOL_STARTED:
            tool_call_id = payload.get("tool_call_id")
            if not isinstance(tool_call_id, str) or tool_call_id in self._active_tools:
                return
            self._active_tools.add(tool_call_id)
        elif event_type is SupervisionEventType.TOOL_COMPLETED:
            tool_call_id = payload.get("tool_call_id")
            if isinstance(tool_call_id, str):
                self._active_tools.discard(tool_call_id)
        if event_type is SupervisionEventType.APPROVAL_REQUESTED:
            approval_id = payload.get("approval_id")
            if isinstance(approval_id, str):
                self._pending_approvals.add(approval_id)
        elif event_type is SupervisionEventType.APPROVAL_RESOLVED:
            approval_id = payload.get("approval_id")
            if isinstance(approval_id, str):
                self._pending_approvals.discard(approval_id)
        native_event_id = native.get("id") if isinstance(native.get("id"), str) else None
        self._append_event(
            native_type=native_type,
            event_type=event_type,
            raw=native,
            payload=payload,
            native_event_id=native_event_id,
        )

    def _append_event(
        self,
        *,
        native_type: str,
        event_type: SupervisionEventType,
        raw: object,
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
                    source=BridgeSource.SERVER,
                    native_event_type=native_type,
                    native_event_id=native_event_id,
                    observed_at=datetime.now(UTC),
                    raw_event_sha256=_canonical_digest(raw),
                    replayed=False,
                ),
                sensitivity=EventSensitivity.METADATA,
            )
        )
        self._changed.set()

    async def close(self) -> None:
        self._closed = True
        await self._client.close()
        if self._pump is not None:
            self._pump.cancel()
            await asyncio.gather(self._pump, return_exceptions=True)
            self._pump = None
        self._changed.set()


def _native_session_id(native: dict[str, Any]) -> str | None:
    properties = native.get("properties")
    if not isinstance(properties, dict):
        return None
    session_id = properties.get("sessionID")
    if isinstance(session_id, str):
        return session_id
    info = properties.get("info")
    if isinstance(info, dict):
        session_id = info.get("sessionID") or info.get("id")
        if isinstance(session_id, str) and session_id.startswith("ses"):
            return session_id
    part = properties.get("part")
    if isinstance(part, dict) and isinstance(part.get("sessionID"), str):
        return part["sessionID"]
    return None


def _map_native_event(
    native_type: str, native: dict[str, Any]
) -> tuple[SupervisionEventType, dict[str, object]] | None:
    properties = native.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    if native_type == "session.created":
        return SupervisionEventType.SESSION_STARTED, {}
    if native_type == "session.deleted":
        return SupervisionEventType.SESSION_COMPLETED, {"reason": "deleted"}
    if native_type == "session.next.step.started":
        message_id = properties.get("assistantMessageID")
        return SupervisionEventType.TURN_STARTED, {
            "turn_id": message_id if isinstance(message_id, str) else "opencode-turn-unknown"
        }
    if native_type == "session.next.step.ended":
        message_id = properties.get("assistantMessageID")
        return SupervisionEventType.TURN_COMPLETED, {
            "turn_id": message_id if isinstance(message_id, str) else "opencode-turn-unknown"
        }
    if native_type == "session.next.tool.called":
        payload: dict[str, object] = {}
        call_id = properties.get("callID")
        tool = properties.get("tool")
        if isinstance(call_id, str):
            payload["tool_call_id"] = call_id
        if isinstance(tool, str):
            payload["tool_name"] = tool
        return SupervisionEventType.TOOL_STARTED, payload
    if native_type in {"session.next.tool.success", "session.next.tool.failed"}:
        payload = {"success": native_type.endswith("success")}
        call_id = properties.get("callID")
        if isinstance(call_id, str):
            payload["tool_call_id"] = call_id
        return SupervisionEventType.TOOL_COMPLETED, payload
    if native_type == "message.part.updated":
        part = properties.get("part")
        if not isinstance(part, dict):
            return SupervisionEventType.UNKNOWN, {}
        part_type = part.get("type")
        message_id = part.get("messageID")
        turn_id = message_id if isinstance(message_id, str) else "opencode-turn-unknown"
        if part_type == "step-start":
            return SupervisionEventType.TURN_STARTED, {"turn_id": turn_id}
        if part_type == "step-finish":
            return SupervisionEventType.TURN_COMPLETED, {"turn_id": turn_id}
        if part_type == "tool":
            call_id = part.get("callID")
            tool = part.get("tool")
            state = part.get("state")
            status = state.get("status") if isinstance(state, dict) else None
            payload = {}
            if isinstance(call_id, str):
                payload["tool_call_id"] = call_id
            if isinstance(tool, str):
                payload["tool_name"] = tool
            if status in {"pending", "running"}:
                return SupervisionEventType.TOOL_STARTED, payload
            if status in {"completed", "error"}:
                payload["success"] = status == "completed"
                return SupervisionEventType.TOOL_COMPLETED, payload
        return SupervisionEventType.UNKNOWN, {}
    if native_type == "message.updated":
        info = properties.get("info")
        if not isinstance(info, dict):
            return SupervisionEventType.UNKNOWN, {}
        role = info.get("role")
        message_id = info.get("id")
        payload = {"role": role} if role in {"user", "assistant"} else {}
        if isinstance(message_id, str):
            payload["message_id"] = message_id
        if role == "user":
            return SupervisionEventType.USER_PROMPT_SUBMITTED, payload
        completed = info.get("time")
        if role == "assistant" and isinstance(completed, dict) and "completed" in completed:
            return SupervisionEventType.AGENT_MESSAGE_COMPLETED, payload
        return SupervisionEventType.UNKNOWN, payload
    if native_type in {"permission.asked", "permission.v2.asked"}:
        approval_id = properties.get("id")
        payload = {"approval_id": approval_id} if isinstance(approval_id, str) else {}
        return SupervisionEventType.APPROVAL_REQUESTED, payload
    if native_type in {"permission.replied", "permission.v2.replied"}:
        approval_id = properties.get("requestID")
        reply = properties.get("reply")
        payload = {"approval_id": approval_id} if isinstance(approval_id, str) else {}
        if isinstance(reply, str):
            payload["decision"] = "denied" if reply == "reject" else "approved"
        return SupervisionEventType.APPROVAL_RESOLVED, payload
    if native_type == "session.idle":
        return SupervisionEventType.SESSION_IDLE, {}
    if native_type == "session.status":
        status = properties.get("status")
        if isinstance(status, dict) and status.get("type") == "idle":
            return SupervisionEventType.SESSION_IDLE, {}
    return SupervisionEventType.UNKNOWN, {}
