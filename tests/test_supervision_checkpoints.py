from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from foreman.foreman import JevForemanModel
from foreman.models import (
    AssessmentProvenance,
    BoundaryAction,
    BoundaryOperation,
    BridgeSource,
    CheckpointKind,
    EventProvenance,
    InferenceMetadata,
    SessionIdentity,
    SupervisionAssessment,
    SupervisionEvent,
    SupervisionEventType,
)
from foreman.supervision import (
    AUTHORITATIVE_MODEL_CHECKPOINT,
    CHECKPOINT_QUESTIONS,
    CHECKPOINT_QUESTIONS_VERSION,
    BoundaryPolicy,
    CheckpointAssessmentService,
    CheckpointSelector,
    LocalJevCheckpointAssessor,
    SessionReducer,
)


def identity() -> SessionIdentity:
    return SessionIdentity(
        foreman_session_id="foreman-1",
        provider_id="prime-agent",
        provider_session_id="prime-1",
        repository="/tmp/project",
        bridge_id="prime-daemon-v4",
        bridge_version="1.0.0",
    )


def event(
    session: SessionIdentity,
    sequence: int,
    event_type: SupervisionEventType,
    payload: dict[str, object] | None = None,
) -> SupervisionEvent:
    return SupervisionEvent(
        session=session,
        sequence=sequence,
        event_type=event_type,
        payload=payload or {},
        provenance=EventProvenance(
            source=BridgeSource.DAEMON,
            native_event_type=event_type.value,
        ),
    )


def reduced(
    event_type: SupervisionEventType,
    payload: dict[str, object] | None = None,
):
    session = identity()
    reducer = SessionReducer(session)
    reducer.apply(event(session, 1, SupervisionEventType.SESSION_STARTED))
    trigger = event(session, 2, event_type, payload)
    reducer.apply(trigger)
    return trigger, reducer.snapshot()


def provenance() -> AssessmentProvenance:
    return AssessmentProvenance(
        provider_id="localjev-qwen3-14b",
        role="authoritative",
        implementation="test",
        endpoint="http://127.0.0.1:8080",
        request_model="jev-latest",
        checkpoint="qwen3:14b@sha256:abc",
        question_version=CHECKPOINT_QUESTIONS_VERSION,
        inference=InferenceMetadata(max_retries=0, latency_seconds=0),
    )


class FakeAssessor:
    def __init__(self) -> None:
        self.calls = []

    async def assess(self, checkpoint, state, *, task_context=None):
        self.calls.append((checkpoint, state, task_context))
        return SupervisionAssessment(
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_sequence=checkpoint.sequence,
            meaningful_progress=0.5,
            work_stuck=0.1,
            work_off_track=0.1,
            verification_sufficient=0.4,
            completion_supported=0.4,
            safe_to_continue=0.8,
            needs_human=0.2,
            provenance=provenance(),
        )

    async def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("event_type", "payload", "kind"),
    [
        (
            SupervisionEventType.TEST_COMPLETED,
            {"check_id": "pytest", "passed": False},
            CheckpointKind.FAILED_VERIFICATION,
        ),
        (SupervisionEventType.SESSION_IDLE, {}, CheckpointKind.IDLE_SESSION),
        (
            SupervisionEventType.AGENT_MESSAGE_COMPLETED,
            {"completion_claim": True, "message": "private"},
            CheckpointKind.COMPLETION_CLAIM,
        ),
    ],
)
def test_selector_emits_only_meaningful_reduced_state_checkpoints(
    event_type: SupervisionEventType,
    payload: dict[str, object],
    kind: CheckpointKind,
) -> None:
    trigger, state = reduced(event_type, payload)

    checkpoint = CheckpointSelector().select(trigger, state)

    assert checkpoint is not None
    assert checkpoint.kind is kind
    assert checkpoint.sequence == state.last_sequence
    assert "private" not in checkpoint.model_dump_json()


def test_selector_requests_semantic_review_only_for_review_required_actions() -> None:
    trigger, state = reduced(SupervisionEventType.PLAN_UPDATED)
    policy = BoundaryPolicy()

    risky = policy.classify(
        BoundaryAction(
            action_id="network-1",
            session=state.session,
            operation=BoundaryOperation.NETWORK_WRITE,
        )
    )
    low = policy.classify(
        BoundaryAction(
            action_id="read-1",
            session=state.session,
            operation=BoundaryOperation.READ_REPOSITORY,
        )
    )
    forbidden = policy.classify(
        BoundaryAction(
            action_id="bypass-1",
            session=state.session,
            operation=BoundaryOperation.BYPASS_SECURITY_CONTROL,
        )
    )

    checkpoint = CheckpointSelector().select(trigger, state, boundary_decision=risky)
    assert checkpoint is not None
    assert checkpoint.kind is CheckpointKind.RISKY_ACTION
    assert CheckpointSelector().select(trigger, state, boundary_decision=low) is None
    assert CheckpointSelector().select(trigger, state, boundary_decision=forbidden) is None


@pytest.mark.asyncio
async def test_service_ignores_noise_and_deduplicates_checkpoint_calls() -> None:
    assessor = FakeAssessor()
    service = CheckpointAssessmentService(assessor)
    prompt, prompt_state = reduced(SupervisionEventType.USER_PROMPT_SUBMITTED)
    assert await service.assess_if_needed(prompt, prompt_state) is None

    trigger, state = reduced(SupervisionEventType.SESSION_IDLE)
    first, second = await asyncio.gather(
        service.assess_if_needed(trigger, state, task_context="private task"),
        service.assess_if_needed(trigger, state, task_context="private task"),
    )

    assert first == second
    assert len(assessor.calls) == 1
    assert assessor.calls[0][2] == "private task"


class Client:
    def __init__(self) -> None:
        self.calls = []

    async def system_one(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            nouls={name: SimpleNamespace(noul=0.5) for name in CHECKPOINT_QUESTIONS}
        )


@pytest.mark.asyncio
async def test_localjev_assessor_uses_authoritative_checkpoint_questions() -> None:
    client = Client()
    model = JevForemanModel(
        client=client,
        provider_id="localjev-qwen3-14b",
        checkpoint=AUTHORITATIVE_MODEL_CHECKPOINT,
        role="authoritative",
    )
    service = CheckpointAssessmentService(LocalJevCheckpointAssessor(model))
    trigger, state = reduced(SupervisionEventType.SESSION_IDLE)

    assessment = await service.assess_if_needed(
        trigger,
        state,
        task_context="in-memory task context",
    )

    assert assessment is not None
    assert assessment.provenance.role == "authoritative"
    assert assessment.provenance.question_version == CHECKPOINT_QUESTIONS_VERSION
    assert set(client.calls[0]["questions"]) == set(CHECKPOINT_QUESTIONS)
    assert client.calls[0]["state"]["task_context"] == "in-memory task context"


def test_localjev_assessor_rejects_shadow_authority() -> None:
    model = JevForemanModel(client=Client(), role="shadow")
    with pytest.raises(ValueError, match="authoritative"):
        LocalJevCheckpointAssessor(model)


def test_localjev_assessor_rejects_unpinned_or_remote_authority() -> None:
    with pytest.raises(ValueError, match="pinned qwen3"):
        LocalJevCheckpointAssessor(
            JevForemanModel(
                client=Client(),
                provider_id="localjev-qwen3-14b",
                checkpoint="qwen3:14b@sha256:wrong",
            )
        )
    with pytest.raises(ValueError, match="loopback"):
        LocalJevCheckpointAssessor(
            JevForemanModel(
                client=Client(),
                provider_id="localjev-qwen3-14b",
                checkpoint=AUTHORITATIVE_MODEL_CHECKPOINT,
                base_url="https://remote.example",
            )
        )
