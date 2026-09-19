from __future__ import annotations

from collections.abc import Iterable

from foreman.models import (
    ActiveTool,
    ApprovalDecision,
    CheckObservation,
    PendingApproval,
    ResolvedApproval,
    SessionIdentity,
    SessionProgress,
    SupervisionEvent,
    SupervisionEventType,
    SupervisionSessionState,
)
from foreman.models.supervision_state import MAX_STATE_ITEMS

_TERMINAL_PROGRESS = {SessionProgress.COMPLETED, SessionProgress.FAILED}


class SessionReductionError(ValueError):
    """Raised when normalized events cannot form one coherent session state."""


def _required_string(event: SupervisionEvent, name: str) -> str:
    value = event.payload.get(name)
    if not isinstance(value, str) or not value or len(value) > 10_000:
        raise SessionReductionError(f"{event.event_type.value} requires a valid {name}")
    return value


def _required_bool(event: SupervisionEvent, name: str) -> bool:
    value = event.payload.get(name)
    if not isinstance(value, bool):
        raise SessionReductionError(f"{event.event_type.value} requires a boolean {name}")
    return value


def _required_approval_decision(event: SupervisionEvent) -> ApprovalDecision:
    value = _required_string(event, "decision")
    try:
        return ApprovalDecision(value)
    except ValueError as error:
        raise SessionReductionError(
            "approval_resolved requires decision approve or deny"
        ) from error


def _bounded_append(items: tuple, item):
    return (*items, item)[-MAX_STATE_ITEMS:]


def _bounded_unique(items: tuple[str, ...], item: str) -> tuple[str, ...]:
    return (*tuple(value for value in items if value != item), item)[-MAX_STATE_ITEMS:]


class SessionReducer:
    def __init__(
        self,
        session: SessionIdentity,
        *,
        initial: SupervisionSessionState | None = None,
    ) -> None:
        if initial is not None and initial.session != session:
            raise SessionReductionError("initial state belongs to another session identity")
        self.session = session
        self._state = initial or SupervisionSessionState(session=session)

    def snapshot(self) -> SupervisionSessionState:
        return self._state

    def apply(self, event: SupervisionEvent) -> SupervisionSessionState:
        state = self._state
        if event.session != self.session:
            raise SessionReductionError("event session identity does not match the reducer")
        expected = state.last_sequence + 1
        if event.sequence != expected:
            raise SessionReductionError(f"expected sequence {expected}, received {event.sequence}")
        if state.progress in _TERMINAL_PROGRESS:
            raise SessionReductionError("events cannot follow a terminal session state")

        updates: dict[str, object] = {
            "last_sequence": event.sequence,
            "last_event_type": event.event_type,
        }
        event_type = event.event_type

        if event_type is SupervisionEventType.SESSION_STARTED:
            if state.progress is not SessionProgress.CREATED:
                raise SessionReductionError("session_started must be the first lifecycle event")
            updates["progress"] = SessionProgress.RUNNING
        elif event_type is SupervisionEventType.USER_PROMPT_SUBMITTED:
            updates["prompts_submitted"] = state.prompts_submitted + 1
            updates["progress"] = SessionProgress.RUNNING
        elif event_type is SupervisionEventType.TURN_STARTED:
            turn_id = _required_string(event, "turn_id")
            if state.active_turn_id is not None:
                raise SessionReductionError("a turn is already active")
            updates.update(
                active_turn_id=turn_id,
                turns_started=state.turns_started + 1,
                progress=SessionProgress.RUNNING,
            )
        elif event_type is SupervisionEventType.PLAN_UPDATED:
            updates["plan_updates"] = state.plan_updates + 1
        elif event_type is SupervisionEventType.TOOL_STARTED:
            tool_call_id = _required_string(event, "tool_call_id")
            tool_name = _required_string(event, "tool_name")
            if any(item.tool_call_id == tool_call_id for item in state.active_tools):
                raise SessionReductionError("tool_call_id is already active")
            if len(state.active_tools) >= MAX_STATE_ITEMS:
                raise SessionReductionError("too many active tools for one snapshot")
            updates.update(
                active_tools=(
                    *state.active_tools,
                    ActiveTool(
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        started_sequence=event.sequence,
                    ),
                ),
                progress=SessionProgress.RUNNING,
            )
        elif event_type is SupervisionEventType.TOOL_COMPLETED:
            tool_call_id = _required_string(event, "tool_call_id")
            success = _required_bool(event, "success")
            active_tools = tuple(
                item for item in state.active_tools if item.tool_call_id != tool_call_id
            )
            found = len(active_tools) != len(state.active_tools)
            updates.update(
                active_tools=active_tools,
                completed_tools=state.completed_tools + 1,
                failed_tools=state.failed_tools + (0 if success else 1),
                orphan_tool_completions=(state.orphan_tool_completions + (0 if found else 1)),
            )
        elif event_type is SupervisionEventType.FILE_CHANGED:
            path = event.payload.get("path") or event.payload.get("path_sha256")
            if not isinstance(path, str) or not path or len(path) > 10_000:
                raise SessionReductionError("file_changed requires a valid path or path_sha256")
            updates.update(
                changed_paths=_bounded_unique(state.changed_paths, path),
                file_change_events=state.file_change_events + 1,
            )
        elif event_type is SupervisionEventType.TEST_COMPLETED:
            check = CheckObservation(
                check_id=_required_string(event, "check_id"),
                passed=_required_bool(event, "passed"),
                sequence=event.sequence,
            )
            updates["checks"] = _bounded_append(state.checks, check)
        elif event_type is SupervisionEventType.APPROVAL_REQUESTED:
            approval_id = _required_string(event, "approval_id")
            category = _required_string(event, "category")
            if any(item.approval_id == approval_id for item in state.pending_approvals):
                raise SessionReductionError("approval_id is already pending")
            if len(state.pending_approvals) >= MAX_STATE_ITEMS:
                raise SessionReductionError("too many pending approvals for one snapshot")
            updates.update(
                pending_approvals=(
                    *state.pending_approvals,
                    PendingApproval(
                        approval_id=approval_id,
                        category=category,
                        requested_sequence=event.sequence,
                    ),
                ),
                progress=SessionProgress.WAITING_APPROVAL,
            )
        elif event_type is SupervisionEventType.APPROVAL_RESOLVED:
            approval_id = _required_string(event, "approval_id")
            decision = _required_approval_decision(event)
            matching = next(
                (item for item in state.pending_approvals if item.approval_id == approval_id),
                None,
            )
            pending = tuple(
                item for item in state.pending_approvals if item.approval_id != approval_id
            )
            found = len(pending) != len(state.pending_approvals)
            updates.update(
                pending_approvals=pending,
                resolved_approvals=state.resolved_approvals + 1,
                approval_decisions=_bounded_append(
                    state.approval_decisions,
                    ResolvedApproval(
                        approval_id=approval_id,
                        category=matching.category if matching else "unknown",
                        decision=decision,
                        sequence=event.sequence,
                    ),
                ),
                orphan_approval_resolutions=(
                    state.orphan_approval_resolutions + (0 if found else 1)
                ),
                progress=(
                    SessionProgress.RUNNING
                    if state.active_turn_id is not None
                    else SessionProgress.IDLE
                ),
            )
        elif event_type is SupervisionEventType.AGENT_MESSAGE_COMPLETED:
            if event.payload.get("completion_claim") is True:
                updates.update(
                    completion_claims=state.completion_claims + 1,
                    latest_completion_claim_sequence=event.sequence,
                )
        elif event_type is SupervisionEventType.TURN_COMPLETED:
            turn_id = _required_string(event, "turn_id")
            if state.active_turn_id != turn_id:
                raise SessionReductionError("turn_completed does not match the active turn")
            updates.update(
                active_turn_id=None,
                turns_completed=state.turns_completed + 1,
                progress=(
                    SessionProgress.WAITING_APPROVAL
                    if state.pending_approvals
                    else SessionProgress.IDLE
                ),
            )
        elif event_type is SupervisionEventType.SESSION_IDLE:
            updates["progress"] = (
                SessionProgress.WAITING_APPROVAL
                if state.pending_approvals
                else SessionProgress.IDLE
            )
        elif event_type is SupervisionEventType.SESSION_COMPLETED:
            updates["progress"] = SessionProgress.COMPLETED
        elif event_type is SupervisionEventType.SESSION_FAILED:
            updates["progress"] = SessionProgress.FAILED
        elif event_type is SupervisionEventType.NORMALIZATION_FAILED:
            updates["normalization_failures"] = state.normalization_failures + 1
        elif event_type is SupervisionEventType.UNKNOWN:
            updates["unknown_events"] = state.unknown_events + 1

        self._state = state.model_copy(update=updates)
        return self._state


def replay_session(
    session: SessionIdentity,
    events: Iterable[SupervisionEvent],
    *,
    initial: SupervisionSessionState | None = None,
) -> SupervisionSessionState:
    reducer = SessionReducer(session, initial=initial)
    for event in events:
        reducer.apply(event)
    return reducer.snapshot()
