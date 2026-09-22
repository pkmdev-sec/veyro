from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_supervision_control_loop import FakeAssessor
from typer.testing import CliRunner

from veyro.cli import app
from veyro.models import (
    AuthorizationOutcome,
    AuthorizationReason,
    BoundaryAction,
    BoundaryOperation,
    BridgeCapability,
    CapabilityDeclaration,
    CapabilitySet,
    ControlEffectStatus,
    ControlRequest,
    ControlResult,
    EventProvenance,
    HumanApprovalEvidence,
    QueueFollowUp,
    ReplyToApproval,
    SessionIdentity,
    SupervisionEvent,
)
from veyro.models.rollout import RolloutMode, RolloutPolicy
from veyro.supervision.authorization import (
    AuthorizedControlDispatcher,
    ControlAuthorizationGate,
    control_request_sha256,
)
from veyro.supervision.checkpoints import CheckpointAssessmentService
from veyro.supervision.control_loop import SupervisionControlLoop
from veyro.supervision.delivery import DeliveryLedger
from veyro.supervision.reducer import SessionReducer
from veyro.supervision.supervisor import ControlProposal


class ObservedBridge:
    def __init__(self, repository: Path, *, stability="stable", availability="supported"):
        self.identity = SessionIdentity(
            veyro_session_id="rollout-test",
            provider_id="test-provider",
            provider_session_id="native-session",
            repository=str(repository.resolve()),
            provider_version="1",
            bridge_id="test-bridge",
            bridge_version="1",
        )
        self.capabilities = CapabilitySet(
            provider_id=self.identity.provider_id,
            provider_version="1",
            bridge_id="test-bridge",
            bridge_version="1",
            declarations=tuple(
                CapabilityDeclaration(
                    capability=cap,
                    availability=availability,
                    stability=stability,
                    evidence="test-only stable adapter",
                )
                for cap in BridgeCapability
            ),
        )
        self.items = [
            self.event(1, "session_started"),
            self.event(2, "approval_requested", {"approval_id": "pending-1", "category": "tool"}),
        ]
        self.calls = []
        self.closed = False

    def event(self, sequence, kind, payload=None):
        return SupervisionEvent(
            session=self.identity,
            sequence=sequence,
            event_type=kind,
            payload=payload or {},
            provenance=EventProvenance(source="server", native_event_type=kind),
        )

    @property
    def last_event_sequence(self):
        return len(self.items)

    async def events(self, *, after_sequence=0):
        for item in self.items:
            if item.sequence > after_sequence:
                yield item

    async def execute(self, request):
        self.calls.append(request)
        return ControlResult(
            veyro_session_id=self.identity.veyro_session_id,
            provider_id=self.identity.provider_id,
            command_id=request.command_id,
            action=request.intent.action,
            outcome="executed",
            detail="SECRET",
        )

    async def execute_if_current(self, request, *, expected_sequence):
        if self.last_event_sequence != expected_sequence:
            return None
        return await self.execute(request)

    async def close(self):
        self.closed = True


def control(bridge, *, message=False):
    return ControlRequest(
        session=bridge.identity,
        command_id="cmd-1",
        intent=QueueFollowUp(message="SECRET")
        if message
        else ReplyToApproval(approval_id="pending-1", decision="deny", reason="Operator policy"),
    )


def action(request, operation=BoundaryOperation.DECLINE_APPROVAL):
    return BoundaryAction(
        session=request.session,
        action_id=request.command_id,
        operation=operation,
        target_sha256=control_request_sha256(request),
    )


def snapshot(bridge):
    reducer = SessionReducer(bridge.identity)
    for event in bridge.items:
        reducer.apply(event)
    return reducer.snapshot()


def approval(request, decision="approve"):
    now = datetime.now(UTC)
    return HumanApprovalEvidence(
        approval_id="operator-approval",
        request_sha256=control_request_sha256(request),
        decision=decision,
        approved_by="test-operator",
        issued_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=1),
    )


def automatic():
    return RolloutPolicy(mode="automatic", automatic_actions=("deny_approval",))


@pytest.mark.parametrize("mode", list(RolloutMode))
@pytest.mark.parametrize("stability", ["stable", "experimental", "internal", None])
@pytest.mark.parametrize("availability", ["supported", "unsupported", "unknown"])
def test_rollout_mode_capability_matrix(tmp_path, mode, stability, availability):
    bridge = ObservedBridge(tmp_path, stability=stability, availability=availability)
    request = control(bridge)
    policy = RolloutPolicy(
        mode=mode, automatic_actions=("deny_approval",) if mode is RolloutMode.AUTOMATIC else ()
    )
    gate = ControlAuthorizationGate(policy)
    result = gate.authorize(
        request,
        capabilities=bridge.capabilities,
        state=snapshot(bridge),
        boundary=gate.boundary_policy.classify(action(request)),
    )
    can_auto = (
        mode is RolloutMode.AUTOMATIC and stability == "stable" and availability == "supported"
    )
    assert (result.outcome is AuthorizationOutcome.AUTHORIZED) == can_auto
    assert result.rollout_mode is mode and result.rollout_policy_sha256 == policy.sha256


@pytest.mark.parametrize("decision", ["approve", "deny"])
@pytest.mark.parametrize("mode", ["observe_only", "advisory"])
async def test_nonexecuting_modes_never_deliver_even_with_approval(tmp_path, decision, mode):
    bridge = ObservedBridge(tmp_path)
    request = control(bridge)
    policy = RolloutPolicy(mode=mode)
    gate = ControlAuthorizationGate(policy)
    ledger = DeliveryLedger(tmp_path / "ledger")
    result = await AuthorizedControlDispatcher(bridge, gate, ledger=ledger).dispatch(
        request,
        state=snapshot(bridge),
        boundary=gate.boundary_policy.classify(action(request)),
        human_approval=approval(request, decision),
    )
    assert result.result is None and not bridge.calls
    assert result.effect.status is ControlEffectStatus.NOT_APPLICABLE
    assert result.authorization.reason is (
        AuthorizationReason.OBSERVE_ONLY
        if mode == "observe_only"
        else AuthorizationReason.HUMAN_DENIED
        if decision == "deny"
        else AuthorizationReason.ADVISORY_ONLY
    )
    (decision_receipt,) = (tmp_path / "ledger").glob("*.decision.json")
    assert json.loads(decision_receipt.read_text())["outcome"] == "denied"


@pytest.mark.parametrize("modification", ["deny", "future", "expired", "wrong_hash"])
def test_supplied_approval_cannot_be_ignored_by_automatic_low_risk_path(tmp_path, modification):
    bridge = ObservedBridge(tmp_path)
    request = control(bridge)
    human = approval(request, "deny" if modification == "deny" else "approve")
    updates = {
        "future": {"issued_at": datetime.now(UTC) + timedelta(seconds=30)},
        "expired": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        "wrong_hash": {"request_sha256": "0" * 64},
        "deny": {},
    }
    human = human.model_copy(update=updates[modification])
    gate = ControlAuthorizationGate(automatic())
    result = gate.authorize(
        request,
        capabilities=bridge.capabilities,
        state=snapshot(bridge),
        boundary=gate.boundary_policy.classify(action(request)),
        human_approval=human,
    )
    assert result.outcome is not AuthorizationOutcome.AUTHORIZED
    assert result.reason is (
        AuthorizationReason.HUMAN_DENIED
        if modification == "deny"
        else AuthorizationReason.HUMAN_APPROVAL_INVALID
    )


def test_free_text_never_automatic_and_boundary_substitution_is_rejected(tmp_path):
    bridge = ObservedBridge(tmp_path)
    request = control(bridge, message=True)
    gate = ControlAuthorizationGate(automatic())
    boundary = gate.boundary_policy.classify(action(request, BoundaryOperation.READ_REPOSITORY))
    result = gate.authorize(
        request, capabilities=bridge.capabilities, state=snapshot(bridge), boundary=boundary
    )
    assert result.outcome is AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED
    changed = request.model_copy(update={"intent": QueueFollowUp(message="different instruction")})
    result = gate.authorize(
        changed, capabilities=bridge.capabilities, state=snapshot(bridge), boundary=boundary
    )
    assert result.reason is AuthorizationReason.EVIDENCE_MISMATCH
    forged = boundary.model_copy(update={"disposition": "forbidden"})
    result = gate.authorize(
        request, capabilities=bridge.capabilities, state=snapshot(bridge), boundary=forged
    )
    assert result.reason is AuthorizationReason.EVIDENCE_MISMATCH


async def test_automatic_decline_requires_pending_approval_and_durable_claim(tmp_path):
    bridge = ObservedBridge(tmp_path)
    request = control(bridge)
    gate = ControlAuthorizationGate(automatic())
    evidence = dict(state=snapshot(bridge), boundary=gate.boundary_policy.classify(action(request)))
    ledger = DeliveryLedger(tmp_path / "ledger")
    dispatcher = AuthorizedControlDispatcher(bridge, gate, ledger=ledger)
    delivered = await dispatcher.dispatch(request, **evidence)
    duplicate = await AuthorizedControlDispatcher(
        bridge, gate, ledger=DeliveryLedger(tmp_path / "ledger")
    ).dispatch(request, **evidence)
    assert delivered.result is not None
    assert delivered.effect.status is ControlEffectStatus.ACKNOWLEDGED_UNVERIFIED
    assert duplicate.result is None
    assert duplicate.effect.status is ControlEffectStatus.NOT_APPLICABLE
    assert len(bridge.calls) == 1
    assert len(list((tmp_path / "ledger").glob("*.attempt.json"))) == 1
    assert all("SECRET" not in path.read_text() for path in (tmp_path / "ledger").iterdir())
    state = evidence["state"].model_copy(update={"pending_approvals": ()})
    result = gate.authorize(
        request, capabilities=bridge.capabilities, state=state, boundary=evidence["boundary"]
    )
    assert result.reason is AuthorizationReason.UNSAFE_STATE


async def test_exact_provider_rejection_is_failed_effect(tmp_path):
    bridge = ObservedBridge(tmp_path)
    request = control(bridge)
    gate = ControlAuthorizationGate(automatic())

    async def reject(control_request):
        bridge.calls.append(control_request)
        return ControlResult(
            veyro_session_id=bridge.identity.veyro_session_id,
            provider_id=bridge.identity.provider_id,
            command_id=control_request.command_id,
            action=control_request.intent.action,
            outcome="rejected",
            detail="provider rejected the control",
        )

    bridge.execute = reject
    result = await AuthorizedControlDispatcher(
        bridge, gate, ledger=DeliveryLedger(tmp_path / "ledger")
    ).dispatch(
        request,
        state=snapshot(bridge),
        boundary=gate.boundary_policy.classify(action(request)),
    )
    assert result.result is not None
    assert result.effect.status is ControlEffectStatus.FAILED
    (attempt,) = (tmp_path / "ledger").glob("*.attempt.json")
    assert json.loads(attempt.read_text())["effect"] == "failed"


async def test_review_required_control_denies_without_invoking_assessor(tmp_path):
    bridge = ObservedBridge(tmp_path)
    request = control(bridge, message=True)
    assessor = FakeAssessor()
    loop = SupervisionControlLoop(
        bridge,
        CheckpointAssessmentService(assessor),
        policy=RolloutPolicy(mode="approval_required"),
        ledger=DeliveryLedger(tmp_path / "ledger"),
    )
    loop.reducer.apply(bridge.items[0])
    result = await loop.run_control(
        event=bridge.items[1],
        request=request,
        boundary_action=action(request, BoundaryOperation.NETWORK_READ),
        human_approval=approval(request),
    )
    assert result.control.authorization.reason is AuthorizationReason.SEMANTIC_EVIDENCE_REQUIRED
    assert result.effect.status is ControlEffectStatus.NOT_APPLICABLE
    assert assessor.calls == 0
    assert not bridge.calls
    (decision,) = (tmp_path / "ledger").glob("*.decision.json")
    assert json.loads(decision.read_text())["reason"] == "semantic_evidence_required"


async def test_uncertain_delivery_claim_is_never_retried(tmp_path):
    bridge = ObservedBridge(tmp_path)
    request = control(bridge)
    gate = ControlAuthorizationGate(automatic())
    evidence = dict(state=snapshot(bridge), boundary=gate.boundary_policy.classify(action(request)))
    entered = asyncio.Event()

    async def hanging(request):
        bridge.calls.append(request)
        entered.set()
        await asyncio.Event().wait()

    bridge.execute = hanging
    dispatcher = AuthorizedControlDispatcher(
        bridge, gate, ledger=DeliveryLedger(tmp_path / "ledger")
    )
    pending = asyncio.create_task(dispatcher.dispatch(request, **evidence))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    retry = await dispatcher.dispatch(request, **evidence)
    assert retry.result is None
    assert retry.effect.status is ControlEffectStatus.NOT_APPLICABLE
    assert len(bridge.calls) == 1
    (attempt,) = (tmp_path / "ledger").glob("*.attempt.json")
    assert json.loads(attempt.read_text())["effect"] == "unknown"


async def test_state_change_during_approval_rejects_stale_evidence(tmp_path):
    bridge = ObservedBridge(tmp_path)
    request = control(bridge, message=True)
    loop = SupervisionControlLoop(
        bridge,
        CheckpointAssessmentService(FakeAssessor()),
        policy=RolloutPolicy(mode="approval_required"),
        ledger=DeliveryLedger(tmp_path / "ledger"),
    )
    loop.reducer.apply(bridge.items[0])

    async def approve(_):
        bridge.items.append(bridge.event(3, "session_idle"))
        return approval(request)

    result = await loop.run_control(
        event=bridge.items[1],
        request=request,
        boundary_action=action(request, BoundaryOperation.READ_REPOSITORY),
        approval_provider=approve,
    )
    assert result.control.authorization.reason is AuthorizationReason.STALE_OBSERVATION
    assert result.effect.status is ControlEffectStatus.NOT_APPLICABLE
    assert not bridge.calls
    reasons = {
        json.loads(decision.read_text())["reason"]
        for decision in (tmp_path / "ledger").glob("*.decision.json")
    }
    assert reasons == {"human_approval_required", "stale_observation"}


@pytest.mark.parametrize(
    "mode,operation", [("observe_only", "unknown"), ("automatic", "expose_credential")]
)
async def test_observe_and_forbidden_never_invoke_assessor(tmp_path, mode, operation):
    bridge = ObservedBridge(tmp_path)
    request = control(bridge, message=True)
    assessor = FakeAssessor()
    loop = SupervisionControlLoop(
        bridge,
        CheckpointAssessmentService(assessor),
        policy=RolloutPolicy(mode=mode),
        ledger=DeliveryLedger(tmp_path / "ledger"),
    )
    loop.reducer.apply(bridge.items[0])
    result = await loop.run_control(
        event=bridge.items[1], request=request, boundary_action=action(request, operation)
    )
    assert not assessor.calls and not bridge.calls and result.control.result is None


def test_risk_configuration_only_strengthens_policy(tmp_path):
    from veyro.supervision.boundary_policy import BoundaryPolicy

    bridge = ObservedBridge(tmp_path)
    request = control(bridge)
    policy = BoundaryPolicy(
        review_operations=(BoundaryOperation.DECLINE_APPROVAL, BoundaryOperation.EXPOSE_CREDENTIAL)
    )
    assert policy.classify(action(request)).disposition.value == "review_required"
    assert (
        policy.classify(action(request, BoundaryOperation.EXPOSE_CREDENTIAL)).disposition.value
        == "forbidden"
    )
    with pytest.raises(ValueError):
        RolloutPolicy(mode="automatic", automatic_actions=("stop_session",))
    with pytest.raises(ValueError):
        ControlProposal.model_validate(
            {"command_id": "one", "intent": request.intent.model_dump(), "mode": "automatic"}
        )


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("mode", ["observe_only", "advisory", "approval_required", "automatic"])
def test_real_cli_drives_policy_and_sanitizes_results(tmp_path, monkeypatch, mode):
    from veyro.supervision import supervisor

    bridge = ObservedBridge(tmp_path)
    records = []

    async def connect(*args, **kwargs):
        return bridge

    monkeypatch.setattr(supervisor, "connect_bridge", connect)
    # Mode without an allowlist remains approval-required, even on a stable adapter.
    proposal = private_json(
        tmp_path / "proposal.json",
        {
            "command_id": "cmd-1",
            "intent": {"action": "queue_follow_up", "message": "SECRET"},
            "operation": "read_repository",
        },
    )
    policy = private_json(tmp_path / "policy.json", {"mode": mode})
    # Patch the approval reader at the service seam, not the authorization path.
    original = supervisor.supervise_proposal

    async def supervise(**kwargs):
        emit = kwargs["emit"]

        def capture(record):
            records.append(record)
            emit(record)

        async def read():
            auth = records[-1]["authorization"]
            now = datetime.now(UTC)
            return HumanApprovalEvidence(
                approval_id="cli-approval",
                request_sha256=auth["request_sha256"],
                decision="approve",
                approved_by="test-operator",
                issued_at=now - timedelta(seconds=1),
                expires_at=now + timedelta(seconds=30),
            )

        return await original(**{**kwargs, "emit": capture, "approval_reader": read})

    monkeypatch.setattr(supervisor, "supervise_proposal", supervise)
    result = CliRunner().invoke(
        app,
        [
            "supervise",
            "--agent",
            "prime-agent",
            "--session",
            "native-session",
            "--repo",
            str(tmp_path),
            "--proposal",
            str(proposal),
            "--policy",
            str(policy),
            "--ledger-dir",
            str(tmp_path / "ledger"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "SECRET" not in result.output
    executing = mode in {"approval_required", "automatic"}
    assert len(bridge.calls) == int(executing)
    assert records[-1]["effect"]["status"] == (
        "acknowledged_unverified" if executing else "not_applicable"
    )
    assert bridge.closed
    assert list((tmp_path / "ledger").glob("*.decision.json"))
    assert records[-1]["type"] == "decision"


@pytest.mark.parametrize(
    "kind", ["exposed", "symlink", "oversized", "mode_in_proposal", "fifo", "hardlink"]
)
def test_cli_rejects_unsafe_input_before_connecting(tmp_path, monkeypatch, kind):
    from veyro.supervision import supervisor

    async def forbidden(*args, **kwargs):
        pytest.fail("must not connect")

    monkeypatch.setattr(supervisor, "connect_bridge", forbidden)
    path = private_json(
        tmp_path / "input.json",
        {"command_id": "cmd", "intent": {"action": "queue_follow_up", "message": "SECRET"}},
    )
    if kind == "exposed":
        path.chmod(0o644)
    elif kind == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        path = link
    elif kind == "oversized":
        path.write_bytes(b"SECRET" * 20000)
    elif kind == "fifo":
        import os

        path.unlink()
        os.mkfifo(path, 0o600)
    elif kind == "hardlink":
        import os

        os.link(path, tmp_path / "duplicate")
    else:
        value = json.loads(path.read_text())
        value["mode"] = "automatic"
        path.write_text(json.dumps(value))
    result = CliRunner().invoke(
        app,
        [
            "supervise",
            "--agent",
            "prime-agent",
            "--session",
            "s",
            "--repo",
            str(tmp_path),
            "--proposal",
            str(path),
        ],
    )
    assert result.exit_code == 2 and "SECRET" not in result.output


async def test_approval_pipe_is_bounded_and_cancellable(tmp_path, monkeypatch):
    import os

    from veyro.supervision import supervisor

    reader_fd, writer_fd = os.pipe()
    with os.fdopen(reader_fd, "rb", buffering=0) as pipe:
        monkeypatch.setattr(supervisor.sys, "stdin", pipe)
        original_blocking = os.get_blocking(reader_fd)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(supervisor.read_approval(), 0.01)
            assert os.get_blocking(reader_fd) == original_blocking
            human = approval(control(ObservedBridge(tmp_path)))
            os.write(writer_fd, human.model_dump_json().encode() + b"\n")
            assert await asyncio.wait_for(supervisor.read_approval(), 1) == human
            assert os.get_blocking(reader_fd) == original_blocking
        finally:
            os.close(writer_fd)


async def test_changed_context_and_boundary_do_not_reuse_semantic_cache(tmp_path):
    from veyro.supervision.boundary_policy import BoundaryPolicy

    bridge = ObservedBridge(tmp_path)
    request = control(bridge, message=True)
    state = snapshot(bridge)
    assessor = FakeAssessor()
    service = CheckpointAssessmentService(assessor)
    boundary = BoundaryPolicy().classify(action(request, BoundaryOperation.UNKNOWN))
    first = await service.assess_if_needed(
        bridge.items[-1], state, boundary_decision=boundary, task_context="one"
    )
    second = await service.assess_if_needed(
        bridge.items[-1], state, boundary_decision=boundary, task_context="two"
    )
    changed = request.model_copy(update={"intent": QueueFollowUp(message="different")})
    another = BoundaryPolicy().classify(action(changed, BoundaryOperation.UNKNOWN))
    third = await service.assess_if_needed(
        bridge.items[-1], state, boundary_decision=another, task_context="two"
    )
    assert assessor.calls == 3
    assert first.checkpoint_id == second.checkpoint_id
    assert second.checkpoint_id != third.checkpoint_id


@pytest.mark.parametrize("change", ["expiry", "cursor", "decision_cursor", "delivery_cursor"])
async def test_persistence_cannot_outlive_approval_or_snapshot(tmp_path, monkeypatch, change):
    from veyro.supervision import authorization as module

    bridge = ObservedBridge(tmp_path)
    request = control(bridge)
    human = approval(request)
    clock = [datetime.now(UTC)]

    class Clock:
        @staticmethod
        def now(zone):
            return clock[0]

    monkeypatch.setattr(module, "datetime", Clock)

    class AdvancingLedger(DeliveryLedger):
        decision_count = 0

        def claim(self, **kwargs):
            delivery_claim = super().claim(**kwargs)
            if change == "expiry":
                clock[0] = human.expires_at + timedelta(seconds=1)
            elif change == "cursor":
                bridge.items.append(bridge.event(3, "session_idle"))
            return delivery_claim

        def record_authorization(self, **kwargs):
            super().record_authorization(**kwargs)
            self.decision_count += 1
            if change == "decision_cursor" and self.decision_count == 2:
                bridge.items.append(bridge.event(3, "session_idle"))
    if change == "delivery_cursor":
        deliver_if_current = bridge.execute_if_current

        async def advance_at_delivery(request, *, expected_sequence):
            bridge.items.append(bridge.event(3, "session_idle"))
            return await deliver_if_current(request, expected_sequence=expected_sequence)

        bridge.execute_if_current = advance_at_delivery
    gate = ControlAuthorizationGate(RolloutPolicy(mode="approval_required"))
    dispatcher = AuthorizedControlDispatcher(
        bridge, gate, ledger=AdvancingLedger(tmp_path / "ledger")
    )
    result = await dispatcher.dispatch(
        request,
        state=snapshot(bridge),
        boundary=gate.boundary_policy.classify(action(request)),
        human_approval=human,
    )
    assert result.result is None and not bridge.calls
    assert result.authorization.reason is (
        AuthorizationReason.HUMAN_APPROVAL_INVALID
        if change == "expiry"
        else AuthorizationReason.STALE_OBSERVATION
    )
    assert len(list((tmp_path / "ledger").iterdir())) == 4
    (attempt,) = (tmp_path / "ledger").glob("*.attempt.json")
    assert json.loads(attempt.read_text())["effect"] == "not_applicable"


async def test_stale_boundary_receipt_failure_leaves_ledger_reopenable(tmp_path):
    bridge = ObservedBridge(tmp_path)
    request = control(bridge)
    human = approval(request)
    deliver_if_current = bridge.execute_if_current

    async def advance_at_delivery(request, *, expected_sequence):
        bridge.items.append(bridge.event(3, "session_idle"))
        return await deliver_if_current(request, expected_sequence=expected_sequence)

    bridge.execute_if_current = advance_at_delivery

    class FailingLedger(DeliveryLedger):
        decisions = 0

        def record_authorization(self, **kwargs):
            self.decisions += 1
            if self.decisions == 3:
                raise RuntimeError("decision receipt unavailable")
            return super().record_authorization(**kwargs)

    root = tmp_path / "ledger"
    ledger = FailingLedger(root)
    gate = ControlAuthorizationGate(RolloutPolicy(mode="approval_required"))
    dispatcher = AuthorizedControlDispatcher(bridge, gate, ledger=ledger)

    with pytest.raises(RuntimeError, match="decision receipt unavailable"):
        await dispatcher.dispatch(
            request,
            state=snapshot(bridge),
            boundary=gate.boundary_policy.classify(action(request)),
            human_approval=human,
        )

    DeliveryLedger(root)


async def test_persistence_wait_can_be_cancelled_without_native_delivery(tmp_path):
    import threading

    bridge = ObservedBridge(tmp_path)
    request = control(bridge)
    gate = ControlAuthorizationGate(automatic())
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    class BlockingLedger(DeliveryLedger):
        def claim(self, **kwargs):
            delivery_claim = super().claim(**kwargs)
            loop.call_soon_threadsafe(started.set)
            assert release.wait(2)
            return delivery_claim
    dispatcher = AuthorizedControlDispatcher(
        bridge, gate, ledger=BlockingLedger(tmp_path / "ledger")
    )
    task = asyncio.create_task(
        dispatcher.dispatch(
            request, state=snapshot(bridge), boundary=gate.boundary_policy.classify(action(request))
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not bridge.calls
        assert len(list((tmp_path / "ledger").iterdir())) == 2
    finally:
        release.set()
