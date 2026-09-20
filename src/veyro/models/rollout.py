from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from veyro.models.boundary import BoundaryOperation
from veyro.models.supervision import ContractModel


class RolloutMode(StrEnum):
    OBSERVE_ONLY = "observe_only"
    ADVISORY = "advisory"
    APPROVAL_REQUIRED = "approval_required"
    AUTOMATIC = "automatic"


class RolloutPolicy(ContractModel):
    protocol_version: Literal["1.0"] = "1.0"
    mode: RolloutMode = RolloutMode.OBSERVE_ONLY
    automatic_actions: tuple[Literal["deny_approval"], ...] = Field(default=(), max_length=1)
    review_operations: tuple[BoundaryOperation, ...] = ()

    @model_validator(mode="after")
    def explicit_automatic_mode(self) -> Self:
        if self.automatic_actions and self.mode is not RolloutMode.AUTOMATIC:
            raise ValueError("automatic actions require automatic mode")
        if len(set(self.review_operations)) != len(self.review_operations):
            raise ValueError("review operations must be unique")
        return self

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()
