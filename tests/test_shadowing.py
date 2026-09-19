from __future__ import annotations

import asyncio

import pytest

from foreman.foreman import ShadowingForemanModel
from foreman.foreman.base import ForemanModelError
from foreman.models import AssessmentProvenance, FactoryAssessment, InferenceMetadata
from foreman.observation import FactoryObservation


def assessment(provider_id: str, role: str, score: float) -> FactoryAssessment:
    return FactoryAssessment(
        implementation_complete=score,
        tests_sufficient=score,
        requirements_satisfied=score,
        needs_verification=score,
        meaningful_progress=score,
        worker_stuck=score,
        work_off_track=score,
        ready_to_finish=score,
        needs_human=score,
        provenance=AssessmentProvenance(
            provider_id=provider_id,
            role=role,
            implementation="test",
            endpoint=f"http://127.0.0.1/{provider_id}",
            request_model="jev-latest",
            checkpoint=f"{provider_id}@sha256:abc",
            question_version="foreman-assessment-v1",
            inference=InferenceMetadata(
                timeout_seconds=1,
                max_retries=0,
                latency_seconds=0.01,
            ),
        ),
    )


def observation() -> FactoryObservation:
    return FactoryObservation(
        original_job="job",
        run_id="run",
        factory_status="running",
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
        attempts=1,
        failures=[],
        elapsed_factory_seconds=0,
    )


class CoordinatedModel:
    def __init__(self, result: FactoryAssessment, barrier: asyncio.Barrier) -> None:
        self.result = result
        self.barrier = barrier
        self.observation_ids: list[int] = []

    async def assess(self, value: FactoryObservation) -> FactoryAssessment:
        self.observation_ids.append(id(value))
        await asyncio.wait_for(self.barrier.wait(), timeout=0.25)
        return self.result

    async def close(self) -> None:
        return None


class FailingModel:
    async def assess(self, value: FactoryObservation) -> FactoryAssessment:
        raise ForemanModelError("shadow unavailable")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_shadowing_runs_same_observation_concurrently_and_keeps_authority_separate() -> None:
    barrier = asyncio.Barrier(2)
    authoritative = CoordinatedModel(assessment("qwen", "authoritative", 0.2), barrier)
    shadow = CoordinatedModel(assessment("jeff", "shadow", 0.9), barrier)
    model = ShadowingForemanModel(authoritative, {"jeff": shadow})
    frozen_observation = observation()

    batch = await model.assess(frozen_observation)

    assert batch.authoritative.ready_to_finish == 0.2
    assert batch.shadows[0].ready_to_finish == 0.9
    assert authoritative.observation_ids == shadow.observation_ids == [id(frozen_observation)]
    assert batch.failures == []


@pytest.mark.asyncio
async def test_shadow_failure_is_reported_without_losing_authoritative_result() -> None:
    barrier = asyncio.Barrier(1)
    authoritative = CoordinatedModel(assessment("qwen", "authoritative", 0.2), barrier)
    model = ShadowingForemanModel(authoritative, {"jeff": FailingModel()})

    batch = await model.assess(observation())

    assert batch.authoritative.provenance.provider_id == "qwen"
    assert batch.shadows == []
    assert batch.failures[0].provider_id == "jeff"
    assert batch.failures[0].error_type == "ForemanModelError"
    assert batch.failures[0].message == "shadow unavailable"
