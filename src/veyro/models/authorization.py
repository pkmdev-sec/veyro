from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from veyro.models.boundary import BoundaryDisposition
from veyro.models.rollout import RolloutMode
from veyro.models.supervision import (
    PROTOCOL_VERSION,
    ApprovalDecision,
    BridgeCapability,
    BridgeStability,
    CapabilityAvailability,
    ControlAction,
    ControlEffect,
    ControlEffectStatus,
    ControlOutcome,
    ControlResult,
)


class AuthorizationOutcome(StrEnum):
    AUTHORIZED = "authorized"
    DENIED = "denied"
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"


class AuthorizationReason(StrEnum):
    OBSERVE_ONLY = "observe_only"
    ADVISORY_ONLY = "advisory_only"
    UNSAFE_STATE = "unsafe_state"
    STALE_OBSERVATION = "stale_observation"
    DELIVERY_UNAVAILABLE = "delivery_unavailable"
    AUTHORIZED = "authorized"
    IDENTITY_MISMATCH = "identity_mismatch"
    CAPABILITY_UNSUPPORTED = "capability_unsupported"
    CAPABILITY_UNKNOWN = "capability_unknown"
    BOUNDARY_FORBIDDEN = "boundary_forbidden"
    EVIDENCE_MISMATCH = "evidence_mismatch"
    SEMANTIC_EVIDENCE_REQUIRED = "semantic_evidence_required"
    SEMANTIC_EVIDENCE_INVALID = "semantic_evidence_invalid"
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"
    HUMAN_APPROVAL_INVALID = "human_approval_invalid"
    HUMAN_DENIED = "human_denied"


class AuthorizationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HumanApprovalEvidence(AuthorizationModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    approval_id: str = Field(min_length=1, max_length=500)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: ApprovalDecision
    approved_by: str = Field(min_length=1, max_length=500)
    issued_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def expiry_follows_issue(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("human approval expiry must follow issue time")
        return self


class ControlAuthorization(AuthorizationModel):
    rollout_mode: RolloutMode
    rollout_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    veyro_session_id: str = Field(min_length=1, max_length=200)
    provider_id: str = Field(min_length=1, max_length=100)
    command_id: str = Field(min_length=1, max_length=200)
    action: ControlAction
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability: BridgeCapability
    capability_availability: CapabilityAvailability
    capability_stability: BridgeStability | None = None
    boundary_disposition: BoundaryDisposition
    boundary_rule_id: str = Field(pattern=r"^boundary\.[a-z_]+$")
    outcome: AuthorizationOutcome
    reason: AuthorizationReason
    detail: str = Field(min_length=1, max_length=2_000)
    checkpoint_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    human_approval_id: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def outcome_matches_reason(self) -> Self:
        authorized = self.outcome is AuthorizationOutcome.AUTHORIZED
        if authorized and self.rollout_mode in {RolloutMode.OBSERVE_ONLY, RolloutMode.ADVISORY}:
            raise ValueError("nonexecuting rollout modes cannot authorize delivery")
        if authorized != (self.reason is AuthorizationReason.AUTHORIZED):
            raise ValueError("only authorized decisions use the authorized reason")
        if self.outcome is AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED and self.reason not in {
            AuthorizationReason.HUMAN_APPROVAL_REQUIRED,
            AuthorizationReason.HUMAN_APPROVAL_INVALID,
        }:
            raise ValueError("human-approval outcomes require a human-approval reason")
        return self


class AuthorizedControlResult(AuthorizationModel):
    authorization: ControlAuthorization
    effect: ControlEffect
    result: ControlResult | None = None

    @model_validator(mode="after")
    def effect_matches_delivery_evidence(self) -> Self:
        authorized = self.authorization.outcome is AuthorizationOutcome.AUTHORIZED
        status = self.effect.status
        if not authorized:
            if self.result is not None or status is not ControlEffectStatus.NOT_APPLICABLE:
                raise ValueError("denied controls have no provider effect")
            return self
        if status is ControlEffectStatus.NOT_APPLICABLE:
            raise ValueError("authorized controls require an effect classification")
        if status in {
            ControlEffectStatus.VERIFIED,
            ControlEffectStatus.ACKNOWLEDGED_UNVERIFIED,
            ControlEffectStatus.FAILED,
        } and self.result is None:
            raise ValueError("known provider effects require a validated control result")
        if self.result is not None:
            executed = self.result.outcome is ControlOutcome.EXECUTED
            if executed and status not in {
                ControlEffectStatus.VERIFIED,
                ControlEffectStatus.ACKNOWLEDGED_UNVERIFIED,
                ControlEffectStatus.UNKNOWN,
            }:
                raise ValueError("executed control has an incompatible effect classification")
            if not executed and status in {
                ControlEffectStatus.VERIFIED,
                ControlEffectStatus.ACKNOWLEDGED_UNVERIFIED,
            }:
                raise ValueError("unsuccessful control cannot have a successful effect")
        return self
