from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from veyro.models import (
    BridgeCapability,
    CapabilitySet,
    ControlAction,
    ControlIntent,
    ControlOutcome,
    ControlRequest,
    ControlResult,
    SessionIdentity,
    SupervisionEvent,
)


class BridgeContractError(ValueError):
    """Raised when an adapter violates the provider-neutral bridge contract."""


_CONTROL_CAPABILITIES = {
    ControlAction.QUEUE_FOLLOW_UP: BridgeCapability.QUEUE_FOLLOW_UP,
    ControlAction.STEER_ACTIVE_TURN: BridgeCapability.STEER_ACTIVE_TURN,
    ControlAction.INTERRUPT_TURN: BridgeCapability.INTERRUPT_TURN,
    ControlAction.STOP_SESSION: BridgeCapability.STOP_SESSION,
    ControlAction.REPLY_TO_APPROVAL: BridgeCapability.REPLY_TO_APPROVAL,
}


def required_capability(action: ControlAction) -> BridgeCapability:
    return _CONTROL_CAPABILITIES[action]


def capability_for(intent: ControlIntent) -> BridgeCapability:
    return required_capability(intent.action)


def validate_connection(identity: SessionIdentity, capabilities: CapabilitySet) -> None:
    if capabilities.provider_id != identity.provider_id:
        raise BridgeContractError("capabilities must describe the connected provider")
    if capabilities.bridge_id != identity.bridge_id:
        raise BridgeContractError("capabilities must describe the connected bridge")
    if capabilities.bridge_version != identity.bridge_version:
        raise BridgeContractError("capabilities must use the connected bridge version")
    if (
        capabilities.provider_version is not None
        and identity.provider_version is not None
        and capabilities.provider_version != identity.provider_version
    ):
        raise BridgeContractError("capabilities must use the connected provider version")


def validate_event_batch(
    identity: SessionIdentity,
    events: Sequence[SupervisionEvent],
    *,
    after_sequence: int = 0,
) -> None:
    previous = after_sequence
    for event in events:
        if event.session != identity:
            raise BridgeContractError("event session identity does not match the bridge session")
        if event.sequence != previous + 1:
            raise BridgeContractError(
                "event sequences must be contiguous and strictly increasing after the cursor"
            )
        previous = event.sequence


def validate_control_result(
    request: ControlRequest,
    result: ControlResult,
    capabilities: CapabilitySet,
) -> None:
    if result.veyro_session_id != request.session.veyro_session_id:
        raise BridgeContractError("control result belongs to a different Veyro session")
    if result.provider_id != request.session.provider_id:
        raise BridgeContractError("control result belongs to a different provider")
    if result.command_id != request.command_id or result.action is not request.intent.action:
        raise BridgeContractError("control result does not match its request")
    supported = capabilities.supports(capability_for(request.intent))
    if result.outcome is ControlOutcome.EXECUTED and not supported:
        raise BridgeContractError("adapter executed a control it did not declare")
    if result.outcome is ControlOutcome.UNSUPPORTED and supported:
        raise BridgeContractError("adapter rejected a capability it declared as supported")


@runtime_checkable
class AgentBridge(Protocol):
    @property
    def last_event_sequence(self) -> int: ...

    @property
    def identity(self) -> SessionIdentity: ...

    @property
    def capabilities(self) -> CapabilitySet: ...

    def events(self, *, after_sequence: int = 0) -> AsyncIterator[SupervisionEvent]: ...

    async def execute(self, request: ControlRequest) -> ControlResult: ...

    async def close(self) -> None: ...
