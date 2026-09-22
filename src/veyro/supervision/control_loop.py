from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Self

from pydantic import BaseModel, ConfigDict, computed_field, model_validator

from veyro.bridges.base import AgentBridge
from veyro.models import (
    AuthorizationOutcome,
    AuthorizedControlResult,
    BoundaryAction,
    BoundaryDecision,
    ControlAction,
    ControlAuthorization,
    ControlEffect,
    ControlEffectStatus,
    ControlOutcome,
    ControlRequest,
    HumanApprovalEvidence,
    SupervisionAssessment,
    SupervisionCheckpoint,
    SupervisionEvent,
    SupervisionEventType,
    SupervisionSessionState,
)
from veyro.models.rollout import RolloutPolicy
from veyro.supervision.authorization import (
    AuthorizedControlDispatcher,
    ControlAuthorizationGate,
    control_request_sha256,
)
from veyro.supervision.boundary_policy import BoundaryPolicy
from veyro.supervision.checkpoints import CheckpointAssessmentService
from veyro.supervision.delivery import DeliveryLedger
from veyro.supervision.reducer import SessionReducer


class ControlLoopError(RuntimeError):
    pass


class ControlLoopEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger_event: SupervisionEvent
    reduced_state: SupervisionSessionState
    request: ControlRequest
    boundary: BoundaryDecision
    checkpoint: SupervisionCheckpoint | None = None
    assessment: SupervisionAssessment | None = None
    control: AuthorizedControlResult
    verification_event: SupervisionEvent | None = None

    @computed_field
    @property
    def effect(self) -> ControlEffect:
        return self.control.effect

    @model_validator(mode="after")
    def evidence_is_bound_to_one_decision(self) -> Self:
        session = self.request.session
        if self.trigger_event.session != session or self.reduced_state.session != session:
            raise ValueError("control-loop evidence spans multiple sessions")
        if self.reduced_state.last_sequence != self.trigger_event.sequence:
            raise ValueError("reduced state does not include exactly the trigger event")
        if (
            self.boundary.action.session != session
            or self.boundary.action.action_id != self.request.command_id
        ):
            raise ValueError("boundary evidence does not match the control request")
        if (self.checkpoint is None) != (self.assessment is None):
            raise ValueError("checkpoint and assessment evidence must appear together")
        if self.checkpoint is not None and (
            self.checkpoint.session != session
            or self.checkpoint.sequence != self.reduced_state.last_sequence
            or self.assessment is None
            or self.assessment.checkpoint_id != self.checkpoint.checkpoint_id
        ):
            raise ValueError("semantic evidence does not match the reduced state")
        authorization = self.control.authorization
        if (
            authorization.command_id != self.request.command_id
            or authorization.veyro_session_id != session.veyro_session_id
            or authorization.provider_id != session.provider_id
        ):
            raise ValueError("authorization does not match the control request")
        if self.verification_event is not None and (
            self.verification_event.session != session
            or self.verification_event.sequence <= self.trigger_event.sequence
        ):
            raise ValueError("provider verification is stale or belongs to another session")
        terminal_verification = (
            self.verification_event is not None
            and self.verification_event.event_type
            in {
                SupervisionEventType.SESSION_COMPLETED,
                SupervisionEventType.SESSION_FAILED,
            }
        )
        if self.control.effect.status is ControlEffectStatus.VERIFIED and not (
            self.request.intent.action is ControlAction.STOP_SESSION
            and self.control.result is not None
            and self.control.result.outcome is ControlOutcome.EXECUTED
            and terminal_verification
        ):
            raise ValueError("verified effects require an executed stop and terminal event")
        if (
            self.control.effect.status is ControlEffectStatus.VERIFIED
            and self.verification_event is not None
            and self.control.effect.native_event is not self.verification_event.event_type
        ):
            raise ValueError("effect evidence does not match the terminal provider event")
        if (
            self.verification_event is not None
            and self.control.effect.status is not ControlEffectStatus.VERIFIED
        ):
            raise ValueError("terminal verification must classify the effect as verified")
        if (
            self.request.intent.action is ControlAction.STOP_SESSION
            and self.control.result is not None
            and self.control.result.outcome is ControlOutcome.EXECUTED
            and self.control.effect.status is ControlEffectStatus.ACKNOWLEDGED_UNVERIFIED
        ):
            raise ValueError("control-loop stop evidence must finalize acknowledged delivery")
        return self


class SupervisionControlLoop:
    """Run one policy-gated control from normalized evidence to provider verification."""

    def __init__(
        self,
        bridge: AgentBridge,
        assessments: CheckpointAssessmentService | None,
        *,
        reducer: SessionReducer | None = None,
        boundary_policy: BoundaryPolicy | None = None,
        policy: RolloutPolicy | None = None,
        ledger: DeliveryLedger | None = None,
    ) -> None:
        self.bridge = bridge
        self.assessments = assessments
        self.reducer = reducer or SessionReducer(bridge.identity)
        gate = ControlAuthorizationGate(policy)
        self.boundary_policy = boundary_policy or gate.boundary_policy
        self.dispatcher = AuthorizedControlDispatcher(bridge, gate, ledger=ledger)
        self.ledger = ledger
        self._lock = asyncio.Lock()

    async def run_control(
        self,
        *,
        event: SupervisionEvent,
        request: ControlRequest,
        boundary_action: BoundaryAction,
        human_approval: HumanApprovalEvidence | None = None,
        approval_provider: Callable[[ControlAuthorization], Awaitable[HumanApprovalEvidence]]
        | None = None,
        task_context: str | None = None,
        verification_timeout: float = 5.0,
    ) -> ControlLoopEvidence:
        async with self._lock:
            if event.session != self.bridge.identity or request.session != self.bridge.identity:
                raise ControlLoopError("event, request, and bridge identities must match")
            if (
                boundary_action.session != request.session
                or boundary_action.action_id != request.command_id
                or boundary_action.target_sha256 != control_request_sha256(request)
            ):
                raise ControlLoopError("boundary action must be bound to the control request")

            state = self.reducer.apply(event)
            boundary = self.boundary_policy.classify(boundary_action)
            checkpoint = None
            assessment = None
            control = await self.dispatcher.dispatch(
                request,
                state=state,
                boundary=boundary,
                checkpoint=checkpoint,
                assessment=assessment,
                human_approval=human_approval,
            )
            if (
                control.authorization.outcome is AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED
                and human_approval is None
                and approval_provider is not None
            ):
                human_approval = await approval_provider(control.authorization)
                control = await self.dispatcher.dispatch(
                    request,
                    state=state,
                    boundary=boundary,
                    checkpoint=checkpoint,
                    assessment=assessment,
                    human_approval=human_approval,
                )
            verification_event = None
            if (
                control.authorization.outcome is AuthorizationOutcome.AUTHORIZED
                and control.result is not None
                and control.result.outcome is ControlOutcome.EXECUTED
                and request.intent.action is ControlAction.STOP_SESSION
            ):
                try:
                    verification_event = await self._wait_for_stopped_session(
                        after_sequence=event.sequence,
                        timeout_seconds=verification_timeout,
                    )
                except ControlLoopError:
                    effect = ControlEffect(status=ControlEffectStatus.UNKNOWN)
                    control = control.model_copy(update={"effect": effect})
                    await self._commit_final_effect(request, effect)
                except BaseException:
                    effect = ControlEffect(status=ControlEffectStatus.UNKNOWN)
                    receipt = asyncio.create_task(self._commit_final_effect(request, effect))
                    try:
                        await asyncio.shield(receipt)
                    except asyncio.CancelledError:
                        await receipt
                    raise
                else:
                    effect = ControlEffect(
                        status=ControlEffectStatus.VERIFIED,
                        native_event=verification_event.event_type,
                    )
                    control = control.model_copy(update={"effect": effect})
                    await self._commit_final_effect(request, effect)
            return ControlLoopEvidence(
                trigger_event=event,
                reduced_state=state,
                request=request,
                boundary=boundary,
                checkpoint=checkpoint,
                assessment=assessment,
                control=control,
                verification_event=verification_event,
            )

    async def _commit_final_effect(
        self, request: ControlRequest, effect: ControlEffect
    ) -> None:
        if self.ledger is None:
            raise ControlLoopError("executed control has no delivery ledger")
        await asyncio.to_thread(
            self.ledger.commit_final_effect,
            session=request.session,
            command_id=request.command_id,
            effect=effect.status,
            native_event=effect.native_event,
        )

    async def _wait_for_stopped_session(
        self,
        *,
        after_sequence: int,
        timeout_seconds: float,
    ) -> SupervisionEvent:
        async def wait() -> SupervisionEvent:
            async for event in self.bridge.events(after_sequence=after_sequence):
                if event.event_type in {
                    SupervisionEventType.SESSION_COMPLETED,
                    SupervisionEventType.SESSION_FAILED,
                }:
                    return event
            raise ControlLoopError("provider event stream closed before stop verification")

        try:
            return await asyncio.wait_for(wait(), timeout_seconds)
        except TimeoutError as error:
            raise ControlLoopError("provider did not verify the stopped session") from error
