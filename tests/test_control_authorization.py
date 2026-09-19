from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from foreman.models import (
    ApprovalDecision,
    AssessmentProvenance,
    AuthorizationOutcome,
    AuthorizationReason,
    BoundaryAction,
    BoundaryOperation,
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
    HumanApprovalEvidence,
    InferenceMetadata,
    QueueFollowUp,
    SessionIdentity,
    StopSession,
    SupervisionAssessment,
    SupervisionEvent,
    SupervisionEventType,
)
from foreman.models.rollout import RolloutMode, RolloutPolicy
from foreman.supervision import (
    AUTHORITATIVE_MODEL_CHECKPOINT,
    AUTHORITATIVE_PROVIDER_ID,
    CHECKPOINT_QUESTIONS_VERSION,
    AuthorizedControlDispatcher,
    BoundaryPolicy,
    CheckpointSelector,
    ControlAuthorizationGate,
    SessionReducer,
    control_request_sha256,
)
from foreman.supervision.delivery import DeliveryLedger


def identity() -> SessionIdentity:
    return SessionIdentity(
        foreman_session_id="session-1",
        provider_id="prime-agent",
        provider_session_id="prime-1",
        repository="/tmp/project",
        provider_version="0.9.5",
        bridge_id="prime-daemon-v4",
        bridge_version="1.0.0",
    )


def request(session: SessionIdentity, *, stop: bool = False) -> ControlRequest:
    return ControlRequest(
        session=session,
        command_id="command-1",
        intent=StopSession(reason="unsafe") if stop else QueueFollowUp(message="continue"),
    )


def capabilities(
    session: SessionIdentity,
    action: BridgeCapability,
    availability: CapabilityAvailability = CapabilityAvailability.SUPPORTED,
    stability: BridgeStability | None = BridgeStability.STABLE,
) -> CapabilitySet:
    return CapabilitySet(
        provider_id=session.provider_id,
        provider_version=session.provider_version,
        bridge_id=session.bridge_id,
        bridge_version=session.bridge_version,
        declarations=(
            CapabilityDeclaration(
                capability=action,
                availability=availability,
                stability=stability,
                evidence="version-pinned test",
            ),
        ),
    )


def context(session: SessionIdentity):
    reducer = SessionReducer(session)
    reducer.apply(
        SupervisionEvent(
            session=session,
            sequence=1,
            event_type=SupervisionEventType.SESSION_STARTED,
            provenance=EventProvenance(
                source=BridgeSource.DAEMON,
                native_event_type="session_started",
            ),
        )
    )
    trigger = SupervisionEvent(
        session=session,
        sequence=2,
        event_type=SupervisionEventType.PLAN_UPDATED,
        provenance=EventProvenance(
            source=BridgeSource.DAEMON,
            native_event_type="plan_updated",
        ),
    )
    reducer.apply(trigger)
    return trigger, reducer.snapshot()


def boundary(control: ControlRequest, operation: BoundaryOperation):
    return BoundaryPolicy().classify(
        BoundaryAction(
            action_id=control.command_id,
            session=control.session,
            operation=operation,
            target_sha256=control_request_sha256(control),
        )
    )


def approval(control: ControlRequest, now: datetime) -> HumanApprovalEvidence:
    return HumanApprovalEvidence(
        approval_id="human-1",
        request_sha256=control_request_sha256(control),
        decision=ApprovalDecision.APPROVE,
        approved_by="local-operator",
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )


def assessment_for(checkpoint) -> SupervisionAssessment:
    return SupervisionAssessment(
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_sequence=checkpoint.sequence,
        meaningful_progress=0.5,
        work_stuck=0.1,
        work_off_track=0.1,
        verification_sufficient=0.5,
        completion_supported=0.5,
        safe_to_continue=0.7,
        needs_human=0.2,
        provenance=AssessmentProvenance(
            provider_id=AUTHORITATIVE_PROVIDER_ID,
            role="authoritative",
            implementation="typesafe-sdk",
            endpoint="http://127.0.0.1:8080",
            request_model="jev-latest",
            checkpoint=AUTHORITATIVE_MODEL_CHECKPOINT,
            question_version=CHECKPOINT_QUESTIONS_VERSION,
            inference=InferenceMetadata(max_retries=2, latency_seconds=0.1),
        ),
    )


def test_stable_supported_low_risk_control_needs_opt_in_approval() -> None:
    session = identity()
    control = request(session)
    _, state = context(session)

    result = ControlAuthorizationGate(RolloutPolicy(mode=RolloutMode.APPROVAL_REQUIRED)).authorize(
        control,
        capabilities=capabilities(session, BridgeCapability.QUEUE_FOLLOW_UP),
        state=state,
        boundary=boundary(control, BoundaryOperation.READ_REPOSITORY),
    )

    assert result.outcome is AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED
    assert result.reason is AuthorizationReason.HUMAN_APPROVAL_REQUIRED


@pytest.mark.parametrize(
    ("availability", "reason"),
    [
        (CapabilityAvailability.UNSUPPORTED, AuthorizationReason.CAPABILITY_UNSUPPORTED),
        (CapabilityAvailability.UNKNOWN, AuthorizationReason.CAPABILITY_UNKNOWN),
    ],
)
def test_unavailable_capabilities_are_denied(availability, reason) -> None:
    session = identity()
    control = request(session)
    _, state = context(session)
    result = ControlAuthorizationGate(RolloutPolicy(mode=RolloutMode.APPROVAL_REQUIRED)).authorize(
        control,
        capabilities=capabilities(session, BridgeCapability.QUEUE_FOLLOW_UP, availability),
        state=state,
        boundary=boundary(control, BoundaryOperation.READ_REPOSITORY),
    )
    assert result.outcome is AuthorizationOutcome.DENIED
    assert result.reason is reason


def test_forbidden_boundary_cannot_be_overridden_by_human_approval() -> None:
    session = identity()
    control = request(session)
    _, state = context(session)
    now = datetime.now(UTC)
    result = ControlAuthorizationGate(RolloutPolicy(mode=RolloutMode.APPROVAL_REQUIRED)).authorize(
        control,
        capabilities=capabilities(session, BridgeCapability.QUEUE_FOLLOW_UP),
        state=state,
        boundary=boundary(control, BoundaryOperation.BYPASS_SECURITY_CONTROL),
        human_approval=approval(control, now),
        now=now,
    )
    assert result.outcome is AuthorizationOutcome.DENIED
    assert result.reason is AuthorizationReason.BOUNDARY_FORBIDDEN


def test_review_required_control_needs_current_semantic_and_human_evidence() -> None:
    session = identity()
    control = request(session)
    trigger, state = context(session)
    risk = boundary(control, BoundaryOperation.NETWORK_WRITE)
    caps = capabilities(session, BridgeCapability.QUEUE_FOLLOW_UP)
    gate = ControlAuthorizationGate(RolloutPolicy(mode=RolloutMode.APPROVAL_REQUIRED))

    missing = gate.authorize(control, capabilities=caps, state=state, boundary=risk)
    assert missing.reason is AuthorizationReason.SEMANTIC_EVIDENCE_REQUIRED

    checkpoint = CheckpointSelector().select(trigger, state, boundary_decision=risk)
    assert checkpoint is not None
    semantic = assessment_for(checkpoint)
    waiting = gate.authorize(
        control,
        capabilities=caps,
        state=state,
        boundary=risk,
        checkpoint=checkpoint,
        assessment=semantic,
    )
    assert waiting.outcome is AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED

    now = datetime.now(UTC)
    authorized = gate.authorize(
        control,
        capabilities=caps,
        state=state,
        boundary=risk,
        checkpoint=checkpoint,
        assessment=semantic,
        human_approval=approval(control, now),
        now=now,
    )
    assert authorized.outcome is AuthorizationOutcome.AUTHORIZED
    assert authorized.checkpoint_id == checkpoint.checkpoint_id
    assert authorized.human_approval_id == "human-1"


def test_disruptive_or_experimental_controls_require_human_approval() -> None:
    session = identity()
    control = request(session, stop=True)
    _, state = context(session)
    result = ControlAuthorizationGate(RolloutPolicy(mode=RolloutMode.APPROVAL_REQUIRED)).authorize(
        control,
        capabilities=capabilities(session, BridgeCapability.STOP_SESSION),
        state=state,
        boundary=boundary(control, BoundaryOperation.READ_REPOSITORY),
    )
    assert result.outcome is AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED

    follow_up = request(session)
    experimental = ControlAuthorizationGate(
        RolloutPolicy(mode=RolloutMode.APPROVAL_REQUIRED)
    ).authorize(
        follow_up,
        capabilities=capabilities(
            session,
            BridgeCapability.QUEUE_FOLLOW_UP,
            stability=BridgeStability.EXPERIMENTAL,
        ),
        state=state,
        boundary=boundary(follow_up, BoundaryOperation.READ_REPOSITORY),
    )
    assert experimental.outcome is AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED


def test_human_approval_must_be_current_and_bound_to_the_exact_request() -> None:
    session = identity()
    control = request(session, stop=True)
    _, state = context(session)
    now = datetime.now(UTC)
    evidence = approval(control, now).model_copy(update={"request_sha256": "f" * 64})

    result = ControlAuthorizationGate(RolloutPolicy(mode=RolloutMode.APPROVAL_REQUIRED)).authorize(
        control,
        capabilities=capabilities(session, BridgeCapability.STOP_SESSION),
        state=state,
        boundary=boundary(control, BoundaryOperation.READ_REPOSITORY),
        human_approval=evidence,
        now=now,
    )

    assert result.outcome is AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED
    assert result.reason is AuthorizationReason.HUMAN_APPROVAL_INVALID


class Bridge:
    def __init__(self, session: SessionIdentity) -> None:
        self.identity = session
        self.capabilities = capabilities(session, BridgeCapability.QUEUE_FOLLOW_UP)
        self.calls = 0
        self.last_event_sequence = 2

    async def execute(self, control: ControlRequest) -> ControlResult:
        self.calls += 1
        return ControlResult(
            foreman_session_id=control.session.foreman_session_id,
            provider_id=control.session.provider_id,
            command_id=control.command_id,
            action=control.intent.action,
            outcome=ControlOutcome.EXECUTED,
        )


@pytest.mark.asyncio
async def test_dispatcher_never_calls_bridge_when_gate_denies(tmp_path) -> None:
    session = identity().model_copy(update={"repository": str(tmp_path.resolve())})
    control = request(session)
    _, state = context(session)
    bridge = Bridge(session)
    dispatcher = AuthorizedControlDispatcher(
        bridge,
        ControlAuthorizationGate(RolloutPolicy(mode=RolloutMode.APPROVAL_REQUIRED)),
        ledger=DeliveryLedger(tmp_path / "delivery"),
    )

    denied = await dispatcher.dispatch(
        control,
        state=state,
        boundary=boundary(control, BoundaryOperation.EXPOSE_CREDENTIAL),
    )
    assert denied.result is None
    assert bridge.calls == 0

    allowed = await dispatcher.dispatch(
        control,
        state=state,
        human_approval=approval(control, datetime.now(UTC)),
        boundary=boundary(control, BoundaryOperation.READ_REPOSITORY),
    )
    assert allowed.result is not None
    assert bridge.calls == 1


def test_human_approval_issued_in_the_future_is_invalid() -> None:
    session = identity()
    control = request(session, stop=True)
    _, state = context(session)
    now = datetime.now(UTC)
    future = approval(control, now + timedelta(minutes=1))

    result = ControlAuthorizationGate(RolloutPolicy(mode=RolloutMode.APPROVAL_REQUIRED)).authorize(
        control,
        capabilities=capabilities(session, BridgeCapability.STOP_SESSION),
        state=state,
        boundary=boundary(control, BoundaryOperation.READ_REPOSITORY),
        human_approval=future,
        now=now,
    )

    assert result.outcome is AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED
    assert result.reason is AuthorizationReason.HUMAN_APPROVAL_INVALID
