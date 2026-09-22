from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Protocol

from veyro.bridges import capability_for, validate_connection, validate_control_result
from veyro.models import (
    ApprovalDecision,
    AuthorizationOutcome,
    AuthorizationReason,
    AuthorizedControlResult,
    BoundaryDecision,
    BoundaryDisposition,
    BoundaryOperation,
    BridgeStability,
    CapabilityAvailability,
    CapabilitySet,
    ControlAction,
    ControlAuthorization,
    ControlEffect,
    ControlEffectStatus,
    ControlOutcome,
    ControlRequest,
    ControlResult,
    HumanApprovalEvidence,
    SessionIdentity,
    SupervisionAssessment,
    SupervisionCheckpoint,
    SupervisionSessionState,
)
from veyro.models.rollout import RolloutMode, RolloutPolicy
from veyro.models.supervision_state import SessionProgress
from veyro.supervision.boundary_policy import BoundaryPolicy
from veyro.supervision.delivery import DeliveryClaim, DeliveryError, DeliveryLedger


def control_request_sha256(request: ControlRequest) -> str:
    payload = json.dumps(
        request.model_dump(mode="json"), separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _evidence_sha256(value: ControlAuthorization | ControlResult) -> str:
    payload = json.dumps(
        value.model_dump(mode="json"), separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class ExecutableBridge(Protocol):
    @property
    def last_event_sequence(self) -> int: ...

    @property
    def identity(self) -> SessionIdentity: ...

    @property
    def capabilities(self) -> CapabilitySet: ...

    async def execute_if_current(
        self, request: ControlRequest, *, expected_sequence: int
    ) -> ControlResult | None: ...


class ControlAuthorizationGate:
    def __init__(self, policy: RolloutPolicy | None = None):
        self.policy = policy or RolloutPolicy()
        self.boundary_policy = BoundaryPolicy(review_operations=self.policy.review_operations)

    def authorize(
        self,
        request: ControlRequest,
        *,
        capabilities: CapabilitySet,
        state: SupervisionSessionState,
        boundary: BoundaryDecision,
        checkpoint: SupervisionCheckpoint | None = None,
        assessment: SupervisionAssessment | None = None,
        human_approval: HumanApprovalEvidence | None = None,
        bridge_identity: SessionIdentity | None = None,
        now: datetime | None = None,
    ) -> ControlAuthorization:
        digest = control_request_sha256(request)
        capability = capability_for(request.intent)
        declaration = next(
            (item for item in capabilities.declarations if item.capability is capability),
            None,
        )
        availability = (
            declaration.availability if declaration is not None else CapabilityAvailability.UNKNOWN
        )
        stability = declaration.stability if declaration is not None else None

        def result(
            outcome: AuthorizationOutcome,
            reason: AuthorizationReason,
            detail: str,
            *,
            checkpoint_id: str | None = None,
            approval_id: str | None = None,
        ) -> ControlAuthorization:
            return ControlAuthorization(
                rollout_mode=self.policy.mode,
                rollout_policy_sha256=self.policy.sha256,
                veyro_session_id=request.session.veyro_session_id,
                provider_id=request.session.provider_id,
                command_id=request.command_id,
                action=request.intent.action,
                request_sha256=digest,
                capability=capability,
                capability_availability=availability,
                capability_stability=stability,
                boundary_disposition=boundary.disposition,
                boundary_rule_id=boundary.rule_id,
                outcome=outcome,
                reason=reason,
                detail=detail,
                checkpoint_id=checkpoint_id,
                human_approval_id=approval_id,
            )

        if bridge_identity is not None and bridge_identity != request.session:
            return result(
                AuthorizationOutcome.DENIED,
                AuthorizationReason.IDENTITY_MISMATCH,
                "bridge identity does not match the control session",
            )
        try:
            validate_connection(request.session, capabilities)
        except ValueError:
            return result(
                AuthorizationOutcome.DENIED,
                AuthorizationReason.IDENTITY_MISMATCH,
                "capabilities do not describe the control session",
            )
        if state.session != request.session:
            return result(
                AuthorizationOutcome.DENIED,
                AuthorizationReason.IDENTITY_MISMATCH,
                "reduced state does not describe the control session",
            )
        if (
            boundary.action.session != request.session
            or boundary.action.action_id != request.command_id
            or boundary.action.target_sha256 != digest
            or boundary != self.boundary_policy.classify(boundary.action)
        ):
            return result(
                AuthorizationOutcome.DENIED,
                AuthorizationReason.EVIDENCE_MISMATCH,
                "boundary evidence is not bound to this command",
            )
        if availability is CapabilityAvailability.UNSUPPORTED:
            return result(
                AuthorizationOutcome.DENIED,
                AuthorizationReason.CAPABILITY_UNSUPPORTED,
                "provider bridge declares this control unsupported",
            )
        if availability is CapabilityAvailability.UNKNOWN:
            return result(
                AuthorizationOutcome.DENIED,
                AuthorizationReason.CAPABILITY_UNKNOWN,
                "provider bridge capability has not been proven",
            )
        if boundary.disposition is BoundaryDisposition.FORBIDDEN:
            return result(
                AuthorizationOutcome.DENIED,
                AuthorizationReason.BOUNDARY_FORBIDDEN,
                "deterministic boundary policy forbids this action",
            )

        if self.policy.mode is RolloutMode.OBSERVE_ONLY:
            return result(
                AuthorizationOutcome.DENIED,
                AuthorizationReason.OBSERVE_ONLY,
                "observe-only mode never assesses or delivers controls",
            )
        if (
            state.progress in {SessionProgress.COMPLETED, SessionProgress.FAILED}
            or state.normalization_failures
        ):
            return result(
                AuthorizationOutcome.DENIED,
                AuthorizationReason.UNSAFE_STATE,
                "terminal or failed observation state cannot authorize controls",
            )
        is_reply = request.intent.action is ControlAction.REPLY_TO_APPROVAL
        is_decline = is_reply and request.intent.decision is ApprovalDecision.DENY
        if (boundary.action.operation is BoundaryOperation.DECLINE_APPROVAL) != is_decline:
            return result(
                AuthorizationOutcome.DENIED,
                AuthorizationReason.EVIDENCE_MISMATCH,
                "decline classification must describe a native approval denial",
            )
        if is_reply and not any(
            p.approval_id == request.intent.approval_id for p in state.pending_approvals
        ):
            return result(
                AuthorizationOutcome.DENIED,
                AuthorizationReason.UNSAFE_STATE,
                "approval reply does not match an observed pending request",
            )
        if (
            request.intent.action in {ControlAction.STEER_ACTIVE_TURN, ControlAction.INTERRUPT_TURN}
            and state.active_turn_id is None
        ):
            return result(
                AuthorizationOutcome.DENIED,
                AuthorizationReason.UNSAFE_STATE,
                "active-turn control requires an observed active turn",
            )

        current_time = now or datetime.now(UTC)
        if human_approval is not None:
            if (
                human_approval.issued_at > current_time
                or human_approval.expires_at <= current_time
                or not hmac.compare_digest(human_approval.request_sha256, digest)
            ):
                return result(
                    AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED,
                    AuthorizationReason.HUMAN_APPROVAL_INVALID,
                    "human approval is not current or bound to this exact request",
                )
            if human_approval.decision is ApprovalDecision.DENY:
                return result(
                    AuthorizationOutcome.DENIED,
                    AuthorizationReason.HUMAN_DENIED,
                    "a human explicitly denied this control",
                    approval_id=human_approval.approval_id,
                )

        if self.policy.mode is RolloutMode.ADVISORY:
            return result(
                AuthorizationOutcome.DENIED,
                AuthorizationReason.ADVISORY_ONLY,
                "advisory mode reports a recommendation but never delivers controls",
            )
        if checkpoint is not None or assessment is not None:
            return result(
                AuthorizationOutcome.DENIED,
                AuthorizationReason.SEMANTIC_EVIDENCE_INVALID,
                "configured assessment labels do not prove authoritative semantic evidence",
            )
        if boundary.disposition is BoundaryDisposition.REVIEW_REQUIRED:
            return result(
                AuthorizationOutcome.DENIED,
                AuthorizationReason.SEMANTIC_EVIDENCE_REQUIRED,
                "review-required actions need qualified semantic evidence",
            )
        intent = request.intent
        automatically_allowed = (
            self.policy.mode is RolloutMode.AUTOMATIC
            and "deny_approval" in self.policy.automatic_actions
            and is_decline
            and state.unknown_events == 0
        )
        human_required = (
            not automatically_allowed
            or stability is not BridgeStability.STABLE
            or intent.action in {ControlAction.INTERRUPT_TURN, ControlAction.STOP_SESSION}
            or (
                intent.action is ControlAction.REPLY_TO_APPROVAL
                and intent.decision is ApprovalDecision.APPROVE
            )
        )
        if not human_required:
            return result(
                AuthorizationOutcome.AUTHORIZED,
                AuthorizationReason.AUTHORIZED,
                "stable supported low-risk control is authorized",
            )

        if human_approval is None:
            return result(
                AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED,
                AuthorizationReason.HUMAN_APPROVAL_REQUIRED,
                "this control requires explicit human approval",
            )
        return result(
            AuthorizationOutcome.AUTHORIZED,
            AuthorizationReason.AUTHORIZED,
            "control is authorized with current human approval",
            approval_id=human_approval.approval_id,
        )



class AuthorizedControlDispatcher:
    def __init__(
        self,
        bridge: ExecutableBridge,
        gate: ControlAuthorizationGate | None = None,
        *,
        ledger: DeliveryLedger | None = None,
    ):
        self.bridge = bridge
        self.gate = gate or ControlAuthorizationGate()
        self.ledger = ledger

    async def dispatch(self, request: ControlRequest, **evidence) -> AuthorizedControlResult:
        evidence = {**evidence, "now": datetime.now(UTC)}
        authorization = self.gate.authorize(
            request,
            capabilities=self.bridge.capabilities,
            bridge_identity=self.bridge.identity,
            **evidence,
        )
        if (
            authorization.outcome
            in {AuthorizationOutcome.AUTHORIZED, AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED}
            and self.bridge.last_event_sequence != evidence["state"].last_sequence
        ):
            authorization = authorization.model_copy(
                update={
                    "outcome": AuthorizationOutcome.DENIED,
                    "reason": AuthorizationReason.STALE_OBSERVATION,
                    "detail": "native observations advanced; reassess before another proposal",
                }
            )
        await self._record_authorization(request, authorization)
        if authorization.outcome is not AuthorizationOutcome.AUTHORIZED:
            return AuthorizedControlResult(
                authorization=authorization,
                effect=ControlEffect(status=ControlEffectStatus.NOT_APPLICABLE),
            )
        reason = None
        claim = None
        if self.bridge.last_event_sequence != evidence["state"].last_sequence:
            reason = AuthorizationReason.STALE_OBSERVATION
        elif self.ledger is None:
            reason = AuthorizationReason.DELIVERY_UNAVAILABLE
        else:
            try:
                claim = await asyncio.to_thread(
                    self.ledger.claim,
                    session=request.session,
                    command_id=request.command_id,
                    request_sha256=authorization.request_sha256,
                )
            except DeliveryError:
                reason = AuthorizationReason.DELIVERY_UNAVAILABLE
        if reason is not None:
            authorization = authorization.model_copy(
                update={
                    "outcome": AuthorizationOutcome.DENIED,
                    "reason": reason,
                    "detail": "stale observation or unavailable delivery claim; no control sent",
                }
            )
            await self._record_authorization(request, authorization)
            return AuthorizedControlResult(
                authorization=authorization,
                effect=ControlEffect(status=ControlEffectStatus.NOT_APPLICABLE),
            )
        if claim is None or self.ledger is None:
            raise DeliveryError("delivery claim is unavailable")
        claimed_authorization = authorization
        try:
            authorization = self.gate.authorize(
                request,
                capabilities=self.bridge.capabilities,
                bridge_identity=self.bridge.identity,
                **{**evidence, "now": datetime.now(UTC)},
            )
            if self.bridge.last_event_sequence != evidence["state"].last_sequence:
                authorization = authorization.model_copy(
                    update={
                        "outcome": AuthorizationOutcome.DENIED,
                        "reason": AuthorizationReason.STALE_OBSERVATION,
                        "detail": (
                            "observation changed during persistence; "
                            "claim retained; no delivery"
                        ),
                    }
                )
            await self._record_authorization(request, authorization)
            if (
                authorization.outcome is AuthorizationOutcome.AUTHORIZED
                and self.bridge.last_event_sequence != evidence["state"].last_sequence
            ):
                authorization = authorization.model_copy(
                    update={
                        "outcome": AuthorizationOutcome.DENIED,
                        "reason": AuthorizationReason.STALE_OBSERVATION,
                        "detail": (
                            "observation changed after authorization persistence; "
                            "claim retained; no delivery"
                        ),
                    }
                )
                await self._record_authorization(request, authorization)
        except BaseException:
            await self._commit_unknown_attempt(claim, claimed_authorization)
            raise
        if authorization.outcome is not AuthorizationOutcome.AUTHORIZED:
            await asyncio.to_thread(
                self.ledger.commit_attempt,
                claim,
                authorization_sha256=_evidence_sha256(authorization),
                result_sha256=None,
                effect=ControlEffectStatus.NOT_APPLICABLE,
            )
            return AuthorizedControlResult(
                authorization=authorization,
                effect=ControlEffect(status=ControlEffectStatus.NOT_APPLICABLE),
            )
        try:
            control_result = await self.bridge.execute_if_current(
                request, expected_sequence=evidence["state"].last_sequence
            )
            if control_result is not None:
                validate_control_result(request, control_result, self.bridge.capabilities)
        except BaseException:
            await self._commit_unknown_attempt(claim, authorization)
            raise
        if control_result is None:
            authorization = authorization.model_copy(
                update={
                    "outcome": AuthorizationOutcome.DENIED,
                    "reason": AuthorizationReason.STALE_OBSERVATION,
                    "detail": "observation changed at delivery boundary; no control sent",
                }
            )
            await self._record_authorization(request, authorization)
            await asyncio.to_thread(
                self.ledger.commit_attempt,
                claim,
                authorization_sha256=_evidence_sha256(authorization),
                result_sha256=None,
                effect=ControlEffectStatus.NOT_APPLICABLE,
            )
            return AuthorizedControlResult(
                authorization=authorization,
                effect=ControlEffect(status=ControlEffectStatus.NOT_APPLICABLE),
            )
        effect_status = (
            ControlEffectStatus.ACKNOWLEDGED_UNVERIFIED
            if control_result.outcome is ControlOutcome.EXECUTED
            else ControlEffectStatus.FAILED
        )
        await asyncio.to_thread(
            self.ledger.commit_attempt,
            claim,
            authorization_sha256=_evidence_sha256(authorization),
            result_sha256=_evidence_sha256(control_result),
            effect=effect_status,
        )
        return AuthorizedControlResult(
            authorization=authorization,
            result=control_result,
            effect=ControlEffect(status=effect_status),
        )

    async def _record_authorization(
        self, request: ControlRequest, authorization: ControlAuthorization
    ) -> None:
        if self.ledger is None:
            raise DeliveryError("delivery ledger is unavailable")
        await asyncio.to_thread(
            self.ledger.record_authorization,
            session=request.session,
            authorization_sha256=_evidence_sha256(authorization),
            outcome=authorization.outcome,
            reason=authorization.reason,
        )

    async def _commit_unknown_attempt(
        self, claim: DeliveryClaim, authorization: ControlAuthorization
    ) -> None:
        if self.ledger is None:
            raise DeliveryError("delivery ledger is unavailable")
        receipt = asyncio.create_task(
            asyncio.to_thread(
                self.ledger.commit_attempt,
                claim,
                authorization_sha256=_evidence_sha256(authorization),
                result_sha256=None,
                effect=ControlEffectStatus.UNKNOWN,
            )
        )
        try:
            await asyncio.shield(receipt)
        except asyncio.CancelledError:
            await receipt
