from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from foreman.bridges import (
    AgentBridge,
    BridgeContractError,
    capability_for,
    required_capability,
    validate_connection,
    validate_control_result,
    validate_event_batch,
)
from foreman.models import (
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
    QueueFollowUp,
    ReplyToApproval,
    SessionIdentity,
    StopSession,
    SupervisionEvent,
    SupervisionEventType,
)


def identity(provider_id: str = "prime-agent") -> SessionIdentity:
    return SessionIdentity(
        foreman_session_id="foreman-1",
        provider_id=provider_id,
        provider_session_id="provider-1",
        repository="/tmp/project",
        bridge_id="prime-daemon-v4",
        bridge_version="1.0.0",
    )


def event(session: SessionIdentity, sequence: int) -> SupervisionEvent:
    return SupervisionEvent(
        session=session,
        sequence=sequence,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        event_type=SupervisionEventType.TURN_STARTED,
        provenance=EventProvenance(
            source=BridgeSource.DAEMON,
            native_event_type="turn_started",
        ),
    )


def capabilities(*items: BridgeCapability) -> CapabilitySet:
    return CapabilitySet(
        provider_id="prime-agent",
        bridge_id="prime-daemon-v4",
        bridge_version="1.0.0",
        declarations=tuple(
            CapabilityDeclaration(
                capability=item,
                availability=CapabilityAvailability.SUPPORTED,
                stability=BridgeStability.STABLE,
                evidence="Public Daemon Protocol v4",
            )
            for item in items
        ),
    )


class FakeBridge:
    def __init__(self) -> None:
        self.identity = identity()
        self.capabilities = capabilities(BridgeCapability.QUEUE_FOLLOW_UP)
        self.closed = False
        self.last_event_sequence = 3

    async def events(self, *, after_sequence: int = 0) -> AsyncIterator[SupervisionEvent]:
        for sequence in (1, 2, 3):
            if sequence > after_sequence:
                yield event(self.identity, sequence)

    async def execute(self, request: ControlRequest) -> ControlResult:
        supported = self.capabilities.supports(capability_for(request.intent))
        outcome = ControlOutcome.EXECUTED if supported else ControlOutcome.UNSUPPORTED
        return ControlResult(
            foreman_session_id=request.session.foreman_session_id,
            provider_id=request.session.provider_id,
            command_id=request.command_id,
            action=request.intent.action,
            outcome=outcome,
            detail="" if supported else "control is unsupported",
        )

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_bridge_protocol_and_replay_cursor_are_reusable() -> None:
    bridge = FakeBridge()

    assert isinstance(bridge, AgentBridge)
    validate_connection(bridge.identity, bridge.capabilities)
    events = [item async for item in bridge.events(after_sequence=1)]
    validate_event_batch(bridge.identity, events, after_sequence=1)

    assert [item.sequence for item in events] == [2, 3]
    await bridge.close()
    assert bridge.closed is True


def test_every_control_action_has_one_required_capability() -> None:
    mapped = {action: required_capability(action) for action in ControlAction}

    assert set(mapped) == set(ControlAction)
    assert all(isinstance(item, BridgeCapability) for item in mapped.values())


def test_connection_rejects_capabilities_for_another_provider() -> None:
    with pytest.raises(BridgeContractError, match="connected provider"):
        validate_connection(identity("claude"), capabilities())


@pytest.mark.parametrize(
    ("sequences", "after_sequence"),
    [((1, 1), 0), ((2, 1), 0), ((1, 2), 1), ((1, 3), 0)],
)
def test_event_batch_rejects_duplicate_reordered_or_stale_events(sequences, after_sequence) -> None:
    session = identity()
    events = [event(session, sequence) for sequence in sequences]

    with pytest.raises(BridgeContractError, match="strictly increasing"):
        validate_event_batch(session, events, after_sequence=after_sequence)


def test_event_batch_rejects_an_event_from_another_session() -> None:
    session = identity()

    with pytest.raises(BridgeContractError, match="session identity"):
        validate_event_batch(session, [event(identity("claude"), 1)])


@pytest.mark.asyncio
async def test_supported_control_result_matches_capability_declaration() -> None:
    bridge = FakeBridge()
    request = ControlRequest(
        session=bridge.identity,
        command_id="command-1",
        intent=QueueFollowUp(message="Run the focused tests."),
    )

    result = await bridge.execute(request)
    validate_control_result(request, result, bridge.capabilities)

    assert result.outcome is ControlOutcome.EXECUTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent",
    [
        StopSession(reason="Operator requested cancellation."),
        ReplyToApproval(
            approval_id="approval-2",
            decision=ApprovalDecision.DENY,
            reason="Approval control is unavailable.",
        ),
    ],
)
async def test_unsupported_control_and_cancellation_are_explicit(intent) -> None:
    bridge = FakeBridge()
    request = ControlRequest(
        session=bridge.identity,
        command_id=f"unsupported-{intent.action.value}",
        intent=intent,
    )

    result = await bridge.execute(request)
    validate_control_result(request, result, bridge.capabilities)

    assert result.outcome is ControlOutcome.UNSUPPORTED


def test_control_result_rejects_undeclared_execution() -> None:
    request = ControlRequest(
        session=identity(),
        command_id="command-2",
        intent=ReplyToApproval(
            approval_id="approval-1",
            decision=ApprovalDecision.DENY,
            reason="Upload is forbidden.",
        ),
    )
    result = ControlResult(
        foreman_session_id=request.session.foreman_session_id,
        provider_id=request.session.provider_id,
        command_id=request.command_id,
        action=request.intent.action,
        outcome=ControlOutcome.EXECUTED,
    )

    with pytest.raises(BridgeContractError, match="did not declare"):
        validate_control_result(request, result, capabilities())


def test_control_result_rejects_false_unsupported_claim() -> None:
    request = ControlRequest(
        session=identity(),
        command_id="command-3",
        intent=QueueFollowUp(message="Run tests."),
    )
    result = ControlResult(
        foreman_session_id=request.session.foreman_session_id,
        provider_id=request.session.provider_id,
        command_id=request.command_id,
        action=request.intent.action,
        outcome=ControlOutcome.UNSUPPORTED,
        detail="unsupported",
    )

    with pytest.raises(BridgeContractError, match="declared as supported"):
        validate_control_result(
            request,
            result,
            capabilities(BridgeCapability.QUEUE_FOLLOW_UP),
        )
