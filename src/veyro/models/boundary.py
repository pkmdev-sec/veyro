from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from veyro.models.supervision import PROTOCOL_VERSION, SessionIdentity


class BoundaryOperation(StrEnum):
    DECLINE_APPROVAL = "decline_approval"
    READ_REPOSITORY = "read_repository"
    WRITE_REPOSITORY = "write_repository"
    RUN_LOCAL_CHECK = "run_local_check"
    INSPECT_LOCAL_ENVIRONMENT = "inspect_local_environment"
    EXECUTE_LOCAL_COMMAND = "execute_local_command"
    NETWORK_READ = "network_read"
    NETWORK_WRITE = "network_write"
    INSTALL_DEPENDENCY = "install_dependency"
    WRITE_OUTSIDE_REPOSITORY = "write_outside_repository"
    DELETE_LOCAL_DATA = "delete_local_data"
    DELETE_REMOTE_RESOURCE = "delete_remote_resource"
    ACCESS_CREDENTIAL = "access_credential"
    MODIFY_SECURITY_CONTROL = "modify_security_control"
    PRIVILEGED_SYSTEM_CHANGE = "privileged_system_change"
    EXPOSE_CREDENTIAL = "expose_credential"
    BYPASS_SECURITY_CONTROL = "bypass_security_control"
    UNKNOWN = "unknown"


class BoundaryDisposition(StrEnum):
    LOW_RISK = "low_risk"
    REVIEW_REQUIRED = "review_required"
    FORBIDDEN = "forbidden"


class BoundaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BoundaryAction(BoundaryModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    action_id: str = Field(min_length=1, max_length=500)
    session: SessionIdentity
    operation: BoundaryOperation
    native_action_id: str | None = Field(default=None, max_length=500)
    target_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class BoundaryDecision(BoundaryModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    policy_version: Literal["1.0"] = "1.0"
    action: BoundaryAction
    disposition: BoundaryDisposition
    rule_id: str = Field(pattern=r"^boundary\.[a-z_]+$")
    reason: str = Field(min_length=1, max_length=500)
