from __future__ import annotations

import asyncio

import pytest

from veyro.config import FactoryConfig
from veyro.models import (
    AssessmentBatch,
    AssessmentProvenance,
    EventType,
    FactoryAssessment,
    FactoryStatus,
    InferenceMetadata,
    InterventionType,
    ProviderAssessmentFailure,
)
from veyro.persistence import RunStore
from veyro.runtime import FactoryRuntime
from veyro.veyro import FakeVeyroModel
from veyro.workers import FakeWorker


def human_assessment() -> FactoryAssessment:
    return FactoryAssessment(
        implementation_complete=0,
        tests_sufficient=0,
        requirements_satisfied=0,
        needs_verification=0,
        meaningful_progress=0,
        worker_stuck=0,
        work_off_track=0,
        ready_to_finish=0,
        needs_human=1,
    )


@pytest.mark.asyncio
async def test_worker_events_reach_veyro_and_intervention_reaches_worker(tmp_path) -> None:
    sink = []
    runtime = FactoryRuntime(
        repository=tmp_path,
        job="Needs credentials",
        model=FakeVeyroModel([human_assessment()]),
        config=FactoryConfig(
            assessment_min_interval_seconds=0,
            periodic_assessment_seconds=0.1,
            worker_timeout_seconds=1,
            overall_timeout_seconds=2,
        ),
        worker_factory=lambda _: FakeWorker(wait_forever=True, output_lines=[]),
        event_sink=sink.append,
    )
    state = await runtime.run()
    assert state.status is FactoryStatus.ESCALATED
    types = [event.event_type for event in sink]
    assert EventType.WORKER_STARTED in types
    assert EventType.VEYRO_ASSESSED in types
    assert EventType.VEYRO_INTERVENED in types
    assessed_event = next(event for event in sink if event.event_type is EventType.VEYRO_ASSESSED)
    assert assessed_event.payload["assessment"]["provenance"]["provider_id"] == "simulation"
    persisted = RunStore(tmp_path).load_state(state.run_id)
    assert persisted.latest_assessment.provenance.provider_id == "simulation"
    assert runtime.state.workers[0].termination_reason == "factory escalated"


def test_runtime_rejects_missing_repository(tmp_path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        FactoryRuntime(
            repository=tmp_path / "missing",
            job="job",
            model=FakeVeyroModel([human_assessment()]),
        )


def test_runtime_rejects_empty_job(tmp_path) -> None:
    with pytest.raises(ValueError, match="empty"):
        FactoryRuntime(repository=tmp_path, job=" ", model=FakeVeyroModel())


@pytest.mark.asyncio
async def test_overall_timeout_terminates_worker_and_persists_failure(tmp_path) -> None:
    continuing = human_assessment().model_copy(update={"needs_human": 0, "meaningful_progress": 1})
    runtime = FactoryRuntime(
        repository=tmp_path,
        job="Long job",
        model=FakeVeyroModel([continuing]),
        config=FactoryConfig(
            assessment_min_interval_seconds=0,
            periodic_assessment_seconds=0.01,
            worker_timeout_seconds=10,
            overall_timeout_seconds=0.05,
            max_iterations=1_000,
        ),
        worker_factory=lambda _: FakeWorker(wait_forever=True, output_lines=[]),
    )
    state = await runtime.run()
    assert state.status is FactoryStatus.FAILED
    assert "overall job timeout" in state.errors
    assert not state.active_workers


@pytest.mark.asyncio
async def test_runtime_cancellation_stops_active_worker(tmp_path) -> None:
    continuing = human_assessment().model_copy(update={"needs_human": 0, "meaningful_progress": 1})
    started = asyncio.Event()

    def observe(event) -> None:
        if event.event_type is EventType.WORKER_STARTED:
            started.set()

    runtime = FactoryRuntime(
        repository=tmp_path,
        job="Cancelled job",
        model=FakeVeyroModel([continuing]),
        config=FactoryConfig(
            assessment_min_interval_seconds=0,
            periodic_assessment_seconds=1,
            worker_timeout_seconds=10,
            overall_timeout_seconds=10,
            max_iterations=1_000,
        ),
        worker_factory=lambda _: FakeWorker(wait_forever=True, output_lines=[]),
        event_sink=observe,
    )
    task = asyncio.create_task(runtime.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.state.status is FactoryStatus.CANCELLED
    assert not runtime.state.active_workers


@pytest.mark.asyncio
async def test_shadow_scores_cannot_drive_policy_and_partial_failures_are_reported(
    tmp_path,
) -> None:
    authoritative = human_assessment()
    shadow = human_assessment().model_copy(
        update={
            "needs_human": 0,
            "ready_to_finish": 1,
            "provenance": AssessmentProvenance(
                provider_id="jeff",
                role="shadow",
                implementation="test",
                endpoint="http://127.0.0.1:8081",
                request_model="jev-latest",
                checkpoint="gliformer@sha256:abc",
                question_version="veyro-assessment-v1",
                inference=InferenceMetadata(
                    timeout_seconds=1,
                    max_retries=0,
                    latency_seconds=0.1,
                ),
            ),
        }
    )
    batch = AssessmentBatch(
        authoritative=authoritative,
        shadows=[shadow],
        failures=[
            ProviderAssessmentFailure(
                provider_id="other-shadow",
                error_type="TimeoutError",
                message="timed out",
            )
        ],
    )

    class BatchModel:
        async def assess(self, observation):
            return batch

        async def close(self):
            return None

    sink = []
    runtime = FactoryRuntime(
        repository=tmp_path,
        job="Authority must stay with Qwen",
        model=BatchModel(),
        event_sink=sink.append,
    )

    runtime.store.initialize(runtime.state)
    intervention = await runtime._assess()

    assert intervention.action is InterventionType.ESCALATE
    assert runtime.state.latest_assessment is authoritative
    assert runtime.state.assessment_history == [authoritative, shadow]
    assert [event.event_type for event in sink].count(EventType.VEYRO_ASSESSED) == 2
    failure = next(event for event in sink if event.event_type is EventType.VEYRO_ASSESSMENT_FAILED)
    assert failure.payload["provider_id"] == "other-shadow"
