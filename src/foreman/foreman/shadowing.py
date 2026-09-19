from __future__ import annotations

import asyncio
from collections.abc import Mapping

from foreman.foreman.base import ForemanModel, ForemanModelError
from foreman.models import AssessmentBatch, FactoryAssessment, ProviderAssessmentFailure
from foreman.observation import FactoryObservation


class ShadowingForemanModel:
    """Runs one policy authority and bounded, non-authoritative observers in parallel."""

    def __init__(
        self,
        authoritative: ForemanModel,
        shadows: Mapping[str, ForemanModel],
    ) -> None:
        if not shadows:
            raise ValueError("at least one shadow provider is required")
        self.authoritative = authoritative
        self.shadows = dict(shadows)

    async def assess(self, observation: FactoryObservation) -> AssessmentBatch:
        provider_ids = list(self.shadows)
        results = await asyncio.gather(
            self.authoritative.assess(observation),
            *(self.shadows[provider_id].assess(observation) for provider_id in provider_ids),
            return_exceptions=True,
        )
        authoritative_result = results[0]
        if isinstance(authoritative_result, asyncio.CancelledError):
            raise authoritative_result
        if isinstance(authoritative_result, BaseException):
            if isinstance(authoritative_result, ForemanModelError):
                raise authoritative_result
            raise ForemanModelError(
                f"authoritative semantic provider failed: {authoritative_result}"
            ) from authoritative_result
        if isinstance(authoritative_result, AssessmentBatch):
            raise ForemanModelError("nested semantic assessment batches are not supported")
        if (
            authoritative_result.provenance is not None
            and authoritative_result.provenance.role != "authoritative"
        ):
            raise ForemanModelError("authoritative provider returned non-authoritative provenance")

        shadows: list[FactoryAssessment] = []
        failures: list[ProviderAssessmentFailure] = []
        for provider_id, result in zip(provider_ids, results[1:], strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                failures.append(self._failure(provider_id, result))
                continue
            if isinstance(result, AssessmentBatch):
                failures.append(
                    self._failure(provider_id, TypeError("nested assessment batch returned"))
                )
                continue
            if result.provenance is None or result.provenance.role != "shadow":
                failures.append(
                    self._failure(provider_id, ValueError("assessment lacks shadow provenance"))
                )
                continue
            if result.provenance.provider_id != provider_id:
                failures.append(
                    self._failure(provider_id, ValueError("assessment provider ID does not match"))
                )
                continue
            shadows.append(result)

        return AssessmentBatch(
            authoritative=authoritative_result,
            shadows=shadows,
            failures=failures,
        )

    async def close(self) -> None:
        results = await asyncio.gather(
            self.authoritative.close(),
            *(model.close() for model in self.shadows.values()),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result

    @staticmethod
    def _failure(provider_id: str, error: BaseException) -> ProviderAssessmentFailure:
        message = str(error).strip() or type(error).__name__
        return ProviderAssessmentFailure(
            provider_id=provider_id,
            error_type=type(error).__name__,
            message=message[:2_000],
        )
