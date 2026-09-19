from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from foreman.models import (
    AssessmentProvenance,
    EventType,
    FactoryAssessment,
    FactoryEvent,
    FactoryState,
    InferenceMetadata,
    Intervention,
    InterventionType,
    WorkerRecord,
    WorkerType,
)
from foreman.observation import FactoryObservation


def test_factory_state_validation(tmp_path) -> None:
    worker = WorkerRecord(worker_id="worker-1", worker_type=WorkerType.CODING, mission="work")
    state = FactoryState(
        run_id="abc",
        job="Do a thing",
        repository=str(tmp_path),
        workers=[worker],
        active_workers=["worker-1"],
    )
    assert state.active_workers == ["worker-1"]


def test_factory_state_rejects_unknown_worker(tmp_path) -> None:
    with pytest.raises(ValidationError, match="known workers"):
        FactoryState(
            run_id="abc",
            job="Do a thing",
            repository=str(tmp_path),
            active_workers=["missing"],
        )


@pytest.mark.parametrize("field", ["run_id", "job", "repository"])
def test_factory_state_rejects_empty_required_fields(tmp_path, field) -> None:
    values = {"run_id": "abc", "job": "job", "repository": str(tmp_path)}
    values[field] = ""
    with pytest.raises(ValidationError):
        FactoryState(**values)


def test_assessment_validation_and_round_trip(assessment) -> None:
    restored = FactoryAssessment.model_validate_json(assessment.model_dump_json())
    assert restored == assessment


@pytest.mark.parametrize("value", [-0.1, 1.1, float("inf"), float("nan")])
def test_assessment_rejects_invalid_score(assessment, value) -> None:
    values = assessment.model_dump(exclude={"assessed_at"})
    values["worker_stuck"] = value
    with pytest.raises(ValidationError):
        FactoryAssessment(**values)


def test_event_serialization() -> None:
    event = FactoryEvent(run_id="abc", event_type=EventType.WORKER_STARTED, payload={"n": 1})
    restored = FactoryEvent.model_validate_json(event.model_dump_json())
    assert restored.event_id == event.event_id
    assert restored.payload == {"n": 1}


def test_intervention_serialization() -> None:
    intervention = Intervention(
        action=InterventionType.STOP_WORKER,
        reason="stuck",
        assessment_iteration=2,
        worker_id="worker-1",
    )
    restored = Intervention.model_validate_json(intervention.model_dump_json())
    assert restored == intervention


def test_observation_validation() -> None:
    observation = FactoryObservation(
        original_job="job",
        run_id="abc",
        factory_status="RUNNING",
        iteration=1,
        active_workers=[],
        worker_history=[],
        latest_worker_output="",
        worker_exit_status={},
        worker_elapsed_seconds={},
        git_status="",
        git_diff="",
        changed_files=[],
        test_results=[],
        verification_results=[],
        recent_events=[],
        previous_assessment=None,
        previous_intervention=None,
        attempts=0,
        failures=[],
        elapsed_factory_seconds=0,
    )
    assert observation.model_dump(mode="json")["original_job"] == "job"


def test_observation_rejects_negative_elapsed() -> None:
    with pytest.raises(ValidationError):
        FactoryObservation(
            original_job="job",
            run_id="abc",
            factory_status="RUNNING",
            iteration=0,
            active_workers=[],
            worker_history=[],
            latest_worker_output="",
            worker_exit_status={},
            worker_elapsed_seconds={},
            git_status="",
            git_diff="",
            changed_files=[],
            test_results=[],
            verification_results=[],
            recent_events=[],
            previous_assessment=None,
            previous_intervention=None,
            attempts=0,
            failures=[],
            elapsed_factory_seconds=-1,
        )


def test_datetime_serializes_as_iso() -> None:
    event = FactoryEvent(
        run_id="abc",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        event_type=EventType.FACTORY_STARTED,
    )
    assert "2026-01-01T00:00:00Z" in event.model_dump_json()


def test_provider_assessments_remain_distinct_in_persisted_state(state, assessment) -> None:
    def provenance(provider_id: str, role: str) -> AssessmentProvenance:
        return AssessmentProvenance(
            provider_id=provider_id,
            role=role,
            implementation="typesafe-sdk",
            endpoint=f"http://127.0.0.1/{provider_id}",
            request_model="jev-latest",
            checkpoint=f"{provider_id}@sha256:abc",
            question_version="foreman-assessment-v1",
            inference=InferenceMetadata(
                timeout_seconds=10,
                max_retries=2,
                latency_seconds=0.25,
            ),
        )

    authoritative = assessment.model_copy(
        update={"provenance": provenance("localjev-qwen", "authoritative")}
    )
    shadow = assessment.model_copy(update={"provenance": provenance("jeff-gliformer", "shadow")})
    state.latest_assessment = authoritative
    state.assessment_history = [authoritative, shadow]

    restored = FactoryState.model_validate_json(state.model_dump_json())

    assert restored.latest_assessment.provenance.provider_id == "localjev-qwen"
    assert [item.provenance.provider_id for item in restored.assessment_history] == [
        "localjev-qwen",
        "jeff-gliformer",
    ]
