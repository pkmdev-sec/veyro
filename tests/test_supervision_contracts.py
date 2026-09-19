from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from foreman.models import (
    PROTOCOL_VERSION,
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
    QueueFollowUp,
    RawProviderEvent,
    ReplyToApproval,
    SessionIdentity,
    StopSession,
    SupervisionEvent,
    SupervisionEventType,
)


def session() -> SessionIdentity:
    return SessionIdentity(
        foreman_session_id="foreman-1",
        provider_id="prime-agent",
        provider_session_id="prime-1",
        repository="/tmp/project",
        provider_version="0.9.5",
        bridge_id="prime-daemon-v4",
        bridge_version="1.0.0",
    )


def test_session_identity_is_versioned_and_round_trips() -> None:
    identity = session()

    restored = SessionIdentity.model_validate_json(identity.model_dump_json())

    assert restored == identity
    assert restored.protocol_version == PROTOCOL_VERSION


def test_supervision_event_preserves_order_provenance_and_privacy() -> None:
    event = SupervisionEvent(
        session=session(),
        sequence=7,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        event_type=SupervisionEventType.TOOL_COMPLETED,
        payload={"tool": "pytest", "exit_code": 0},
        provenance=EventProvenance(
            source=BridgeSource.DAEMON,
            native_event_type="tool_end",
            native_event_id="event-7",
            raw_event_sha256="a" * 64,
            replayed=True,
        ),
        sensitivity=EventSensitivity.METADATA,
    )

    restored = SupervisionEvent.model_validate_json(event.model_dump_json())

    assert restored == event
    assert restored.sequence == 7
    assert restored.provenance.replayed is True
    assert restored.sensitivity is EventSensitivity.METADATA


def test_supervision_event_rejects_invalid_raw_event_hash() -> None:
    with pytest.raises(ValidationError, match="raw_event_sha256"):
        EventProvenance(
            source=BridgeSource.HOOK,
            native_event_type="PostToolUse",
            raw_event_sha256="not-a-sha256",
        )


def test_capabilities_require_unique_evidence_backed_declarations() -> None:
    steer = CapabilityDeclaration(
        capability=BridgeCapability.STEER_ACTIVE_TURN,
        availability=CapabilityAvailability.SUPPORTED,
        stability=BridgeStability.STABLE,
        evidence="Public Daemon Protocol v4",
    )
    capabilities = CapabilitySet(
        provider_id="prime-agent",
        bridge_id="prime-daemon-v4",
        bridge_version="1.0.0",
        declarations=(steer,),
    )

    assert capabilities.supports(BridgeCapability.STEER_ACTIVE_TURN)
    assert (
        capabilities.availability(BridgeCapability.REPLY_TO_APPROVAL)
        is CapabilityAvailability.UNKNOWN
    )

    with pytest.raises(ValidationError, match="unique"):
        CapabilitySet(
            provider_id="prime-agent",
            bridge_id="prime-daemon-v4",
            bridge_version="1.0.0",
            declarations=(steer, steer),
        )


def test_control_request_uses_a_discriminated_intent() -> None:
    request = ControlRequest(
        session=session(),
        command_id="command-1",
        intent=QueueFollowUp(message="Run the focused tests before finishing."),
    )

    restored = ControlRequest.model_validate_json(request.model_dump_json())

    assert isinstance(restored.intent, QueueFollowUp)
    assert restored.intent.action is ControlAction.QUEUE_FOLLOW_UP


def test_approval_control_requires_an_explicit_decision() -> None:
    request = ControlRequest(
        session=session(),
        command_id="command-2",
        intent=ReplyToApproval(
            approval_id="approval-1",
            decision=ApprovalDecision.DENY,
            reason="Network upload is not allowed.",
        ),
    )

    assert request.intent.decision is ApprovalDecision.DENY


def test_control_result_requires_detail_when_not_executed() -> None:
    with pytest.raises(ValidationError, match="detail"):
        ControlResult(
            foreman_session_id="foreman-1",
            provider_id="claude",
            command_id="command-3",
            action=ControlAction.STOP_SESSION,
            outcome=ControlOutcome.UNSUPPORTED,
        )

    result = ControlResult(
        foreman_session_id="foreman-1",
        provider_id="claude",
        command_id="command-3",
        action=ControlAction.STOP_SESSION,
        outcome=ControlOutcome.UNSUPPORTED,
        detail="The bridge cannot stop independently attached sessions.",
    )
    assert result.outcome is ControlOutcome.UNSUPPORTED


def test_control_variants_make_message_and_reason_requirements_explicit() -> None:
    with pytest.raises(ValidationError):
        QueueFollowUp(message="")
    with pytest.raises(ValidationError):
        StopSession(reason="")


def test_raw_provider_content_requires_explicit_opt_in() -> None:
    values = {
        "session": session(),
        "native_event_type": "agent_message",
        "payload_sha256": "b" * 64,
        "payload": {"message": "private content"},
    }

    with pytest.raises(ValidationError, match="content_opt_in"):
        RawProviderEvent(**values)

    event = RawProviderEvent(
        **values,
        sensitivity=EventSensitivity.CONTENT_OPT_IN,
    )
    assert event.payload == {"message": "private content"}
