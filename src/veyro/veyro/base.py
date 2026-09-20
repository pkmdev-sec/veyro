from __future__ import annotations

from typing import Protocol

from veyro.models import AssessmentBatch, FactoryAssessment
from veyro.observation import FactoryObservation


class VeyroModelError(RuntimeError):
    """The semantic supervisor could not produce a valid assessment."""


class VeyroModel(Protocol):
    async def assess(
        self, observation: FactoryObservation
    ) -> FactoryAssessment | AssessmentBatch: ...

    async def close(self) -> None: ...
