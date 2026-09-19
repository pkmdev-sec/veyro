from foreman.bridges.base import (
    AgentBridge,
    BridgeContractError,
    capability_for,
    required_capability,
    validate_connection,
    validate_control_result,
    validate_event_batch,
)
from foreman.bridges.opencode import (
    OpenCodeClient,
    OpenCodeError,
    OpenCodeServerBridge,
    attached_tui_command,
    opencode_server_capabilities,
)
from foreman.bridges.prime_agent import (
    PrimeAgentDaemonBridge,
    PrimeDaemonClient,
    PrimeDaemonError,
    prime_daemon_capabilities,
)

__all__ = [
    "AgentBridge",
    "BridgeContractError",
    "capability_for",
    "required_capability",
    "validate_connection",
    "validate_control_result",
    "validate_event_batch",
    "OpenCodeClient",
    "OpenCodeError",
    "OpenCodeServerBridge",
    "attached_tui_command",
    "opencode_server_capabilities",
    "PrimeAgentDaemonBridge",
    "PrimeDaemonClient",
    "PrimeDaemonError",
    "prime_daemon_capabilities",
]
