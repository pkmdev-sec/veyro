from __future__ import annotations

from typing import Literal

from pydantic import Field

from veyro.models.supervision import CapabilitySet, ContractModel, SessionIdentity


class SessionCandidate(ContractModel):
    provider_id: str
    selector: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    native_session_id: str = Field(min_length=1, max_length=500)
    repository: str
    provider_version: str
    mode: Literal["native_daemon", "native_server", "local_hook_journal"]
    native_liveness: Literal["loaded", "unknown"] = "unknown"


class SessionDiscovery(ContractModel):
    protocol_version: Literal["1.0"] = "1.0"
    provider_id: str
    status: Literal["supported", "unsupported"] = "supported"
    detail: str
    sessions: tuple[SessionCandidate, ...] = ()
    truncated: bool = False
    skipped: int = Field(default=0, ge=0)


class AttachmentHistory(ContractModel):
    mode: Literal["snapshot_metadata_only", "unavailable", "local_hook_journal"]
    native_history_complete: Literal[False] = False
    detail: str
    snapshot_message_count: int | None = Field(default=None, ge=0)
    native_sequence: int | None = Field(default=None, ge=0)
    native_replay_status: Literal["complete", "partial", "unavailable", "unknown"] = "unknown"
    after_sequence: int = Field(default=0, ge=0)


class AttachmentReport(ContractModel):
    protocol_version: Literal["1.0"] = "1.0"
    candidate: SessionCandidate
    identity: SessionIdentity
    history: AttachmentHistory
    observation: Literal["live_native_stream", "local_journal_only"]
    provider_capabilities: CapabilitySet
    controls_enabled: Literal[False] = False
    content_included: Literal[False] = False
    evidence_retention: Literal["stdout_only"] = "stdout_only"
