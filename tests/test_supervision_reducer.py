from __future__ import annotations

import json

import pytest

from foreman.models import (
    BridgeSource,
    EventProvenance,
    SessionIdentity,
    SessionProgress,
    SupervisionEvent,
    SupervisionEventType,
)
from foreman.supervision import SessionReducer, SessionReductionError, replay_session


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


def lifecycle(session: SessionIdentity) -> list[SupervisionEvent]:
    return [
        event(session, 1, SupervisionEventType.SESSION_STARTED),
        event(session, 2, SupervisionEventType.USER_PROMPT_SUBMITTED),
        event(session, 3, SupervisionEventType.TURN_STARTED, {"turn_id": "turn-1"}),
        event(
            session,
            4,
            SupervisionEventType.TOOL_STARTED,
            {"tool_call_id": "tool-1", "tool_name": "shell"},
        ),
        event(
            session,
            5,
            SupervisionEventType.FILE_CHANGED,
            {"path": "src/auth.py"},
        ),
        event(
            session,
            6,
            SupervisionEventType.TEST_COMPLETED,
            {"check_id": "pytest", "passed": False},
        ),
        event(
            session,
            7,
            SupervisionEventType.APPROVAL_REQUESTED,
            {"approval_id": "approval-1", "category": "network"},
        ),
        event(
            session,
            8,
            SupervisionEventType.AGENT_MESSAGE_COMPLETED,
            {"completion_claim": True, "message": "private transcript content"},
        ),
        event(
            session,
            9,
            SupervisionEventType.TOOL_COMPLETED,
            {"tool_call_id": "tool-1", "success": True},
        ),
        event(
            session,
            10,
            SupervisionEventType.APPROVAL_RESOLVED,
            {"approval_id": "approval-1", "decision": "deny"},
        ),
        event(session, 11, SupervisionEventType.TURN_COMPLETED, {"turn_id": "turn-1"}),
        event(session, 12, SupervisionEventType.SESSION_IDLE),
    ]


def test_reducer_builds_a_bounded_provider_neutral_snapshot() -> None:
    session = identity()

    state = replay_session(session, lifecycle(session))

    assert state.progress is SessionProgress.IDLE
    assert state.last_sequence == 12
    assert state.prompts_submitted == 1
    assert state.turns_started == 1
    assert state.turns_completed == 1
    assert state.active_tools == ()
    assert state.completed_tools == 1
    assert state.failed_tools == 0
    assert state.changed_paths == ("src/auth.py",)
    assert [(check.check_id, check.passed) for check in state.checks] == [("pytest", False)]
    assert state.pending_approvals == ()
    assert state.resolved_approvals == 1
    assert state.approval_decisions[0].decision == "deny"
    assert state.completion_claims == 1
    assert state.latest_completion_claim_sequence == 8
    assert "private transcript content" not in state.model_dump_json()


def test_reducer_can_resume_from_a_persisted_snapshot() -> None:
    session = identity()
    first = replay_session(session, lifecycle(session)[:6])
    restored = first.__class__.model_validate_json(first.model_dump_json())
    reducer = SessionReducer(session, initial=restored)

    for item in lifecycle(session)[6:]:
        reducer.apply(item)
    resumed = reducer.snapshot()

    assert resumed == replay_session(session, lifecycle(session))


def test_reducer_rejects_gaps_and_foreign_sessions() -> None:
    session = identity()
    reducer = SessionReducer(session)

    with pytest.raises(SessionReductionError, match="expected sequence 1"):
        reducer.apply(event(session, 2, SupervisionEventType.SESSION_STARTED))

    foreign = identity().model_copy(update={"foreman_session_id": "other"})
    with pytest.raises(SessionReductionError, match="session identity"):
        reducer.apply(event(foreign, 1, SupervisionEventType.SESSION_STARTED))


def test_reducer_rejects_malformed_normalized_payloads() -> None:
    session = identity()
    reducer = SessionReducer(session)
    reducer.apply(event(session, 1, SupervisionEventType.SESSION_STARTED))

    with pytest.raises(SessionReductionError, match="tool_name"):
        reducer.apply(
            event(
                session,
                2,
                SupervisionEventType.TOOL_STARTED,
                {"tool_call_id": "tool-1"},
            )
        )


def test_reducer_rejects_events_after_terminal_state() -> None:
    session = identity()
    reducer = SessionReducer(session)
    reducer.apply(event(session, 1, SupervisionEventType.SESSION_STARTED))
    reducer.apply(event(session, 2, SupervisionEventType.SESSION_COMPLETED))

    with pytest.raises(SessionReductionError, match="terminal"):
        reducer.apply(event(session, 3, SupervisionEventType.TURN_STARTED))


def test_normalization_failures_are_visible_without_provider_payload() -> None:
    session = identity()
    state = replay_session(
        session,
        [
            event(session, 1, SupervisionEventType.SESSION_STARTED),
            event(
                session,
                2,
                SupervisionEventType.NORMALIZATION_FAILED,
                {"native_event_type": "new_event", "raw_event_sha256": "a" * 64},
            ),
        ],
    )

    assert state.normalization_failures == 1
    assert "new_event" not in json.dumps(state.model_dump(mode="json"))


def test_reducer_bounds_replayable_detail_without_losing_totals() -> None:
    session = identity()
    reducer = SessionReducer(session)
    reducer.apply(event(session, 1, SupervisionEventType.SESSION_STARTED))
    for offset in range(205):
        reducer.apply(
            event(
                session,
                offset + 2,
                SupervisionEventType.FILE_CHANGED,
                {"path": f"src/file-{offset}.py"},
            )
        )

    state = reducer.snapshot()
    assert len(state.changed_paths) == 200
    assert state.changed_paths[0] == "src/file-5.py"
    assert state.file_change_events == 205
    assert state.last_sequence == 206
