from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

PROTOCOL_VERSION = "1.0"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BridgeSource(StrEnum):
    PROCESS = "process"
    WORKSPACE = "workspace"
    DAEMON = "daemon"
    SERVER = "server"
    EXTENSION = "extension"
    HOOK = "hook"
    SESSION_FILE = "session_file"


class BridgeStability(StrEnum):
    STABLE = "stable"
    EXPERIMENTAL = "experimental"
    INTERNAL = "internal"


class CapabilityAvailability(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class EventSensitivity(StrEnum):
    METADATA = "metadata"
    REDACTED = "redacted"
    CONTENT_OPT_IN = "content_opt_in"


class SupervisionEventType(StrEnum):
    SESSION_STARTED = "session_started"
    USER_PROMPT_SUBMITTED = "user_prompt_submitted"
    TURN_STARTED = "turn_started"
    PLAN_UPDATED = "plan_updated"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    FILE_CHANGED = "file_changed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    TEST_COMPLETED = "test_completed"
    AGENT_MESSAGE_COMPLETED = "agent_message_completed"
    TURN_COMPLETED = "turn_completed"
    SESSION_IDLE = "session_idle"
    SESSION_COMPLETED = "session_completed"
    SESSION_FAILED = "session_failed"
    UNKNOWN = "unknown"
    NORMALIZATION_FAILED = "normalization_failed"


class SessionIdentity(ContractModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    veyro_session_id: str = Field(min_length=1, max_length=200)
    provider_id: str = Field(min_length=1, max_length=100)
    provider_session_id: str | None = Field(default=None, max_length=500)
    repository: str = Field(min_length=1, max_length=10_000)
    provider_version: str | None = Field(default=None, max_length=200)
    bridge_id: str = Field(min_length=1, max_length=100)
    bridge_version: str = Field(min_length=1, max_length=200)
    started_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class EventProvenance(ContractModel):
    source: BridgeSource
    native_event_type: str = Field(min_length=1, max_length=200)
    native_event_id: str | None = Field(default=None, max_length=500)
    observed_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    raw_event_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    replayed: bool = False


class SupervisionEvent(ContractModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    event_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=200)
    session: SessionIdentity
    sequence: int = Field(ge=1)
    occurred_at: AwareDatetime | None = None
    event_type: SupervisionEventType
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    provenance: EventProvenance
    sensitivity: EventSensitivity = EventSensitivity.METADATA


class BridgeCapability(StrEnum):
    OBSERVE_LIFECYCLE = "observe_lifecycle"
    OBSERVE_MESSAGES = "observe_messages"
    OBSERVE_TOOLS = "observe_tools"
    OBSERVE_APPROVALS = "observe_approvals"
    REPLAY_EVENTS = "replay_events"
    QUEUE_FOLLOW_UP = "queue_follow_up"
    STEER_ACTIVE_TURN = "steer_active_turn"
    INTERRUPT_TURN = "interrupt_turn"
    STOP_SESSION = "stop_session"
    REPLY_TO_APPROVAL = "reply_to_approval"
    ATTACH_EXISTING = "attach_existing"


class CapabilityDeclaration(ContractModel):
    capability: BridgeCapability
    availability: CapabilityAvailability
    stability: BridgeStability | None = None
    evidence: str = Field(min_length=1, max_length=2_000)
    detail: str = Field(default="", max_length=2_000)


class CapabilitySet(ContractModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    provider_id: str = Field(min_length=1, max_length=100)
    provider_version: str | None = Field(default=None, max_length=200)
    bridge_id: str = Field(min_length=1, max_length=100)
    bridge_version: str = Field(min_length=1, max_length=200)
    probed_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    declarations: tuple[CapabilityDeclaration, ...] = ()

    @model_validator(mode="after")
    def declarations_are_unique(self) -> Self:
        capabilities = [item.capability for item in self.declarations]
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("capability declarations must be unique")
        return self

    def availability(self, capability: BridgeCapability) -> CapabilityAvailability:
        return next(
            (item.availability for item in self.declarations if item.capability is capability),
            CapabilityAvailability.UNKNOWN,
        )

    def supports(self, capability: BridgeCapability) -> bool:
        return self.availability(capability) is CapabilityAvailability.SUPPORTED


class RawProviderEvent(ContractModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    raw_event_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=200)
    session: SessionIdentity
    observed_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    native_event_type: str = Field(min_length=1, max_length=200)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: JsonValue | None = None
    sensitivity: EventSensitivity = EventSensitivity.METADATA

    @model_validator(mode="after")
    def raw_content_requires_opt_in(self) -> Self:
        if self.payload is not None and self.sensitivity is not EventSensitivity.CONTENT_OPT_IN:
            raise ValueError("raw provider payload requires content_opt_in sensitivity")
        return self


class ControlAction(StrEnum):
    QUEUE_FOLLOW_UP = "queue_follow_up"
    STEER_ACTIVE_TURN = "steer_active_turn"
    INTERRUPT_TURN = "interrupt_turn"
    STOP_SESSION = "stop_session"
    REPLY_TO_APPROVAL = "reply_to_approval"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    DENY = "deny"


class QueueFollowUp(ContractModel):
    action: Literal[ControlAction.QUEUE_FOLLOW_UP] = ControlAction.QUEUE_FOLLOW_UP
    message: str = Field(min_length=1, max_length=50_000)


class SteerActiveTurn(ContractModel):
    action: Literal[ControlAction.STEER_ACTIVE_TURN] = ControlAction.STEER_ACTIVE_TURN
    message: str = Field(min_length=1, max_length=50_000)


class InterruptTurn(ContractModel):
    action: Literal[ControlAction.INTERRUPT_TURN] = ControlAction.INTERRUPT_TURN
    reason: str = Field(min_length=1, max_length=2_000)


class StopSession(ContractModel):
    action: Literal[ControlAction.STOP_SESSION] = ControlAction.STOP_SESSION
    reason: str = Field(min_length=1, max_length=2_000)


class ReplyToApproval(ContractModel):
    action: Literal[ControlAction.REPLY_TO_APPROVAL] = ControlAction.REPLY_TO_APPROVAL
    approval_id: str = Field(min_length=1, max_length=500)
    decision: ApprovalDecision
    reason: str = Field(min_length=1, max_length=2_000)


ControlIntent = Annotated[
    QueueFollowUp | SteerActiveTurn | InterruptTurn | StopSession | ReplyToApproval,
    Field(discriminator="action"),
]


class ControlRequest(ContractModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    session: SessionIdentity
    command_id: str = Field(min_length=1, max_length=200)
    intent: ControlIntent
    issued_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class ControlOutcome(StrEnum):
    EXECUTED = "executed"
    UNSUPPORTED = "unsupported"
    REJECTED = "rejected"
    FAILED = "failed"


class ControlResult(ContractModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    veyro_session_id: str = Field(min_length=1, max_length=200)
    provider_id: str = Field(min_length=1, max_length=100)
    command_id: str = Field(min_length=1, max_length=200)
    action: ControlAction
    outcome: ControlOutcome
    detail: str = Field(default="", max_length=2_000)
    provider_command_id: str | None = Field(default=None, max_length=500)
    completed_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def unsuccessful_results_explain_why(self) -> Self:
        if self.outcome is not ControlOutcome.EXECUTED and not self.detail.strip():
            raise ValueError("detail is required when a control was not executed")
        return self
