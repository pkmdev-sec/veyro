from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from veyro.bridges.prime_agent import PrimeAgentDaemonBridge, PrimeDaemonClient
from veyro.models import (
    ApprovalDecision,
    AuthorizationOutcome,
    AuthorizationReason,
    BoundaryAction,
    BoundaryOperation,
    ControlOutcome,
    ControlRequest,
    HumanApprovalEvidence,
    StopSession,
)
from veyro.models.rollout import RolloutPolicy
from veyro.supervision.authorization import control_request_sha256
from veyro.supervision.checkpoints import (
    AUTHORITATIVE_MODEL_CHECKPOINT,
    CheckpointAssessmentService,
    LocalJevCheckpointAssessor,
)
from veyro.supervision.control_loop import ControlLoopEvidence, SupervisionControlLoop
from veyro.supervision.delivery import DeliveryLedger
from veyro.veyro.jev import JevVeyroModel


def default_socket_path() -> Path:
    user = str(os.getuid()) if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"prime-agent-{user}" / "daemon.sock"


async def run_prime_canary(
    *,
    socket_path: Path,
    repository: Path,
    approved_by: str,
) -> ControlLoopEvidence:
    client = PrimeDaemonClient(socket_path)
    bridge: PrimeAgentDaemonBridge | None = None
    assessments: CheckpointAssessmentService | None = None
    active_session_id: str | None = None
    stopped = False
    ledger_tree = tempfile.TemporaryDirectory(prefix="veyro-canary-delivery-")
    try:
        resolved_repository = await asyncio.to_thread(repository.resolve)
        await client.connect()
        created = await client.request(
            {
                "type": "create",
                "noSession": True,
                "name": "veyro-sup009-canary",
                "lifecycle": "client_owned",
                "config": {"cwd": str(resolved_repository)},
            }
        )
        data = created.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("activeSessionId"), str):
            raise RuntimeError("Prime Agent did not return a disposable session identity")
        active_session_id = data["activeSessionId"]
        bridge = await PrimeAgentDaemonBridge.attach_connected(
            client=client,
            active_session_id=active_session_id,
            veyro_session_id="veyro-sup009-live-canary",
            repository=resolved_repository,
        )
        events = bridge.events()
        trigger = await anext(events)
        await events.aclose()
        request = ControlRequest(
            session=bridge.identity,
            command_id="sup009-stop-disposable-session",
            intent=StopSession(
                reason="Complete and clean up the no-prompt SUP-009 canary session."
            ),
        )
        action = BoundaryAction(
            action_id=request.command_id,
            session=request.session,
            operation=BoundaryOperation.UNKNOWN,
            target_sha256=control_request_sha256(request),
        )
        now = datetime.now(UTC)
        approval = HumanApprovalEvidence(
            approval_id="sup009-explicit-stop-approval",
            request_sha256=control_request_sha256(request),
            decision=ApprovalDecision.APPROVE,
            approved_by=approved_by,
            issued_at=now,
            expires_at=now + timedelta(minutes=10),
        )
        model = JevVeyroModel(
            provider_id="localjev-qwen3-14b",
            base_url="http://127.0.0.1:8080",
            api_key="loopback-localjev",
            model="jev-latest",
            checkpoint=AUTHORITATIVE_MODEL_CHECKPOINT,
            role="authoritative",
            timeout_seconds=180,
            max_state_characters=50_000,
            state_format="json",
        )
        assessments = CheckpointAssessmentService(LocalJevCheckpointAssessor(model))
        loop = SupervisionControlLoop(
            bridge,
            assessments,
            policy=RolloutPolicy(mode="approval_required"),
            ledger=DeliveryLedger(await asyncio.to_thread(Path(ledger_tree.name).resolve)),
        )
        for attempt in range(2):
            evidence = await loop.run_control(
                event=trigger,
                request=request,
                boundary_action=action,
                human_approval=approval,
                task_context=(
                    "This is a disposable, no-prompt Prime Agent session. "
                    "Stop only this isolated canary session after policy review."
                ),
                verification_timeout=10,
            )
            if evidence.control.authorization.reason is not AuthorizationReason.STALE_OBSERVATION:
                break
            if attempt == 1:
                break
            # Only this owned no-prompt fixture may reassess after startup metadata arrives.
            # A stale decision sent no control and acquired no delivery claim.
            pending = bridge.events(after_sequence=loop.reducer.snapshot().last_sequence)
            try:
                trigger = await asyncio.wait_for(anext(pending), timeout=5)
                while trigger.sequence < bridge.last_event_sequence:
                    if trigger.sequence >= 1000:
                        raise RuntimeError("canary startup exceeded observation limit")
                    loop.reducer.apply(trigger)
                    trigger = await asyncio.wait_for(anext(pending), timeout=5)
            finally:
                await pending.aclose()
        result = evidence.control.result
        stopped = (
            evidence.control.authorization.outcome is AuthorizationOutcome.AUTHORIZED
            and result is not None
            and result.outcome is ControlOutcome.EXECUTED
            and evidence.verification_event is not None
        )
        if not stopped:
            raise RuntimeError(
                "canary stop not delivered/verified: "
                + evidence.control.authorization.reason.value
                + f" (assessed sequence {trigger.sequence}, observed {bridge.last_event_sequence})"
            )
        return evidence
    finally:
        if assessments is not None:
            await assessments.close()
        if active_session_id is not None and not stopped:
            try:
                await client.request({"type": "kill", "activeSessionId": active_session_id})
            except Exception:
                pass
        try:
            if bridge is not None:
                await bridge.close()
            else:
                await client.close()
        finally:
            ledger_tree.cleanup()


def _summary(evidence: ControlLoopEvidence) -> dict[str, object]:
    assessment = evidence.assessment
    result = evidence.control.result
    verification = evidence.verification_event
    return {
        "trigger": evidence.trigger_event.event_type.value,
        "checkpoint": evidence.checkpoint.kind.value if evidence.checkpoint else None,
        "semantic_provider": assessment.provenance.provider_id if assessment else None,
        "semantic_checkpoint": assessment.provenance.checkpoint if assessment else None,
        "boundary": evidence.boundary.disposition.value,
        "authorization": evidence.control.authorization.outcome.value,
        "control": result.outcome.value if result else None,
        "verification": verification.event_type.value if verification else None,
        "verification_reason": verification.payload.get("reason") if verification else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the no-prompt SUP-009 Prime Agent canary")
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--approve-stop", action="store_true")
    args = parser.parse_args()
    if not args.approve_stop:
        parser.error("--approve-stop is required for the isolated session stop")
    evidence = asyncio.run(
        run_prime_canary(
            socket_path=args.socket,
            repository=args.repo,
            approved_by=args.approved_by,
        )
    )
    print(json.dumps(_summary(evidence), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
