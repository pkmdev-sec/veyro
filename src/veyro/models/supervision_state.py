from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from veyro.models.supervision import (
    PROTOCOL_VERSION,
    ApprovalDecision,
    SessionIdentity,
    SupervisionEventType,
)

MAX_STATE_ITEMS = 200


class SessionProgress(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    IDLE = "idle"
    COMPLETED = "completed"
    FAILED = "failed"


class StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ActiveTool(StateModel):
    tool_call_id: str = Field(min_length=1, max_length=500)
    tool_name: str = Field(min_length=1, max_length=500)
    started_sequence: int = Field(ge=1)


class CheckObservation(StateModel):
    check_id: str = Field(min_length=1, max_length=500)
    passed: bool
    sequence: int = Field(ge=1)


class PendingApproval(StateModel):
    approval_id: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=500)
    requested_sequence: int = Field(ge=1)


class ResolvedApproval(StateModel):
    approval_id: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=500)
    decision: ApprovalDecision
    sequence: int = Field(ge=1)


class SupervisionSessionState(StateModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    session: SessionIdentity
    progress: SessionProgress = SessionProgress.CREATED
    last_sequence: int = Field(default=0, ge=0)
    last_event_type: SupervisionEventType | None = None
    active_turn_id: str | None = Field(default=None, max_length=500)
    prompts_submitted: int = Field(default=0, ge=0)
    turns_started: int = Field(default=0, ge=0)
    turns_completed: int = Field(default=0, ge=0)
    plan_updates: int = Field(default=0, ge=0)
    active_tools: tuple[ActiveTool, ...] = Field(default=(), max_length=MAX_STATE_ITEMS)
    completed_tools: int = Field(default=0, ge=0)
    failed_tools: int = Field(default=0, ge=0)
    orphan_tool_completions: int = Field(default=0, ge=0)
    changed_paths: tuple[str, ...] = Field(default=(), max_length=MAX_STATE_ITEMS)
    file_change_events: int = Field(default=0, ge=0)
    checks: tuple[CheckObservation, ...] = Field(default=(), max_length=MAX_STATE_ITEMS)
    pending_approvals: tuple[PendingApproval, ...] = Field(default=(), max_length=MAX_STATE_ITEMS)
    resolved_approvals: int = Field(default=0, ge=0)
    approval_decisions: tuple[ResolvedApproval, ...] = Field(default=(), max_length=MAX_STATE_ITEMS)
    orphan_approval_resolutions: int = Field(default=0, ge=0)
    completion_claims: int = Field(default=0, ge=0)
    latest_completion_claim_sequence: int | None = Field(default=None, ge=1)
    unknown_events: int = Field(default=0, ge=0)
    normalization_failures: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if (self.last_sequence == 0) != (self.last_event_type is None):
            raise ValueError("last sequence and event type must be present together")
        if self.turns_completed > self.turns_started:
            raise ValueError("completed turns cannot exceed started turns")
        if self.failed_tools > self.completed_tools:
            raise ValueError("failed tools cannot exceed completed tools")
        sequences = [
            *(item.started_sequence for item in self.active_tools),
            *(item.sequence for item in self.checks),
            *(item.requested_sequence for item in self.pending_approvals),
            *(item.sequence for item in self.approval_decisions),
        ]
        if self.latest_completion_claim_sequence is not None:
            sequences.append(self.latest_completion_claim_sequence)
        if any(sequence > self.last_sequence for sequence in sequences):
            raise ValueError("snapshot item sequence exceeds the replay cursor")
        return self
