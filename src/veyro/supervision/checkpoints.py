from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Protocol
from urllib.parse import urlparse

from pydantic import ValidationError

from veyro.models import (
    BoundaryDecision,
    BoundaryDisposition,
    CheckpointKind,
    SupervisionAssessment,
    SupervisionCheckpoint,
    SupervisionEvent,
    SupervisionEventType,
    SupervisionSessionState,
)
from veyro.veyro.base import VeyroModelError
from veyro.veyro.jev import JevVeyroModel

CHECKPOINT_QUESTIONS_VERSION = "supervision-checkpoint-v1"
AUTHORITATIVE_PROVIDER_ID = "localjev-qwen3-14b"
AUTHORITATIVE_MODEL_CHECKPOINT = (
    "qwen3:14b@sha256:bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8"
)
CHECKPOINT_QUESTIONS: dict[str, str] = {
    "meaningful_progress": "Does the reduced session evidence show meaningful task progress?",
    "work_stuck": "Does the reduced session evidence show that work is stuck or looping?",
    "work_off_track": "Does the reduced session evidence show work drifting off task?",
    "verification_sufficient": (
        "Does the reduced session evidence contain sufficient successful verification?"
    ),
    "completion_supported": (
        "Does the reduced evidence support the agent's current completion posture?"
    ),
    "safe_to_continue": (
        "Is it safe for this session to continue without immediate human intervention?"
    ),
    "needs_human": (
        "Does this checkpoint require human judgment, clarification, credentials, or approval?"
    ),
}


class CheckpointAssessor(Protocol):
    async def assess(
        self,
        checkpoint: SupervisionCheckpoint,
        state: SupervisionSessionState,
        *,
        task_context: str | None = None,
    ) -> SupervisionAssessment: ...

    async def close(self) -> None: ...


class CheckpointSelectionError(ValueError):
    """Raised when checkpoint inputs do not describe one reduced event."""


class CheckpointSelector:
    def select(
        self,
        event: SupervisionEvent,
        state: SupervisionSessionState,
        *,
        boundary_decision: BoundaryDecision | None = None,
    ) -> SupervisionCheckpoint | None:
        if event.session != state.session:
            raise CheckpointSelectionError("event and state session identities differ")
        if event.sequence != state.last_sequence:
            raise CheckpointSelectionError("state cursor does not match the trigger event")
        if boundary_decision is not None and boundary_decision.action.session != state.session:
            raise CheckpointSelectionError("boundary decision belongs to another session")

        kind: CheckpointKind | None = None
        retained_decision: BoundaryDecision | None = None
        if (
            boundary_decision is not None
            and boundary_decision.disposition is BoundaryDisposition.REVIEW_REQUIRED
        ):
            kind = CheckpointKind.RISKY_ACTION
            retained_decision = boundary_decision
        elif (
            event.event_type is SupervisionEventType.TEST_COMPLETED
            and state.checks
            and state.checks[-1].sequence == event.sequence
            and not state.checks[-1].passed
        ):
            kind = CheckpointKind.FAILED_VERIFICATION
        elif (
            event.event_type is SupervisionEventType.AGENT_MESSAGE_COMPLETED
            and state.latest_completion_claim_sequence == event.sequence
        ):
            kind = CheckpointKind.COMPLETION_CLAIM
        elif event.event_type is SupervisionEventType.SESSION_IDLE:
            kind = CheckpointKind.IDLE_SESSION

        if kind is None:
            return None
        checkpoint_id = hashlib.sha256(
            json.dumps(
                {
                    "state": state.model_dump(mode="json"),
                    "kind": kind.value,
                    "boundary": retained_decision.model_dump(mode="json")
                    if retained_decision
                    else None,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return SupervisionCheckpoint(
            checkpoint_id=checkpoint_id,
            session=state.session,
            sequence=event.sequence,
            kind=kind,
            trigger_event_type=event.event_type,
            boundary_decision=retained_decision,
        )


class LocalJevCheckpointAssessor:
    """Authoritative LocalJev adapter for reduced supervision checkpoints."""

    def __init__(self, model: JevVeyroModel) -> None:
        if model.role != "authoritative":
            raise ValueError("checkpoint assessor requires an authoritative Jev model")
        if model.provider_id != AUTHORITATIVE_PROVIDER_ID:
            raise ValueError("checkpoint assessor requires the pinned LocalJev provider identity")
        if model.checkpoint != AUTHORITATIVE_MODEL_CHECKPOINT:
            raise ValueError("checkpoint assessor requires the pinned qwen3:14b checkpoint")
        if urlparse(model.base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("checkpoint assessor requires a loopback LocalJev endpoint")
        self.model = model

    async def assess(
        self,
        checkpoint: SupervisionCheckpoint,
        state: SupervisionSessionState,
        *,
        task_context: str | None = None,
    ) -> SupervisionAssessment:
        if checkpoint.session != state.session or checkpoint.sequence != state.last_sequence:
            raise VeyroModelError("checkpoint does not match reduced session state")
        if task_context is not None and len(task_context) > 50_000:
            raise VeyroModelError("checkpoint task context exceeds 50000 characters")
        model_state = {
            "checkpoint": checkpoint.model_dump(mode="json"),
            "session_state": state.model_dump(mode="json"),
            "task_context": task_context or "",
        }
        values, provenance = await self.model.assess_values(
            state=model_state,
            questions=CHECKPOINT_QUESTIONS,
            question_version=CHECKPOINT_QUESTIONS_VERSION,
        )
        try:
            return SupervisionAssessment(
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_sequence=checkpoint.sequence,
                provenance=provenance,
                **values,
            )
        except ValidationError as error:
            raise VeyroModelError("LocalJev returned an invalid checkpoint assessment") from error

    async def close(self) -> None:
        await self.model.close()


class CheckpointAssessmentService:
    """Selects, serializes, and deduplicates meaningful semantic assessments."""

    def __init__(
        self,
        assessor: CheckpointAssessor,
        *,
        selector: CheckpointSelector | None = None,
    ) -> None:
        self.assessor = assessor
        self.selector = selector or CheckpointSelector()
        self._assessments: dict[tuple[str, str], SupervisionAssessment] = {}
        self._lock = asyncio.Lock()

    async def assess_if_needed(
        self,
        event: SupervisionEvent,
        state: SupervisionSessionState,
        *,
        boundary_decision: BoundaryDecision | None = None,
        task_context: str | None = None,
    ) -> SupervisionAssessment | None:
        checkpoint = self.selector.select(
            event,
            state,
            boundary_decision=boundary_decision,
        )
        if checkpoint is None:
            return None
        cache_key = (
            checkpoint.checkpoint_id,
            hashlib.sha256((task_context or "").encode()).hexdigest(),
        )
        async with self._lock:
            existing = self._assessments.get(cache_key)
            if existing is not None:
                return existing
            assessment = await self.assessor.assess(
                checkpoint,
                state,
                task_context=task_context,
            )
            if (
                assessment.checkpoint_id != checkpoint.checkpoint_id
                or assessment.checkpoint_sequence != checkpoint.sequence
                or assessment.provenance.role != "authoritative"
            ):
                raise VeyroModelError(
                    "checkpoint assessor returned mismatched or non-authoritative evidence"
                )
            self._assessments[cache_key] = assessment
            return assessment

    async def close(self) -> None:
        await self.assessor.close()
