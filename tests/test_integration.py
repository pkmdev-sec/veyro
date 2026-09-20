from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from veyro.config import FactoryConfig
from veyro.models import EventType, FactoryAssessment, FactoryEvent, FactoryStatus, WorkerType
from veyro.observation import ObservationBuilder
from veyro.persistence import RunStore
from veyro.runtime import FactoryRuntime
from veyro.veyro import FakeVeyroModel
from veyro.workers import FakeWorker


def score(**updates: float) -> FactoryAssessment:
    base = {
        "implementation_complete": 0.3,
        "tests_sufficient": 0.1,
        "requirements_satisfied": 0.2,
        "needs_verification": 0.1,
        "meaningful_progress": 0.9,
        "worker_stuck": 0.0,
        "work_off_track": 0.0,
        "ready_to_finish": 0.0,
        "needs_human": 0.0,
    }
    base.update(updates)
    return FactoryAssessment(**base)


def config(**updates) -> FactoryConfig:
    values = {
        "assessment_min_interval_seconds": 0.01,
        "periodic_assessment_seconds": 0.05,
        # These tests exercise lifecycle ordering, not Git/disk latency.
        "worker_timeout_seconds": 30,
        "overall_timeout_seconds": 60,
        "max_workers": 3,
        "max_retries": 1,
        "max_iterations": 10,
        "steering_grace_seconds": 0,
    }
    values.update(updates)
    return FactoryConfig(**values)


@pytest.fixture(params=[0.0, 1.1])
def observation_latency(request, monkeypatch):
    build = ObservationBuilder.build

    async def delayed_build(self, state):
        if request.param:
            await asyncio.sleep(request.param)
        return await build(self, state)

    monkeypatch.setattr(ObservationBuilder, "build", delayed_build)


@pytest.mark.asyncio
async def test_full_simulated_factory_assesses_live_and_verifies(
    tmp_path, observation_latency
) -> None:
    first_assessed = asyncio.Event()
    second_assessed = asyncio.Event()

    async def synchronize_phases(event: FactoryEvent) -> None:
        if event.event_type is EventType.VEYRO_ASSESSED:
            if event.payload["iteration"] == 1:
                first_assessed.set()
            elif event.payload["iteration"] == 2:
                second_assessed.set()
        elif event.event_type is EventType.WORKER_OUTPUT:
            # Output callbacks run inside FakeWorker's worker timeout.
            if event.payload["line"] == "editing":
                await first_assessed.wait()
            elif event.payload["line"] == "testing":
                await second_assessed.wait()

    model = FakeVeyroModel(
        [
            score(implementation_complete=0.31, meaningful_progress=0.88),
            score(implementation_complete=0.72, meaningful_progress=0.94),
            score(
                implementation_complete=0.96,
                tests_sufficient=0.91,
                requirements_satisfied=0.92,
                needs_verification=0.93,
                ready_to_finish=0.68,
            ),
            score(
                implementation_complete=0.98,
                tests_sufficient=0.96,
                requirements_satisfied=0.97,
                needs_verification=0.04,
                ready_to_finish=0.98,
            ),
        ]
    )

    def workers(worker_type: WorkerType) -> FakeWorker:
        if worker_type is WorkerType.VERIFIER:
            return FakeWorker(output_lines=["verified"], delay_seconds=0.002)
        return FakeWorker(output_lines=["editing", "testing"], delay_seconds=0.2)

    runtime = FactoryRuntime(
        repository=tmp_path,
        job="Implement and test the feature",
        model=model,
        config=config(
            max_workers=2,
            assessment_min_interval_seconds=0.15,
            periodic_assessment_seconds=1.0,
        ),
        worker_factory=workers,
        event_sink=synchronize_phases,
    )
    state = await runtime.run()
    assert state.status is FactoryStatus.FINISHED
    assert Counter(worker.worker_type for worker in state.workers) == {
        WorkerType.CODING: 1,
        WorkerType.VERIFIER: 1,
    }
    assert any(observation.active_workers for observation in model.calls[:2])
    events = RunStore(tmp_path).load_events(state.run_id)
    types = {event.event_type for event in events}
    assert EventType.FACTORY_STARTED in types
    assert EventType.VERIFICATION_STARTED in types
    assert EventType.VERIFICATION_COMPLETED in types
    assert EventType.FACTORY_FINISHED in types


@pytest.mark.asyncio
async def test_stuck_worker_is_steered_stopped_retried_verified_and_finished(
    tmp_path, observation_latency
) -> None:
    model = FakeVeyroModel(
        [
            score(meaningful_progress=0.8),
            score(meaningful_progress=0.1, worker_stuck=0.95),
            score(meaningful_progress=0.1, worker_stuck=0.95),
            score(
                implementation_complete=0.95,
                tests_sufficient=0.85,
                requirements_satisfied=0.9,
                needs_verification=0.9,
                ready_to_finish=0.6,
            ),
            score(
                implementation_complete=0.95,
                tests_sufficient=0.85,
                requirements_satisfied=0.9,
                needs_verification=0.9,
                ready_to_finish=0.6,
            ),
            score(
                implementation_complete=0.99,
                tests_sufficient=0.96,
                requirements_satisfied=0.98,
                needs_verification=0.02,
                ready_to_finish=0.99,
            ),
        ]
    )
    coding_count = 0

    def workers(worker_type: WorkerType) -> FakeWorker:
        nonlocal coding_count
        if worker_type is WorkerType.VERIFIER:
            return FakeWorker(output_lines=["verified"], delay_seconds=0.001)
        coding_count += 1
        if coding_count == 1:
            return FakeWorker(output_lines=["same failure"], delay_seconds=0.02, wait_forever=True)
        return FakeWorker(output_lines=["fixed"], delay_seconds=0.001)

    runtime = FactoryRuntime(
        repository=tmp_path,
        job="Fix the bug",
        model=model,
        config=config(codex_backend="app-server"),
        worker_factory=workers,
    )
    state = await runtime.run()
    assert state.status is FactoryStatus.FINISHED
    assert state.retry_count == 1
    assert len(state.workers) == 3
    actions = [item.action.value for item in state.intervention_history]
    assert "STEER_WORKER" in actions
    assert "STOP_WORKER" in actions
    assert "RETRY_WORKER" in actions
    events = RunStore(tmp_path).load_events(state.run_id)
    assert any(event.event_type is EventType.WORKER_STEERED for event in events)
    assert any(event.event_type is EventType.WORKER_STOPPED for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("output_delay", [0, 0.01])
async def test_noisy_events_are_coalesced(tmp_path, output_delay) -> None:
    first_assessed = asyncio.Event()

    async def synchronize_phases(event: FactoryEvent) -> None:
        if event.event_type is EventType.VEYRO_ASSESSED:
            first_assessed.set()
        elif event.event_type is EventType.WORKER_OUTPUT:
            await first_assessed.wait()

    ready = score(
        implementation_complete=0.99,
        tests_sufficient=0.99,
        requirements_satisfied=0.99,
        needs_verification=0.0,
        ready_to_finish=0.99,
    )
    model = FakeVeyroModel([score(), ready])
    runtime = FactoryRuntime(
        repository=tmp_path,
        job="Small job",
        model=model,
        # Output stays inside the debounce window; completion must force the next assessment.
        config=config(assessment_min_interval_seconds=60, periodic_assessment_seconds=60),
        worker_factory=lambda _: FakeWorker(
            output_lines=[str(number) for number in range(50)], delay_seconds=output_delay
        ),
        event_sink=synchronize_phases,
    )
    state = await runtime.run()
    output_events = [
        event
        for event in RunStore(tmp_path).load_events(state.run_id)
        if event.event_type is EventType.WORKER_OUTPUT
    ]
    assert len(output_events) == 50
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_worker_timeout_reaches_terminal_state(tmp_path) -> None:
    model = FakeVeyroModel([score(needs_human=0.99)])
    runtime = FactoryRuntime(
        repository=tmp_path,
        job="Blocked job",
        model=model,
        config=config(worker_timeout_seconds=0.01),
        worker_factory=lambda _: FakeWorker(wait_forever=True, output_lines=[]),
    )
    state = await runtime.run()
    assert state.status is FactoryStatus.ESCALATED
