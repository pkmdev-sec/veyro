from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from foreman.models import (
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
    EventSensitivity,
    QueueFollowUp,
    RawProviderEvent,
    SessionIdentity,
    SupervisionEvent,
    SupervisionEventType,
)
from foreman.supervision import (
    MAX_MESSAGE_BYTES,
    BrokerConflictError,
    BrokerError,
    BrokerStore,
    SessionBroker,
)


def identity() -> SessionIdentity:
    return SessionIdentity(
        foreman_session_id="foreman-1",
        provider_id="prime-agent",
        provider_session_id="prime-1",
        repository="/tmp/project",
        provider_version="0.9.5",
        bridge_id="prime-daemon-v4",
        bridge_version="1.0.0",
    )


def capabilities() -> CapabilitySet:
    return CapabilitySet(
        provider_id="prime-agent",
        provider_version="0.9.5",
        bridge_id="prime-daemon-v4",
        bridge_version="1.0.0",
        declarations=(
            CapabilityDeclaration(
                capability=BridgeCapability.QUEUE_FOLLOW_UP,
                availability=CapabilityAvailability.SUPPORTED,
                stability=BridgeStability.STABLE,
                evidence="Public Daemon Protocol v4",
            ),
        ),
    )


def event(
    sequence: int,
    *,
    session: SessionIdentity | None = None,
    sensitivity=EventSensitivity.METADATA,
) -> SupervisionEvent:
    return SupervisionEvent(
        session=session or identity(),
        sequence=sequence,
        event_type=SupervisionEventType.TOOL_COMPLETED,
        payload={"tool": "pytest", "exit_code": 0},
        provenance=EventProvenance(
            source=BridgeSource.DAEMON,
            native_event_type="tool_end",
            raw_event_sha256=f"{sequence:064x}",
        ),
        sensitivity=sensitivity,
    )


def control(
    command_id: str,
    message: str = "Run tests.",
    *,
    session: SessionIdentity | None = None,
) -> ControlRequest:
    return ControlRequest(
        session=session or identity(),
        command_id=command_id,
        intent=QueueFollowUp(message=message),
    )


def control_result(request: ControlRequest, detail: str = "") -> ControlResult:
    return ControlResult(
        foreman_session_id=request.session.foreman_session_id,
        provider_id=request.session.provider_id,
        command_id=request.command_id,
        action=request.intent.action,
        outcome=ControlOutcome.EXECUTED,
        detail=detail,
    )


def test_store_is_private_and_recovers_replay_cursor(tmp_path: Path) -> None:
    session = identity()
    store = BrokerStore.create(tmp_path, session, capabilities())
    store.append_event(event(1, session=session))
    store.append_event(event(2, session=session))

    reopened = BrokerStore.open(tmp_path, "foreman-1")

    assert (store.directory.stat().st_mode & 0o777) == 0o700
    assert (store.token_path.stat().st_mode & 0o777) == 0o600
    assert store.token not in store.identity_path.read_text()
    assert [item.sequence for item in reopened.replay_events(after_sequence=1)] == [2]
    reopened.append_event(event(3, session=session))
    assert [item.sequence for item in reopened.replay_events()] == [1, 2, 3]


def test_store_rejects_sequence_gaps_and_wrong_sessions(tmp_path: Path) -> None:
    session = identity()
    store = BrokerStore.create(tmp_path, session, capabilities())

    with pytest.raises(BrokerConflictError, match="expected sequence 1"):
        store.append_event(event(2, session=session))

    foreign = event(1, session=session).model_copy(
        update={"session": identity().model_copy(update={"foreman_session_id": "other"})}
    )
    with pytest.raises(BrokerConflictError, match="session identity"):
        store.append_event(foreign)


def test_store_requires_explicit_content_retention_opt_in(tmp_path: Path) -> None:
    session = identity()
    store = BrokerStore.create(tmp_path, session, capabilities())

    with pytest.raises(BrokerError, match="content retention"):
        store.append_event(event(1, session=session, sensitivity=EventSensitivity.CONTENT_OPT_IN))

    opted_in_session = session.model_copy(update={"foreman_session_id": "foreman-2"})
    opted_in = BrokerStore.create(
        tmp_path,
        opted_in_session,
        capabilities(),
        allow_content=True,
    )
    content_event = event(
        1,
        session=opted_in_session,
        sensitivity=EventSensitivity.CONTENT_OPT_IN,
    )
    opted_in.append_event(content_event)
    assert opted_in.replay_events()[0].sensitivity is EventSensitivity.CONTENT_OPT_IN


def test_control_journal_is_idempotent_without_storing_message_content(tmp_path: Path) -> None:
    session = identity()
    store = BrokerStore.create(tmp_path, session, capabilities())
    request = control("command-1", "Private steering content", session=session)
    first = control_result(request)

    assert store.record_control(request, first) == first
    assert store.record_control(request, control_result(request, "different result")) == first
    assert store.lookup_control("command-1") == first
    assert "Private steering content" not in store.controls_path.read_text()

    with pytest.raises(BrokerConflictError, match="different request"):
        store.record_control(
            control("command-1", "Conflicting content", session=session),
            control_result(request),
        )


def test_reopened_store_preserves_control_idempotency(tmp_path: Path) -> None:
    session = identity()
    store = BrokerStore.create(tmp_path, session, capabilities())
    request_value = control("command-reopen", session=session)
    original = control_result(request_value)
    store.record_control(request_value, original)

    reopened = BrokerStore.open(tmp_path, session.foreman_session_id)

    assert reopened.lookup_control(request_value.command_id) == original
    assert (
        reopened.record_control(
            request_value,
            control_result(request_value, "must not replace original"),
        )
        == original
    )


def test_store_persists_digest_only_raw_events_without_content_opt_in(tmp_path: Path) -> None:
    session = identity()
    store = BrokerStore.create(tmp_path, session, capabilities())
    raw = RawProviderEvent(
        session=session,
        native_event_type="agent_message",
        payload_sha256="c" * 64,
    )

    store.append_raw_event(raw)

    persisted = store.raw_events_path.read_text()
    assert raw.raw_event_id in persisted
    assert '"payload":null' in persisted


def test_store_rejects_a_replay_cursor_beyond_persisted_events(tmp_path: Path) -> None:
    session = identity()
    store = BrokerStore.create(tmp_path, session, capabilities())
    store.append_event(event(1, session=session))

    with pytest.raises(BrokerConflictError, match="replay cursor"):
        store.replay_events(after_sequence=2)


def test_store_refuses_an_exposed_token_file(tmp_path: Path) -> None:
    session = identity()
    store = BrokerStore.create(tmp_path, session, capabilities())
    store.token_path.chmod(0o644)

    with pytest.raises(BrokerError, match="token permissions"):
        BrokerStore.open(tmp_path, session.foreman_session_id)


def test_store_refuses_a_symlinked_private_root(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / ".foreman").symlink_to(target, target_is_directory=True)

    with pytest.raises(BrokerError, match="symlinked broker directory"):
        BrokerStore.create(tmp_path, identity(), capabilities())


async def request(socket_path: Path, payload: dict[str, object]) -> dict[str, object]:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    writer.write(json.dumps(payload).encode() + b"\n")
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    return response


@pytest.mark.asyncio
async def test_authenticated_socket_publishes_and_replays_events(tmp_path: Path) -> None:
    session = identity()
    store = BrokerStore.create(tmp_path, session, capabilities())
    broker = SessionBroker(store)
    await broker.start()
    try:
        assert broker.socket_path.exists()
        assert (broker.socket_path.stat().st_mode & 0o777) == 0o600

        denied = await request(
            broker.socket_path,
            {"operation": "replay_events", "token": "wrong", "after_sequence": 0},
        )
        assert denied == {"ok": False, "error": "unauthorized"}

        published = await request(
            broker.socket_path,
            {
                "operation": "publish_event",
                "token": store.token,
                "event": event(1, session=session).model_dump(mode="json"),
            },
        )
        assert published == {"ok": True, "sequence": 1}

        replayed = await request(
            broker.socket_path,
            {
                "operation": "replay_events",
                "token": store.token,
                "after_sequence": 0,
            },
        )
        assert replayed["ok"] is True
        assert [item["sequence"] for item in replayed["events"]] == [1]

        command = control("socket-command", "Private follow-up", session=session)
        result = control_result(command)
        recorded = await request(
            broker.socket_path,
            {
                "operation": "record_control",
                "token": store.token,
                "request": command.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
            },
        )
        assert recorded["ok"] is True
        looked_up = await request(
            broker.socket_path,
            {
                "operation": "lookup_control",
                "token": store.token,
                "command_id": command.command_id,
            },
        )
        assert looked_up["result"]["command_id"] == command.command_id
        assert "Private follow-up" not in store.controls_path.read_text()
    finally:
        await broker.close()

    assert not broker.socket_path.exists()


@pytest.mark.asyncio
async def test_socket_replay_uses_bounded_cursors(tmp_path: Path) -> None:
    session = identity()
    store = BrokerStore.create(tmp_path, session, capabilities())
    for sequence in range(1, 11):
        store.append_event(event(sequence, session=session))
    broker = SessionBroker(store)
    await broker.start()
    try:
        first = await request(
            broker.socket_path,
            {
                "operation": "replay_events",
                "token": store.token,
                "after_sequence": 0,
            },
        )
        second = await request(
            broker.socket_path,
            {
                "operation": "replay_events",
                "token": store.token,
                "after_sequence": first["next_sequence"],
            },
        )

        assert [item["sequence"] for item in first["events"]] == list(range(1, 9))
        assert first["has_more"] is True
        assert [item["sequence"] for item in second["events"]] == [9, 10]
        assert second["has_more"] is False
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_unstarted_broker_does_not_remove_an_unowned_socket(tmp_path: Path) -> None:
    store = BrokerStore.create(tmp_path, identity(), capabilities())
    broker = SessionBroker(store)
    broker.socket_path.write_text("not a socket")

    await broker.close()

    assert broker.socket_path.read_text() == "not a socket"


@pytest.mark.asyncio
async def test_socket_rejects_oversized_messages_without_stopping(tmp_path: Path) -> None:
    store = BrokerStore.create(tmp_path, identity(), capabilities())
    broker = SessionBroker(store)
    await broker.start()
    try:
        reader, writer = await asyncio.open_unix_connection(broker.socket_path)
        writer.write(b"x" * (MAX_MESSAGE_BYTES + 1) + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()

        assert response == {"ok": False, "error": "message_too_large"}
        healthy = await request(
            broker.socket_path,
            {
                "operation": "replay_events",
                "token": store.token,
                "after_sequence": 0,
            },
        )
        assert healthy["ok"] is True
    finally:
        await broker.close()
