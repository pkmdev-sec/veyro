from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from foreman.bridges.prime_agent import prime_daemon_capabilities
from foreman.models import (
    AssessmentProvenance,
    AuthorizationOutcome,
    BoundaryAction,
    BoundaryOperation,
    ControlOutcome,
    ControlRequest,
    ControlResult,
    EventProvenance,
    HumanApprovalEvidence,
    InferenceMetadata,
    SessionIdentity,
    StopSession,
    SupervisionAssessment,
    SupervisionEvent,
    SupervisionEventType,
)
from foreman.models.rollout import RolloutPolicy
from foreman.supervision import CheckpointAssessmentService, control_request_sha256
from foreman.supervision.control_loop import ControlLoopError, SupervisionControlLoop
from foreman.supervision.delivery import DeliveryLedger


def identity(repository: Path = Path("/tmp/repository")) -> SessionIdentity:
    return SessionIdentity(
        foreman_session_id="prime-loop-1",
        provider_id="prime-agent",
        provider_session_id="prime-native-1",
        repository=str(repository),
        provider_version="0.9.5",
        bridge_id="prime-agent-daemon",
        bridge_version="7.29",
    )


def event(
    session: SessionIdentity, sequence: int, event_type: SupervisionEventType
) -> SupervisionEvent:
    payload = {"reason": "killed"} if event_type is SupervisionEventType.SESSION_FAILED else {}
    return SupervisionEvent(
        session=session,
        sequence=sequence,
        event_type=event_type,
        payload=payload,
        provenance=EventProvenance(
            source="daemon",
            native_event_type=event_type.value,
        ),
    )


class FakeAssessor:
    calls = 0

    async def assess(self, checkpoint, state, *, task_context=None) -> SupervisionAssessment:
        self.calls += 1
        return SupervisionAssessment(
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_sequence=checkpoint.sequence,
            meaningful_progress=0.5,
            work_stuck=0.0,
            work_off_track=0.0,
            verification_sufficient=0.5,
            completion_supported=0.5,
            safe_to_continue=0.9,
            needs_human=0.1,
            provenance=AssessmentProvenance(
                provider_id="localjev-qwen3-14b",
                role="authoritative",
                implementation="fake-localjev",
                endpoint="http://127.0.0.1:8080",
                request_model="jev-latest",
                checkpoint=(
                    "qwen3:14b@sha256:"
                    "bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8"
                ),
                question_version="supervision-checkpoint-v1",
                inference=InferenceMetadata(
                    timeout_seconds=1,
                    max_retries=0,
                    latency_seconds=0.01,
                    state_characters=100,
                    state_format="json",
                ),
            ),
        )

    async def close(self) -> None:
        pass


class FakePrimeBridge:
    def __init__(
        self,
        *,
        repository: Path = Path("/tmp/repository"),
        verify_stop: bool = True,
        outcome: ControlOutcome = ControlOutcome.EXECUTED,
    ) -> None:
        self.identity = identity(repository)
        self.capabilities = prime_daemon_capabilities()
        self.verify_stop = verify_stop
        self.outcome = outcome
        self.executed: list[ControlRequest] = []
        self._events = [event(self.identity, 1, SupervisionEventType.SESSION_STARTED)]

    @property
    def last_event_sequence(self):
        return len(self._events)

    async def events(self, *, after_sequence: int = 0) -> AsyncIterator[SupervisionEvent]:
        for item in self._events:
            if item.sequence > after_sequence:
                yield item

    async def execute(self, request: ControlRequest) -> ControlResult:
        self.executed.append(request)
        if self.verify_stop:
            self._events.append(event(self.identity, 2, SupervisionEventType.SESSION_FAILED))
        return ControlResult(
            foreman_session_id=request.session.foreman_session_id,
            provider_id=request.session.provider_id,
            command_id=request.command_id,
            action=request.intent.action,
            outcome=self.outcome,
            provider_command_id="prime-command-1",
            detail="provider control result",
        )

    async def close(self) -> None:
        pass


def request(session: SessionIdentity) -> ControlRequest:
    return ControlRequest(
        session=session,
        command_id="stop-canary-session",
        intent=StopSession(reason="Complete the isolated supervision canary."),
    )


def boundary_action(control: ControlRequest) -> BoundaryAction:
    return BoundaryAction(
        action_id=control.command_id,
        session=control.session,
        operation=BoundaryOperation.UNKNOWN,
        target_sha256=control_request_sha256(control),
    )


def approval(control: ControlRequest, *, now: datetime | None = None) -> HumanApprovalEvidence:
    current = now or datetime.now(UTC)
    return HumanApprovalEvidence(
        approval_id="user-approved-sup009",
        request_sha256=control_request_sha256(control),
        decision="approve",
        approved_by="user:conversation",
        issued_at=current - timedelta(seconds=1),
        expires_at=current + timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_full_control_loop_assesses_authorizes_dispatches_and_verifies(tmp_path) -> None:
    bridge = FakePrimeBridge(repository=tmp_path.resolve())
    control = request(bridge.identity)
    assessor = FakeAssessor()
    loop = SupervisionControlLoop(
        bridge,
        CheckpointAssessmentService(assessor),
        policy=RolloutPolicy(mode="approval_required"),
        ledger=DeliveryLedger(tmp_path / "delivery"),
    )

    evidence = await loop.run_control(
        event=bridge._events[0],
        request=control,
        boundary_action=boundary_action(control),
        human_approval=approval(control),
        task_context="Stop only the disposable Prime Agent canary session.",
    )

    assert evidence.reduced_state.last_sequence == 1
    assert evidence.checkpoint is not None
    assert evidence.checkpoint.kind.value == "risky_action"
    assert evidence.assessment is not None
    assert evidence.assessment.provenance.provider_id == "localjev-qwen3-14b"
    assert evidence.control.authorization.outcome is AuthorizationOutcome.AUTHORIZED
    assert evidence.control.result is not None
    assert evidence.control.result.outcome is ControlOutcome.EXECUTED
    assert evidence.verification_event is not None
    assert evidence.verification_event.payload == {"reason": "killed"}
    assert bridge.executed == [control]
    assert assessor.calls == 1


@pytest.mark.asyncio
async def test_missing_human_approval_blocks_prime_daemon_dispatch(tmp_path) -> None:
    bridge = FakePrimeBridge(repository=tmp_path.resolve())
    control = request(bridge.identity)
    loop = SupervisionControlLoop(
        bridge,
        CheckpointAssessmentService(FakeAssessor()),
        policy=RolloutPolicy(mode="approval_required"),
        ledger=DeliveryLedger(tmp_path / "delivery"),
    )

    evidence = await loop.run_control(
        event=bridge._events[0],
        request=control,
        boundary_action=boundary_action(control),
    )

    assert evidence.control.authorization.outcome is AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED
    assert evidence.control.result is None
    assert evidence.verification_event is None
    assert bridge.executed == []


@pytest.mark.asyncio
async def test_executed_stop_requires_provider_terminal_event(tmp_path) -> None:
    bridge = FakePrimeBridge(repository=tmp_path.resolve(), verify_stop=False)
    control = request(bridge.identity)
    loop = SupervisionControlLoop(
        bridge,
        CheckpointAssessmentService(FakeAssessor()),
        policy=RolloutPolicy(mode="approval_required"),
        ledger=DeliveryLedger(tmp_path / "delivery"),
    )

    with pytest.raises(ControlLoopError, match="closed before stop verification"):
        await loop.run_control(
            event=bridge._events[0],
            request=control,
            boundary_action=boundary_action(control),
            human_approval=approval(control),
        )


@pytest.mark.asyncio
async def test_rejected_stop_returns_without_waiting_for_terminal_event(tmp_path) -> None:
    bridge = FakePrimeBridge(
        repository=tmp_path.resolve(), verify_stop=False, outcome=ControlOutcome.REJECTED
    )
    control = request(bridge.identity)
    loop = SupervisionControlLoop(
        bridge,
        CheckpointAssessmentService(FakeAssessor()),
        policy=RolloutPolicy(mode="approval_required"),
        ledger=DeliveryLedger(tmp_path / "delivery"),
    )

    evidence = await loop.run_control(
        event=bridge._events[0],
        request=control,
        boundary_action=boundary_action(control),
        human_approval=approval(control),
    )

    assert evidence.control.authorization.outcome is AuthorizationOutcome.AUTHORIZED
    assert evidence.control.result is not None
    assert evidence.control.result.outcome is ControlOutcome.REJECTED
    assert evidence.verification_event is None
