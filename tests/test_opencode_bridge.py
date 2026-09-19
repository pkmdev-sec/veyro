from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from foreman.bridges.opencode import (
    BRIDGE_ID,
    BRIDGE_VERSION,
    OPENCODE_VERSION,
    OpenCodeClient,
    OpenCodeError,
    OpenCodeServerBridge,
    attached_tui_command,
    opencode_server_capabilities,
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
        self.requests: list[tuple[str, str, object | None]] = []
        self.native_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        body: object | None = None,
        directory: Path | None = None,
        expected_status: tuple[int, ...] = (200,),
    ) -> object | None:
        self.requests.append((method, path, body))
        if path.endswith("/prompt_async"):
            return None
        return True

    async def events(self, *, directory: Path):
        while True:
            yield await self.native_events.get()

    async def close(self) -> None:
        self.closed = True


class AttachableFakeClient(FakeClient):
    def __init__(
        self, *, version: str = OPENCODE_VERSION, directory: str = "/tmp/repository"
    ) -> None:
        super().__init__()
        self.version = version
        self.directory = directory

    async def events(self, *, directory: Path):
        yield {"type": "server.connected"}
        async for event in super().events(directory=directory):
            yield event

    async def health(self) -> dict[str, object]:
        return {"healthy": True, "version": self.version}

    async def session(self, session_id: str, *, directory: Path) -> dict[str, object]:
        return {
            "id": session_id,
            "version": self.version,
            "directory": self.directory,
        }


def identity() -> SessionIdentity:
    return SessionIdentity(
        foreman_session_id="foreman-opencode-1",
        provider_id="opencode",
        provider_session_id="ses_opencode_1",
        repository="/tmp/repository",
        provider_version=OPENCODE_VERSION,
        bridge_id=BRIDGE_ID,
        bridge_version=BRIDGE_VERSION,
    )


def bridge() -> tuple[OpenCodeServerBridge, FakeClient]:
    client = FakeClient()
    return (
        OpenCodeServerBridge(
            client=client,  # type: ignore[arg-type]
            identity=identity(),
            repository=Path("/tmp/repository"),
        ),
        client,
    )


def request(intent: object, *, session: SessionIdentity | None = None) -> ControlRequest:
    return ControlRequest(
        session=session or identity(),
        command_id="control-1",
        intent=intent,  # type: ignore[arg-type]
    )


def test_capabilities_are_complete_and_do_not_invent_steering_or_stop() -> None:
    capabilities = opencode_server_capabilities()

    assert {item.capability for item in capabilities.declarations} == set(BridgeCapability)
    assert (
        capabilities.availability(BridgeCapability.QUEUE_FOLLOW_UP)
        is CapabilityAvailability.SUPPORTED
    )
    assert (
        capabilities.availability(BridgeCapability.INTERRUPT_TURN)
        is CapabilityAvailability.SUPPORTED
    )
    assert (
        capabilities.availability(BridgeCapability.REPLY_TO_APPROVAL)
        is CapabilityAvailability.SUPPORTED
    )
    assert (
        capabilities.availability(BridgeCapability.STEER_ACTIVE_TURN)
        is CapabilityAvailability.UNSUPPORTED
    )
    assert (
        capabilities.availability(BridgeCapability.STOP_SESSION)
        is CapabilityAvailability.UNSUPPORTED
    )
    declarations = {item.capability: item for item in capabilities.declarations}
    assert declarations[BridgeCapability.QUEUE_FOLLOW_UP].stability.value == "experimental"
    assert declarations[BridgeCapability.REPLY_TO_APPROVAL].stability.value == "experimental"
    assert declarations[BridgeCapability.INTERRUPT_TURN].stability.value == "stable"


@pytest.mark.asyncio
async def test_supported_controls_use_exact_server_endpoints() -> None:
    adapter, client = bridge()

    follow_up = await adapter.execute(
        request(QueueFollowUp(message="Run the focused tests."), session=adapter.identity)
    )
    interrupt = await adapter.execute(
        request(InterruptTurn(reason="Boundary denied the action."), session=adapter.identity)
    )

    assert follow_up.outcome is ControlOutcome.EXECUTED
    assert interrupt.outcome is ControlOutcome.EXECUTED
    assert client.requests == [
        (
            "POST",
            "/session/ses_opencode_1/prompt_async",
            {"parts": [{"type": "text", "text": "Run the focused tests."}]},
        ),
        ("POST", "/session/ses_opencode_1/abort", None),
    ]


@pytest.mark.asyncio
async def test_approval_reply_is_one_time_and_bound_to_an_observed_request() -> None:
    adapter, client = bridge()
    approval = ReplyToApproval(
        approval_id="per_1",
        decision=ApprovalDecision.APPROVE,
        reason="The operator approved this exact request.",
    )

    rejected = await adapter.execute(request(approval, session=adapter.identity))
    adapter._normalize(
        {
            "id": "evt_permission",
            "type": "permission.asked",
            "properties": {
                "id": "per_1",
                "sessionID": "ses_opencode_1",
                "permission": "bash",
                "patterns": ["secret command"],
                "metadata": {"arguments": "secret"},
                "always": [],
            },
        }
    )
    executed = await adapter.execute(request(approval, session=adapter.identity))

    assert rejected.outcome is ControlOutcome.REJECTED
    assert executed.outcome is ControlOutcome.EXECUTED
    assert client.requests == [
        ("POST", "/permission/per_1/reply", {"reply": "once"}),
    ]
    assert "per_1" not in adapter._pending_approvals


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent",
    [
        SteerActiveTurn(message="Change direction."),
        StopSession(reason="Stop the whole session."),
    ],
)
async def test_unsupported_controls_are_not_dispatched(intent: object) -> None:
    adapter, client = bridge()

    result = await adapter.execute(request(intent, session=adapter.identity))

    assert result.outcome is ControlOutcome.UNSUPPORTED
    assert client.requests == []


@pytest.mark.asyncio
async def test_foreign_session_control_is_rejected_before_dispatch() -> None:
    adapter, client = bridge()
    foreign = identity().model_copy(update={"foreman_session_id": "other"})

    result = await adapter.execute(
        request(InterruptTurn(reason="Do not dispatch."), session=foreign)
    )

    assert result.outcome is ControlOutcome.REJECTED
    assert client.requests == []


@pytest.mark.asyncio
async def test_attach_requires_exact_version_and_repository() -> None:
    valid_client = AttachableFakeClient()
    adapter = await OpenCodeServerBridge.attach_connected(
        client=valid_client,  # type: ignore[arg-type]
        provider_session_id="ses_opencode_1",
        foreman_session_id="foreman-opencode-1",
        repository=Path("/tmp/repository"),
    )

    assert adapter.identity.provider_version == OPENCODE_VERSION
    assert adapter._events[0].event_type is SupervisionEventType.SESSION_STARTED
    await adapter.close()

    with pytest.raises(OpenCodeError, match="unsupported OpenCode server version"):
        await OpenCodeServerBridge.attach_connected(
            client=AttachableFakeClient(version="1.18.29"),  # type: ignore[arg-type]
            provider_session_id="ses_opencode_1",
            foreman_session_id="foreman-opencode-old",
            repository=Path("/tmp/repository"),
        )
    with pytest.raises(OpenCodeError, match="different repository"):
        await OpenCodeServerBridge.attach_connected(
            client=AttachableFakeClient(directory="/tmp/other"),  # type: ignore[arg-type]
            provider_session_id="ses_opencode_1",
            foreman_session_id="foreman-opencode-other",
            repository=Path("/tmp/repository"),
        )


def test_native_events_are_session_filtered_reducer_compatible_and_content_free() -> None:
    adapter, _ = bridge()
    native_events = [
        {
            "id": "evt_step_start",
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_opencode_1",
                "part": {
                    "type": "step-start",
                    "sessionID": "ses_opencode_1",
                    "messageID": "msg_1",
                    "snapshot": "secret snapshot",
                },
            },
        },
        {
            "id": "evt_tool_pending",
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_opencode_1",
                "part": {
                    "type": "tool",
                    "sessionID": "ses_opencode_1",
                    "messageID": "msg_1",
                    "callID": "call_1",
                    "tool": "bash",
                    "state": {"status": "pending", "input": {"command": "secret command"}},
                },
            },
        },
        {
            "id": "evt_tool_running",
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_opencode_1",
                "part": {
                    "type": "tool",
                    "sessionID": "ses_opencode_1",
                    "messageID": "msg_1",
                    "callID": "call_1",
                    "tool": "bash",
                    "state": {"status": "running", "input": {"command": "secret command"}},
                },
            },
        },
        {
            "id": "evt_other",
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_other",
                "part": {
                    "type": "tool",
                    "sessionID": "ses_other",
                    "messageID": "msg_other",
                    "callID": "call_other",
                    "tool": "bash",
                    "state": {"status": "error", "error": "secret error"},
                },
            },
        },
        {
            "id": "evt_tool_end",
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_opencode_1",
                "part": {
                    "type": "tool",
                    "sessionID": "ses_opencode_1",
                    "messageID": "msg_1",
                    "callID": "call_1",
                    "tool": "bash",
                    "state": {"status": "completed", "output": "secret output"},
                },
            },
        },
        {
            "id": "evt_step_end",
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_opencode_1",
                "part": {
                    "type": "step-finish",
                    "sessionID": "ses_opencode_1",
                    "messageID": "msg_1",
                    "reason": "stop",
                    "snapshot": "secret snapshot",
                },
            },
        },
        {
            "id": "evt_idle",
            "type": "session.idle",
            "properties": {"sessionID": "ses_opencode_1"},
        },
    ]
    for native in native_events:
        adapter._normalize(native)

    state = replay_session(adapter.identity, adapter._events)

    assert [event.sequence for event in adapter._events] == [1, 2, 3, 4, 5]
    assert state.turns_started == 1
    assert state.turns_completed == 1
    assert state.completed_tools == 1
    assert state.failed_tools == 0
    assert state.progress.value == "idle"
    assert all(event.sensitivity is EventSensitivity.METADATA for event in adapter._events)
    persisted = "".join(event.model_dump_json() for event in adapter._events)
    assert "secret" not in persisted


def test_message_and_approval_events_keep_only_provider_neutral_metadata() -> None:
    adapter, _ = bridge()
    adapter._normalize(
        {
            "id": "evt_user",
            "type": "message.updated",
            "properties": {
                "sessionID": "ses_opencode_1",
                "info": {"id": "msg_user", "role": "user", "content": "secret prompt"},
            },
        }
    )
    adapter._normalize(
        {
            "id": "evt_approval",
            "type": "permission.v2.asked",
            "properties": {
                "id": "per_2",
                "sessionID": "ses_opencode_1",
                "action": "bash",
                "resources": ["secret resource"],
            },
        }
    )

    assert adapter._events[0].event_type is SupervisionEventType.USER_PROMPT_SUBMITTED
    assert adapter._events[0].payload == {"role": "user", "message_id": "msg_user"}
    assert adapter._events[1].event_type is SupervisionEventType.APPROVAL_REQUESTED
    assert adapter._events[1].payload == {"approval_id": "per_2"}
    assert "secret" not in "".join(event.model_dump_json() for event in adapter._events)


def test_client_rejects_non_loopback_and_credentials_in_url() -> None:
    with pytest.raises(OpenCodeError, match="loopback"):
        OpenCodeClient("http://example.com:4096", username="foreman", password="secret")
    with pytest.raises(OpenCodeError, match="credentials"):
        OpenCodeClient("http://user:secret@127.0.0.1:4096", username="foreman", password="secret")


def test_attached_tui_command_uses_native_attach_without_putting_password_in_argv() -> None:
    command = attached_tui_command(
        executable=Path("/usr/local/bin/opencode"),
        base_url="http://127.0.0.1:4096",
        repository=Path("/tmp/repository"),
        session_id="ses_opencode_1",
    )

    assert command == (
        "/usr/local/bin/opencode",
        "attach",
        "http://127.0.0.1:4096",
        "--dir",
        "/tmp/repository",
        "--session",
        "ses_opencode_1",
    )
    assert "password" not in " ".join(command).lower()


@pytest.mark.asyncio
async def test_attachment_requires_working_observation_stream() -> None:
    class UnavailableClient(AttachableFakeClient):
        async def events(self, *, directory: Path):
            raise OpenCodeError("unavailable")
            yield

    client = UnavailableClient()
    with pytest.raises(OpenCodeError, match="observation stream"):
        await OpenCodeServerBridge.attach_connected(
            client=client,
            provider_session_id="ses_opencode_1",
            foreman_session_id="foreman-opencode-1",
            repository=Path("/tmp/repository"),
        )
    assert client.closed


@pytest.mark.asyncio
async def test_cancelling_attachment_closes_observer_not_native_session() -> None:
    class WaitingClient(AttachableFakeClient):
        async def events(self, *, directory: Path):
            await asyncio.Event().wait()
            yield

    client = WaitingClient()
    task = asyncio.create_task(
        OpenCodeServerBridge.attach_connected(
            client=client,
            provider_session_id="ses_opencode_1",
            foreman_session_id="foreman-opencode-1",
            repository=Path("/tmp/repository"),
        )
    )
    # Stop at a deterministic point after the event stream opens.
    opened = asyncio.Event()
    original = client.events

    async def events(*, directory):
        opened.set()
        async for event in original(directory=directory):
            yield event

    client.events = events
    await asyncio.wait_for(opened.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.closed
    assert client.requests == []


@pytest.mark.asyncio
async def test_attach_rejects_relative_native_repository() -> None:
    with pytest.raises(OpenCodeError, match="absolute directory"):
        await OpenCodeServerBridge.attach_connected(
            client=AttachableFakeClient(directory="."),
            provider_session_id="ses_opencode_1",
            foreman_session_id="foreman-opencode-1",
            repository=Path.cwd(),
        )
