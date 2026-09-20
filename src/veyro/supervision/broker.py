from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from veyro.bridges import validate_connection, validate_control_result
from veyro.models import (
    CapabilitySet,
    ControlRequest,
    ControlResult,
    EventSensitivity,
    RawProviderEvent,
    SessionIdentity,
    SupervisionEvent,
)

MAX_MESSAGE_BYTES = 256 * 1024
MAX_JOURNAL_ENTRY_BYTES = 64 * 1024
SOCKET_REPLAY_LIMIT = 8
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class BrokerError(RuntimeError):
    """Raised when supervision data cannot be stored or served safely."""


class BrokerConflictError(BrokerError):
    """Raised when ordered or idempotent data conflicts with persisted data."""


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _ensure_private_directory(path: Path, *, create: bool = True) -> None:
    if path.is_symlink():
        raise BrokerError(f"refusing symlinked broker directory: {path}")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise BrokerError(f"broker directory is not a directory: {path}")
    path.chmod(0o700)


def _create_private_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise BrokerError(f"cannot create private broker file: {path.name}") from error
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _replace_private_file(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _read_private_file(path: Path, *, maximum_bytes: int = MAX_MESSAGE_BYTES) -> bytes:
    if path.is_symlink():
        raise BrokerError(f"refusing symlinked broker file: {path.name}")
    try:
        if path.stat().st_size > maximum_bytes:
            raise BrokerError(f"broker file is too large: {path.name}")
        return path.read_bytes()
    except OSError as error:
        raise BrokerError(f"cannot read broker file: {path.name}") from error


def _append_private_line(path: Path, payload: bytes) -> None:
    if len(payload) > MAX_JOURNAL_ENTRY_BYTES:
        raise BrokerError("journal entry is too large")
    if path.is_symlink():
        raise BrokerError(f"refusing symlinked broker journal: {path.name}")
    flags = os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BrokerError(f"cannot append broker journal: {path.name}") from error
    with os.fdopen(descriptor, "ab") as handle:
        handle.write(payload)
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _iter_private_lines(path: Path):
    if path.is_symlink():
        raise BrokerError(f"refusing symlinked broker journal: {path.name}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BrokerError(f"cannot read broker journal: {path.name}") from error
    with os.fdopen(descriptor, "rb") as handle:
        for line in handle:
            if len(line) > MAX_JOURNAL_ENTRY_BYTES + 1:
                raise BrokerError(f"broker journal entry is too large: {path.name}")
            yield line.rstrip(b"\n")


def _ensure_local_git_exclude(repository: Path) -> None:
    exclude = repository / ".git" / "info" / "exclude"
    if not exclude.parent.is_dir() or exclude.is_symlink():
        return
    entry = "/.veyro/"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if entry in existing.splitlines():
        return
    separator = "" if not existing or existing.endswith("\n") else "\n"
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(f"{separator}{entry}\n")


class BrokerStore:
    def __init__(
        self,
        repository: Path,
        identity: SessionIdentity,
        capabilities: CapabilitySet,
        token: str,
        *,
        allow_content: bool,
    ) -> None:
        self.repository = repository.resolve()
        self.identity = identity
        self.capabilities = capabilities
        self.token = token
        self.allow_content = allow_content
        self.directory = self.repository / ".veyro" / "supervision" / identity.veyro_session_id
        self.identity_path = self.directory / "identity.json"
        self.capabilities_path = self.directory / "capabilities.json"
        self.settings_path = self.directory / "settings.json"
        self.token_path = self.directory / "token"
        self.events_path = self.directory / "events.jsonl"
        self.raw_events_path = self.directory / "raw-events.jsonl"
        self.controls_path = self.directory / "controls.jsonl"
        self.socket_metadata_path = self.directory / "socket.json"
        self._lock = threading.Lock()
        self._last_sequence = 0
        self._controls: dict[str, tuple[str, ControlResult]] = {}

    @classmethod
    def create(
        cls,
        repository: Path,
        identity: SessionIdentity,
        capabilities: CapabilitySet,
        *,
        allow_content: bool = False,
    ) -> BrokerStore:
        if not _SESSION_ID.fullmatch(identity.veyro_session_id):
            raise BrokerError("invalid Veyro session id")
        validate_connection(identity, capabilities)
        repository = repository.resolve()
        _ensure_local_git_exclude(repository)
        root = repository / ".veyro"
        supervision = root / "supervision"
        _ensure_private_directory(root)
        _ensure_private_directory(supervision)
        directory = supervision / identity.veyro_session_id
        if directory.exists() or directory.is_symlink():
            raise BrokerConflictError("supervision session already exists")
        directory.mkdir(mode=0o700)
        token = secrets.token_urlsafe(32)
        store = cls(
            repository,
            identity,
            capabilities,
            token,
            allow_content=allow_content,
        )
        _create_private_file(store.identity_path, identity.model_dump_json(indent=2).encode())
        _create_private_file(
            store.capabilities_path,
            capabilities.model_dump_json(indent=2).encode(),
        )
        _create_private_file(
            store.settings_path,
            _json_bytes({"allow_content": allow_content}),
        )
        _create_private_file(store.token_path, token.encode())
        for journal in (store.events_path, store.raw_events_path, store.controls_path):
            _create_private_file(journal, b"")
        return store

    @classmethod
    def open(cls, repository: Path, veyro_session_id: str) -> BrokerStore:
        if not _SESSION_ID.fullmatch(veyro_session_id):
            raise BrokerError("invalid Veyro session id")
        repository = repository.resolve()
        root = repository / ".veyro"
        supervision = root / "supervision"
        directory = supervision / veyro_session_id
        _ensure_private_directory(root, create=False)
        _ensure_private_directory(supervision, create=False)
        _ensure_private_directory(directory, create=False)
        token_path = directory / "token"
        if token_path.stat().st_mode & 0o077:
            raise BrokerError("broker token permissions are not private")
        try:
            identity = SessionIdentity.model_validate_json(
                _read_private_file(directory / "identity.json")
            )
            capabilities = CapabilitySet.model_validate_json(
                _read_private_file(directory / "capabilities.json")
            )
            settings = json.loads(_read_private_file(directory / "settings.json"))
            token = _read_private_file(token_path).decode("utf-8")
        except (ValidationError, ValueError, UnicodeDecodeError) as error:
            raise BrokerError("broker metadata is malformed") from error
        validate_connection(identity, capabilities)
        store = cls(
            repository,
            identity,
            capabilities,
            token,
            allow_content=bool(settings.get("allow_content", False)),
        )
        store._load_events()
        store._load_controls()
        return store

    def _load_events(self) -> None:
        events = self.replay_events()
        self._last_sequence = events[-1].sequence if events else 0

    def _load_controls(self) -> None:
        self._controls = {}
        for line_number, line in enumerate(_iter_private_lines(self.controls_path), start=1):
            if not line:
                continue
            try:
                payload = json.loads(line)
                command_id = str(payload["command_id"])
                request_sha256 = str(payload["request_sha256"])
                result = ControlResult.model_validate(payload["result"])
                if (
                    result.command_id != command_id
                    or payload["action"] != result.action.value
                    or not re.fullmatch(r"[0-9a-f]{64}", request_sha256)
                ):
                    raise ValueError("control journal metadata mismatch")
            except (KeyError, TypeError, ValueError, ValidationError) as error:
                raise BrokerError(f"control journal line {line_number} is malformed") from error
            if command_id in self._controls:
                raise BrokerError("control journal contains duplicate command ids")
            self._controls[command_id] = (request_sha256, result)

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    def append_event(self, event: SupervisionEvent) -> None:
        with self._lock:
            if event.session != self.identity:
                raise BrokerConflictError("event session identity does not match the broker")
            if event.sensitivity is EventSensitivity.CONTENT_OPT_IN and not self.allow_content:
                raise BrokerError("content retention is not enabled for this session")
            expected = self._last_sequence + 1
            if event.sequence != expected:
                raise BrokerConflictError(
                    f"expected sequence {expected}, received {event.sequence}"
                )
            _append_private_line(self.events_path, event.model_dump_json().encode())
            self._last_sequence = event.sequence

    def append_raw_event(self, event: RawProviderEvent) -> None:
        with self._lock:
            if event.session != self.identity:
                raise BrokerConflictError("raw event session identity does not match the broker")
            if event.payload is not None and not self.allow_content:
                raise BrokerError("content retention is not enabled for this session")
            _append_private_line(self.raw_events_path, event.model_dump_json().encode())

    def replay_events(
        self,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> list[SupervisionEvent]:
        if after_sequence < 0 or limit is not None and limit < 1:
            raise BrokerError("invalid replay cursor or limit")
        events: list[SupervisionEvent] = []
        expected = 1
        found_after_cursor = False
        for line_number, line in enumerate(
            _iter_private_lines(self.events_path),
            start=1,
        ):
            if not line:
                continue
            try:
                event = SupervisionEvent.model_validate_json(line)
            except (ValidationError, ValueError) as error:
                raise BrokerError(f"event journal line {line_number} is malformed") from error
            if event.session != self.identity or event.sequence != expected:
                raise BrokerError("event journal identity or sequence is malformed")
            expected += 1
            if event.sequence > after_sequence:
                found_after_cursor = True
                events.append(event)
                if limit is not None and len(events) >= limit:
                    break
        last_sequence = expected - 1
        if not found_after_cursor and after_sequence > last_sequence:
            raise BrokerConflictError("replay cursor is beyond the persisted event sequence")
        return events

    def record_control(
        self,
        request: ControlRequest,
        result: ControlResult,
    ) -> ControlResult:
        if request.session != self.identity:
            raise BrokerConflictError("control request session identity does not match the broker")
        validate_control_result(request, result, self.capabilities)
        request_payload = request.model_dump(mode="json", exclude={"issued_at"})
        request_sha256 = hashlib.sha256(_json_bytes(request_payload)).hexdigest()
        with self._lock:
            existing = self._controls.get(request.command_id)
            if existing is not None:
                existing_hash, existing_result = existing
                if existing_hash != request_sha256:
                    raise BrokerConflictError("command id was already used for a different request")
                return existing_result
            entry = {
                "protocol_version": "1.0",
                "command_id": request.command_id,
                "action": request.intent.action.value,
                "request_sha256": request_sha256,
                "result": result.model_dump(mode="json"),
            }
            _append_private_line(self.controls_path, _json_bytes(entry))
            self._controls[request.command_id] = (request_sha256, result)
            return result

    def lookup_control(self, command_id: str) -> ControlResult | None:
        existing = self._controls.get(command_id)
        return existing[1] if existing is not None else None

    def publish_socket_path(self, socket_path: Path) -> None:
        _replace_private_file(
            self.socket_metadata_path,
            _json_bytes({"protocol_version": "1.0", "socket_path": str(socket_path)}),
        )


class SessionBroker:
    def __init__(self, store: BrokerStore) -> None:
        self.store = store
        candidate = store.directory / "broker.sock"
        if len(os.fsencode(candidate)) <= 100:
            self.socket_path = candidate
        else:
            digest = hashlib.sha256(str(store.directory).encode()).hexdigest()[:20]
            self.socket_path = Path(tempfile.gettempdir()) / f"veyro-{os.getuid()}-{digest}.sock"
        self._server: asyncio.AbstractServer | None = None
        self._owns_socket = False

    async def start(self) -> None:
        if self._server is not None:
            raise BrokerConflictError("session broker is already running")
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise BrokerConflictError("session broker socket already exists")
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_client,
                path=self.socket_path,
                limit=MAX_MESSAGE_BYTES + 1,
            )
            self._owns_socket = True
            self.socket_path.chmod(0o600)
            self.store.publish_socket_path(self.socket_path)
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        if self._owns_socket:
            self.socket_path.unlink(missing_ok=True)
            self.store.socket_metadata_path.unlink(missing_ok=True)
            self._owns_socket = False

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            try:
                message = await reader.readline()
            except ValueError:
                await self._write(writer, {"ok": False, "error": "message_too_large"})
                return
            if len(message) > MAX_MESSAGE_BYTES:
                await self._write(writer, {"ok": False, "error": "message_too_large"})
                return
            try:
                request = json.loads(message)
            except (json.JSONDecodeError, UnicodeDecodeError):
                await self._write(writer, {"ok": False, "error": "invalid_request"})
                return
            if not isinstance(request, dict):
                await self._write(writer, {"ok": False, "error": "invalid_request"})
                return
            token = request.get("token")
            if not isinstance(token, str) or not hmac.compare_digest(token, self.store.token):
                await self._write(writer, {"ok": False, "error": "unauthorized"})
                return
            response = self._dispatch(request)
            await self._write(writer, response)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    def _dispatch(self, request: dict[str, Any]) -> dict[str, object]:
        try:
            operation = request.get("operation")
            if operation == "publish_event":
                event = SupervisionEvent.model_validate(request.get("event"))
                self.store.append_event(event)
                return {"ok": True, "sequence": event.sequence}
            if operation == "publish_raw_event":
                event = RawProviderEvent.model_validate(request.get("event"))
                self.store.append_raw_event(event)
                return {"ok": True, "raw_event_id": event.raw_event_id}
            if operation == "replay_events":
                after_sequence = request.get("after_sequence", 0)
                if isinstance(after_sequence, bool) or not isinstance(after_sequence, int):
                    raise ValueError("after_sequence must be an integer")
                events = self.store.replay_events(
                    after_sequence=after_sequence,
                    limit=SOCKET_REPLAY_LIMIT,
                )
                serialized: list[dict[str, Any]] = []
                for event in events:
                    candidate = [*serialized, event.model_dump(mode="json")]
                    response = {"ok": True, "events": candidate}
                    if len(_json_bytes(response)) + 1 > MAX_MESSAGE_BYTES:
                        break
                    serialized = candidate
                next_sequence = serialized[-1]["sequence"] if serialized else after_sequence
                return {
                    "ok": True,
                    "events": serialized,
                    "next_sequence": next_sequence,
                    "has_more": next_sequence < self.store.last_sequence,
                }
            if operation == "record_control":
                control_request = ControlRequest.model_validate(request.get("request"))
                result = ControlResult.model_validate(request.get("result"))
                stored = self.store.record_control(control_request, result)
                return {"ok": True, "result": stored.model_dump(mode="json")}
            if operation == "lookup_control":
                command_id = request.get("command_id")
                if not isinstance(command_id, str):
                    raise ValueError("command id is required")
                result = self.store.lookup_control(command_id)
                return {
                    "ok": True,
                    "result": result.model_dump(mode="json") if result else None,
                }
            return {"ok": False, "error": "unknown_operation"}
        except BrokerConflictError:
            return {"ok": False, "error": "conflict"}
        except (BrokerError, ValidationError, TypeError, ValueError):
            return {"ok": False, "error": "invalid_request"}

    async def _write(
        self,
        writer: asyncio.StreamWriter,
        response: dict[str, object],
    ) -> None:
        payload = _json_bytes(response) + b"\n"
        if len(payload) > MAX_MESSAGE_BYTES:
            payload = b'{"error":"response_too_large","ok":false}\n'
        writer.write(payload)
        await writer.drain()
