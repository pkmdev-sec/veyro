from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from foreman.bridges.prime_agent import (
    BRIDGE_ID,
    BRIDGE_VERSION,
    DAEMON_PROTOCOL_NAME,
    DAEMON_PROTOCOL_VERSION,
    DAEMON_SCHEMA_ID,
    DAEMON_SCHEMA_REVISION,
    PRIME_AGENT_VERSION,
    PrimeAgentDaemonBridge,
    PrimeDaemonClient,
    PrimeDaemonError,
    _map_native_event,
    prime_daemon_capabilities,
)
from foreman.models import (
    ApprovalDecision,
    BridgeCapability,
    CapabilityAvailability,
    ControlOutcome,
    ControlRequest,
    EventSensitivity,
    InterruptTurn,
    QueueFollowUp,
    ReplyToApproval,
    SessionIdentity,
    SteerActiveTurn,
    StopSession,
    SupervisionEventType,
)
from foreman.supervision import replay_session


class FakeClient:
    def __init__(self) -> None:
        self.commands: list[dict[str, object]] = []
        self.outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False

    async def request(self, command: Mapping[str, object]) -> dict[str, Any]:
        self.commands.append(dict(command))
        return {
            "type": "response",
            "id": f"native-{len(self.commands)}",
            "command": command["type"],
            "success": True,
            "data": {},
        }

    async def next_outbound(self) -> dict[str, Any]:
        return await self.outbound.get()

    async def close(self) -> None:
        self.closed = True


def identity() -> SessionIdentity:
    return SessionIdentity(
        foreman_session_id="foreman-prime-1",
        provider_id="prime-agent",
        provider_session_id="active-prime-1",
        repository="/tmp/repository",
        provider_version=PRIME_AGENT_VERSION,
        bridge_id=BRIDGE_ID,
        bridge_version=BRIDGE_VERSION,
    )


def bridge() -> tuple[PrimeAgentDaemonBridge, FakeClient]:
    client = FakeClient()
    return (
        PrimeAgentDaemonBridge(
            client=client,  # type: ignore[arg-type]
            identity=identity(),
            active_session_id="active-prime-1",
        ),
        client,
    )


def request(
    intent: object,
    *,
    session: SessionIdentity | None = None,
    command_id: str = "control-1",
) -> ControlRequest:
    return ControlRequest(session=session or identity(), command_id=command_id, intent=intent)  # type: ignore[arg-type]


def test_capabilities_are_complete_and_honest_about_internal_daemon_api() -> None:
    capabilities = prime_daemon_capabilities()

    assert {item.capability for item in capabilities.declarations} == set(BridgeCapability)
    assert (
        capabilities.availability(BridgeCapability.STEER_ACTIVE_TURN)
        is CapabilityAvailability.SUPPORTED
    )
    assert (
        capabilities.availability(BridgeCapability.REPLY_TO_APPROVAL)
        is CapabilityAvailability.UNSUPPORTED
    )
    supported = [
        item
        for item in capabilities.declarations
        if item.availability is CapabilityAvailability.SUPPORTED
    ]
    assert all(item.stability.value == "internal" for item in supported)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent", "native_type"),
    [
        (QueueFollowUp(message="Run tests."), "follow_up"),
        (SteerActiveTurn(message="Use the smaller change."), "steer"),
        (InterruptTurn(reason="Boundary denied the action."), "abort"),
        (StopSession(reason="Operator stopped the session."), "kill"),
    ],
)
async def test_supported_controls_use_exact_daemon_commands(
    intent: object, native_type: str
) -> None:
    adapter, client = bridge()

    result = await adapter.execute(request(intent, session=adapter.identity))

    assert result.outcome is ControlOutcome.EXECUTED
    assert client.commands[-1]["type"] == native_type
    assert client.commands[-1]["activeSessionId"] == "active-prime-1"


@pytest.mark.asyncio
async def test_approval_reply_is_explicitly_unsupported_and_never_dispatched() -> None:
    adapter, client = bridge()

    result = await adapter.execute(
        request(
            ReplyToApproval(
                approval_id="approval-1",
                decision=ApprovalDecision.DENY,
                reason="Prime daemon has no approval response mapping.",
            ),
            session=adapter.identity,
        )
    )

    assert result.outcome is ControlOutcome.UNSUPPORTED
    assert client.commands == []


@pytest.mark.asyncio
async def test_foreign_session_control_is_rejected_before_dispatch() -> None:
    adapter, client = bridge()
    foreign = request(QueueFollowUp(message="Do not dispatch."))
    foreign = foreign.model_copy(
        update={"session": foreign.session.model_copy(update={"foreman_session_id": "other"})}
    )

    result = await adapter.execute(foreign)

    assert result.outcome is ControlOutcome.REJECTED
    assert client.commands == []


@pytest.mark.asyncio
async def test_native_events_are_ordered_replayable_and_content_free() -> None:
    adapter, client = bridge()
    adapter._append_event(
        native_type="session_attached",
        event_type=SupervisionEventType.SESSION_STARTED,
        raw={"type": "session_attached"},
        replayed=False,
        payload={"attached": True},
    )
    adapter._pump = asyncio.create_task(adapter._pump_outbound())
    await client.outbound.put(
        {
            "type": "session_event",
            "activeSessionId": "active-prime-1",
            "event": {
                "type": "tool_execution_start",
                "toolCallId": "call-1",
                "toolName": "bash",
                "args": {"command": "secret command"},
            },
            "meta": {"id": "native-event-1", "replayed": True},
        }
    )
    await asyncio.sleep(0)

    observed = []
    stream = adapter.events(after_sequence=0)
    observed.append(await anext(stream))
    observed.append(await anext(stream))

    assert [event.sequence for event in observed] == [1, 2]
    assert observed[1].event_type is SupervisionEventType.TOOL_STARTED
    assert observed[1].payload == {"tool_call_id": "call-1", "tool_name": "bash"}
    assert observed[1].provenance.replayed is True
    assert observed[1].provenance.raw_event_sha256 is not None
    assert observed[1].sensitivity is EventSensitivity.METADATA
    assert "secret" not in observed[1].model_dump_json()
    await stream.aclose()
    replay = adapter.events(after_sequence=1)
    assert (await anext(replay)).sequence == 2
    await replay.aclose()
    await adapter.close()


@pytest.mark.parametrize(
    ("native_type", "expected"),
    [
        ("agent_start", SupervisionEventType.UNKNOWN),
        ("agent_end", SupervisionEventType.SESSION_IDLE),
        ("bash_start", SupervisionEventType.TOOL_STARTED),
        ("bash_end", SupervisionEventType.TOOL_COMPLETED),
        ("new_future_event", SupervisionEventType.UNKNOWN),
    ],
)
def test_native_event_mapping_is_total(native_type: str, expected: SupervisionEventType) -> None:
    event_type, _ = _map_native_event("session_event", native_type, {"type": native_type})
    assert event_type is expected


@pytest.mark.asyncio
async def test_live_wire_client_requires_exact_versioned_hello() -> None:
    socket_path = Path("/tmp") / f"foreman-prime-{uuid4().hex[:10]}.sock"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b'{"type":"daemon_hello","protocol":{"name":"prime-agent.daemon",'
            b'"version":7},"schemaRevision":28,"schemaId":"old","appVersion":"0.9.4"}\n'
        )
        await writer.drain()
        await reader.read()
        writer.close()

    server = await asyncio.start_unix_server(handle, socket_path)
    socket_path.chmod(0o600)
    client = PrimeDaemonClient(socket_path)
    try:
        with pytest.raises(PrimeDaemonError, match="schema revision"):
            await client.connect()
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_wire_client_sends_v7_envelope_and_acknowledges_mutation() -> None:
    socket_path = Path("/tmp") / f"foreman-prime-{uuid4().hex[:10]}.sock"
    received: list[dict[str, Any]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        hello = {
            "type": "daemon_hello",
            "protocol": {"name": DAEMON_PROTOCOL_NAME, "version": DAEMON_PROTOCOL_VERSION},
            "schemaRevision": DAEMON_SCHEMA_REVISION,
            "schemaId": DAEMON_SCHEMA_ID,
            "appVersion": PRIME_AGENT_VERSION,
            "clientId": "daemon-client",
            "serverCapabilities": ["event_sequence"],
        }
        writer.write(json_line(hello))
        await writer.drain()
        command = json_load(await reader.readline())
        received.append(command)
        writer.write(
            json_line(
                {
                    "type": "response",
                    "id": command["id"],
                    "command": "abort",
                    "success": True,
                }
            )
        )
        await writer.drain()
        received.append(json_load(await reader.readline()))
        writer.close()

    server = await asyncio.start_unix_server(handle, socket_path)
    socket_path.chmod(0o600)
    client = PrimeDaemonClient(socket_path)
    try:
        await client.connect()
        await client.request({"type": "abort", "activeSessionId": "active-1"})
        await asyncio.sleep(0)
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert received[0]["type"] == "command"
    assert received[0]["protocol"] == {"name": DAEMON_PROTOCOL_NAME, "version": 7}
    assert received[0]["command"]["type"] == "abort"
    assert received[1]["command"]["type"] == "ack_result"
    assert received[1]["command"]["commandId"] == received[0]["id"]


def json_line(value: object) -> bytes:
    import json

    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def json_load(value: bytes) -> dict[str, Any]:
    import json

    loaded = json.loads(value)
    assert isinstance(loaded, dict)
    return loaded


def test_normalized_turn_and_tool_events_reduce_without_provider_special_cases() -> None:
    adapter, _ = bridge()
    adapter._append_event(
        native_type="session_attached",
        event_type=SupervisionEventType.SESSION_STARTED,
        raw={"type": "session_attached"},
        replayed=False,
        payload={"attached": True},
    )
    native_events = [
        ("turn_start", {"type": "turn_start"}, "native-turn-start"),
        (
            "tool_execution_start",
            {
                "type": "tool_execution_start",
                "toolCallId": "call-1",
                "toolName": "bash",
            },
            "native-tool-start",
        ),
        (
            "tool_execution_end",
            {
                "type": "tool_execution_end",
                "toolCallId": "call-1",
                "toolName": "bash",
                "isError": False,
            },
            "native-tool-end",
        ),
        ("turn_end", {"type": "turn_end"}, "native-turn-end"),
        ("agent_end", {"type": "agent_end"}, "native-agent-end"),
    ]
    for _native_type, native, native_id in native_events:
        adapter._normalize(
            {
                "type": "session_event",
                "activeSessionId": "active-prime-1",
                "event": native,
                "meta": {"id": native_id},
            }
        )

    state = replay_session(adapter.identity, adapter._events)

    assert state.last_sequence == 6
    assert state.turns_started == 1
    assert state.turns_completed == 1
    assert state.completed_tools == 1
    assert state.failed_tools == 0
    assert state.progress.value == "idle"
