from __future__ import annotations

import asyncio
import os
import re
import stat
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from veyro.agents import AgentId
from veyro.bridges.base import AgentBridge
from veyro.bridges.opencode import OpenCodeClient, OpenCodeServerBridge
from veyro.bridges.prime_agent import PrimeAgentDaemonBridge
from veyro.models import (
    BoundaryAction,
    BoundaryOperation,
    ControlAuthorization,
    ControlIntent,
    ControlRequest,
    HumanApprovalEvidence,
)
from veyro.models.rollout import RolloutMode, RolloutPolicy
from veyro.models.supervision import ContractModel
from veyro.supervision.authorization import control_request_sha256
from veyro.supervision.checkpoints import (
    AUTHORITATIVE_MODEL_CHECKPOINT,
    AUTHORITATIVE_PROVIDER_ID,
    CheckpointAssessmentService,
    LocalJevCheckpointAssessor,
)
from veyro.supervision.control_loop import SupervisionControlLoop
from veyro.supervision.delivery import DeliveryLedger
from veyro.veyro.jev import JevVeyroModel

MAX_INPUT_BYTES = 65536


class ControlProposal(ContractModel):
    protocol_version: Literal["1.0"] = "1.0"
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    intent: ControlIntent
    operation: BoundaryOperation = BoundaryOperation.UNKNOWN


def read_operator_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > MAX_INPUT_BYTES
        ):
            raise ValueError(
                "operator input must be a private owned regular file within the size limit"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            data = source.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError("operator input exceeds size limit")
        return data
    finally:
        os.close(descriptor)


async def read_approval() -> HumanApprovalEvidence:
    descriptor = sys.stdin.fileno()
    if stat.S_ISREG(os.fstat(descriptor).st_mode):
        raw = os.read(descriptor, MAX_INPUT_BYTES + 1)
    else:
        blocking = os.get_blocking(descriptor)
        pipe = os.fdopen(os.dup(descriptor), "rb", buffering=0)
        transport = None
        try:
            reader = asyncio.StreamReader(limit=MAX_INPUT_BYTES + 1)
            protocol = asyncio.StreamReaderProtocol(reader)
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: protocol, pipe
            )
            raw = await reader.readline()
        finally:
            if transport is not None:
                transport.close()
            else:
                pipe.close()
            os.set_blocking(descriptor, blocking)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("approval exceeds size limit")
    return HumanApprovalEvidence.model_validate_json(raw)


async def connect_bridge(
    provider: AgentId, repository: Path, selector: str, *, socket: Path | None, server: str | None
) -> AgentBridge:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", selector):
        raise ValueError("invalid session selector")
    if provider is AgentId.PRIME_AGENT and socket is not None and server is None:
        return await PrimeAgentDaemonBridge.attach(
            socket_path=socket,
            active_session_id=selector,
            veyro_session_id=f"supervise-{uuid4().hex}",
            repository=repository,
        )
    if provider is AgentId.OPENCODE and server is not None and socket is None:
        client = OpenCodeClient.from_environment(server)
        try:
            return await OpenCodeServerBridge.attach_connected(
                client=client,
                provider_session_id=selector,
                veyro_session_id=f"supervise-{uuid4().hex}",
                repository=repository,
            )
        except BaseException:
            await client.close()
            raise
    raise ValueError(
        "supervise requires Prime --socket or OpenCode --server; other providers are unsupported"
    )


def authoritative_assessments() -> CheckpointAssessmentService:
    model = JevVeyroModel(
        provider_id=AUTHORITATIVE_PROVIDER_ID,
        base_url="http://127.0.0.1:8080",
        api_key="loopback-localjev",
        model="jev-latest",
        checkpoint=AUTHORITATIVE_MODEL_CHECKPOINT,
        role="authoritative",
        timeout_seconds=120,
        max_state_characters=50000,
        state_format="json",
    )
    return CheckpointAssessmentService(LocalJevCheckpointAssessor(model))


async def supervise_proposal(
    *,
    provider: AgentId,
    repository: Path,
    selector: str,
    proposal: ControlProposal,
    policy: RolloutPolicy,
    emit: Callable[[dict[str, object]], None],
    socket: Path | None = None,
    server: str | None = None,
    ledger_directory: Path | None = None,
    approval_reader: Callable[[], Awaitable[HumanApprovalEvidence]] = read_approval,
) -> None:
    executing_mode = policy.mode in {RolloutMode.APPROVAL_REQUIRED, RolloutMode.AUTOMATIC}
    if executing_mode and ledger_directory is None:
        raise ValueError("executing modes require an explicit persistent ledger directory")
    ledger = DeliveryLedger(ledger_directory) if executing_mode else None
    bridge = None
    assessments = None
    try:
        bridge = await connect_bridge(provider, repository, selector, socket=socket, server=server)
        if policy.mode is not RolloutMode.OBSERVE_ONLY:
            assessments = authoritative_assessments()
        loop = SupervisionControlLoop(bridge, assessments, policy=policy, ledger=ledger)
        await asyncio.sleep(0)
        events = bridge.events()
        try:
            trigger = await anext(events)
            # Reduce all metadata already received, never invent missing native history.
            while trigger.sequence < bridge.last_event_sequence:
                if trigger.sequence >= 1000:
                    raise ValueError("observation startup exceeds 1000 events")
                await asyncio.sleep(0)
                loop.reducer.apply(trigger)
                trigger = await anext(events)
        finally:
            await events.aclose()
        request = ControlRequest(
            session=bridge.identity, command_id=proposal.command_id, intent=proposal.intent
        )
        action = BoundaryAction(
            session=bridge.identity,
            action_id=request.command_id,
            operation=proposal.operation,
            target_sha256=control_request_sha256(request),
        )

        async def approve(authorization: ControlAuthorization) -> HumanApprovalEvidence:
            emit(
                {
                    "type": "approval_required",
                    "authorization": authorization.model_dump(mode="json"),
                }
            )
            return await approval_reader()

        emit(
            {
                "type": "supervision",
                "protocol_version": "1.0",
                "identity": bridge.identity.model_dump(mode="json"),
                "mode": policy.mode.value,
                "policy_sha256": policy.sha256,
                "request_sha256": control_request_sha256(request),
                "content_included": False,
                "native_history_complete": False,
            }
        )
        evidence = await loop.run_control(
            event=trigger, request=request, boundary_action=action, approval_provider=approve
        )
        result = evidence.control.result
        emit(
            {
                "type": "decision",
                "authorization": evidence.control.authorization.model_dump(mode="json"),
                "delivery": (
                    {"outcome": result.outcome.value, "command_id": result.command_id}
                    if result is not None
                    else None
                ),
                "checkpoint_id": evidence.checkpoint.checkpoint_id if evidence.checkpoint else None,
                "verification": (
                    evidence.verification_event.event_type.value
                    if evidence.verification_event
                    else None
                ),
            }
        )
    finally:
        try:
            if bridge is not None:
                await bridge.close()
        finally:
            if assessments is not None:
                await assessments.close()
