from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from veyro.models.assessment import AssessmentProvenance
from veyro.models.boundary import BoundaryDecision
from veyro.models.supervision import PROTOCOL_VERSION, SessionIdentity, SupervisionEventType


class CheckpointKind(StrEnum):
    FAILED_VERIFICATION = "failed_verification"
    RISKY_ACTION = "risky_action"
    IDLE_SESSION = "idle_session"
    COMPLETION_CLAIM = "completion_claim"


class CheckpointModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SupervisionCheckpoint(CheckpointModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    checkpoint_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    session: SessionIdentity
    sequence: int = Field(ge=1)
    kind: CheckpointKind
    trigger_event_type: SupervisionEventType
    boundary_decision: BoundaryDecision | None = None

    @model_validator(mode="after")
    def boundary_matches_kind(self) -> Self:
        is_risky = self.kind is CheckpointKind.RISKY_ACTION
        if is_risky != (self.boundary_decision is not None):
            raise ValueError("only risky-action checkpoints carry a boundary decision")
        return self


class SupervisionAssessment(CheckpointModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    checkpoint_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_sequence: int = Field(ge=1)
    meaningful_progress: float = Field(ge=0.0, le=1.0)
    work_stuck: float = Field(ge=0.0, le=1.0)
    work_off_track: float = Field(ge=0.0, le=1.0)
    verification_sufficient: float = Field(ge=0.0, le=1.0)
    completion_supported: float = Field(ge=0.0, le=1.0)
    safe_to_continue: float = Field(ge=0.0, le=1.0)
    needs_human: float = Field(ge=0.0, le=1.0)
    provenance: AssessmentProvenance

    @model_validator(mode="after")
    def authoritative_only(self) -> Self:
        if self.provenance.role != "authoritative":
            raise ValueError("supervision assessment must be authoritative")
        return self
