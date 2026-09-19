from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InferenceMetadata(BaseModel):
    """Non-secret settings and timing for one provider call."""

    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float | None = Field(default=None, gt=0.0)
    max_retries: int = Field(ge=0)
    latency_seconds: float = Field(ge=0.0)
    state_characters: int | None = Field(default=None, ge=0)
    max_state_characters: int | None = Field(default=None, gt=0)
    state_format: Literal["kv", "json", "values"] | None = None


class AssessmentProvenance(BaseModel):
    """Identity required to interpret and compare one semantic assessment."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    role: Literal["authoritative", "shadow"]
    implementation: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    request_model: str = Field(min_length=1)
    checkpoint: str = Field(min_length=1)
    question_version: str = Field(min_length=1)
    inference: InferenceMetadata


class FactoryAssessment(BaseModel):
    """Jev's normalized semantic view of the job and current factory floor."""

    model_config = ConfigDict(extra="forbid")

    implementation_complete: float = Field(ge=0.0, le=1.0)
    tests_sufficient: float = Field(ge=0.0, le=1.0)
    requirements_satisfied: float = Field(ge=0.0, le=1.0)
    needs_verification: float = Field(ge=0.0, le=1.0)
    meaningful_progress: float = Field(ge=0.0, le=1.0)
    worker_stuck: float = Field(ge=0.0, le=1.0)
    work_off_track: float = Field(ge=0.0, le=1.0)
    ready_to_finish: float = Field(ge=0.0, le=1.0)
    needs_human: float = Field(ge=0.0, le=1.0)
    assessed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provenance: AssessmentProvenance | None = None

    @field_validator(
        "implementation_complete",
        "tests_sufficient",
        "requirements_satisfied",
        "needs_verification",
        "meaningful_progress",
        "worker_stuck",
        "work_off_track",
        "ready_to_finish",
        "needs_human",
    )
    @classmethod
    def finite_scores(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("assessment scores must be finite")
        return value


class ProviderAssessmentFailure(BaseModel):
    """A non-authoritative provider failure that must not fail the factory run."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    role: Literal["shadow"] = "shadow"
    error_type: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=2_000)


class AssessmentBatch(BaseModel):
    """One policy input plus independently retained shadow observations."""

    model_config = ConfigDict(extra="forbid")

    authoritative: FactoryAssessment
    shadows: list[FactoryAssessment] = Field(default_factory=list)
    failures: list[ProviderAssessmentFailure] = Field(default_factory=list)

    @model_validator(mode="after")
    def roles_match_batch_position(self) -> AssessmentBatch:
        authoritative = self.authoritative.provenance
        if authoritative is not None and authoritative.role != "authoritative":
            raise ValueError("authoritative assessment must have the authoritative role")
        for shadow in self.shadows:
            if shadow.provenance is None or shadow.provenance.role != "shadow":
                raise ValueError("shadow assessments must have shadow provenance")
        return self
