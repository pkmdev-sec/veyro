from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from math import isfinite
from time import perf_counter
from typing import Any, Literal
from urllib.parse import urlsplit

from foreman.foreman.base import ForemanModelError
from foreman.models import AssessmentProvenance, FactoryAssessment, InferenceMetadata
from foreman.observation import FactoryObservation

ASSESSMENT_QUESTIONS_VERSION = "foreman-assessment-v1"


ASSESSMENT_QUESTIONS: dict[str, str] = {
    "implementation_complete": (
        "Is the implementation work required by the original job complete?"
    ),
    "tests_sufficient": (
        "Does the work have sufficient relevant test coverage and passing verification?"
    ),
    "requirements_satisfied": (
        "Does the current repository satisfy the original free-form job as a whole?"
    ),
    "needs_verification": (
        "Does the current state warrant an independent verification pass before finishing?"
    ),
    "meaningful_progress": (
        "Is the active or most recent worker making meaningful progress toward the job?"
    ),
    "worker_stuck": (
        "Does the active or most recent worker appear stuck, looping, or unable to advance?"
    ),
    "work_off_track": (
        "Is the current work drifting from the original job or making unrelated changes?"
    ),
    "ready_to_finish": ("Given all evidence, is the factory job ready to be declared complete?"),
    "needs_human": (
        "Does this situation require human judgment, credentials, clarification, or permission?"
    ),
}


def normalize_scores(
    values: Mapping[str, Any],
    question_names: Mapping[str, str] | set[str],
) -> dict[str, float]:
    names = tuple(question_names)
    normalized: dict[str, float] = {}
    missing = set(names) - set(values)
    if missing:
        raise ForemanModelError(f"Jev response omitted: {', '.join(sorted(missing))}")
    for name in names:
        raw = values[name]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ForemanModelError(f"Jev answer {name!r} is not numeric")
        value = float(raw)
        if not isfinite(value):
            raise ForemanModelError(f"Jev answer {name!r} is not finite")
        normalized[name] = min(1.0, max(0.0, value))
    return normalized


def parse_jev_scores(
    response: Any,
    questions: Mapping[str, str],
) -> dict[str, float]:
    values: dict[str, Any] = {}
    nouls = getattr(response, "nouls", None)
    if nouls is not None:
        for name in questions:
            answer = nouls.get(name)
            if answer is not None:
                values[name] = getattr(answer, "noul", None)
    elif isinstance(response, Mapping):
        answers = response.get("answers", response)
        if isinstance(answers, Mapping):
            for name in questions:
                answer = answers.get(name)
                if isinstance(answer, Mapping):
                    values[name] = answer.get("noul")
                elif answer is not None:
                    values[name] = answer
    return normalize_scores(values, questions)


def normalize_assessment(values: Mapping[str, Any]) -> FactoryAssessment:
    """Validate all nine Noul probabilities and clamp minor numeric overshoot."""

    return FactoryAssessment(**normalize_scores(values, ASSESSMENT_QUESTIONS))


def parse_jev_response(response: Any) -> FactoryAssessment:
    """Translate official SDK response types (or their test doubles) into our model."""

    return FactoryAssessment(**parse_jev_scores(response, ASSESSMENT_QUESTIONS))


def _serialized_state(state: dict[str, Any], state_format: str) -> str:
    def scalar(value: Any) -> str:
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    if state_format == "json":
        return json.dumps(state, ensure_ascii=False)
    if state_format == "values":
        return "\n".join(scalar(value) for value in state.values())
    return "\n".join(f"{key}: {scalar(value)}" for key, value in state.items())


class JevForemanModel:
    """Adapter for TypeSafe's official asynchronous Python SDK."""

    def __init__(
        self,
        *,
        provider_id: str = "localjev-qwen3-14b",
        base_url: str = "http://127.0.0.1:8080",
        api_key: str | None = None,
        model: str = "jev-latest",
        checkpoint: str = "unknown",
        role: Literal["authoritative", "shadow"] = "authoritative",
        timeout_seconds: float = 10.0,
        max_state_characters: int | None = None,
        state_format: Literal["kv", "json", "values"] = "kv",
        client: Any | None = None,
    ) -> None:
        if client is None and not api_key:
            raise ValueError("an explicit API key is required for an owned TypeSafe client")
        if max_state_characters is not None and max_state_characters <= 0:
            raise ValueError("max_state_characters must be positive")
        if state_format not in {"kv", "json", "values"}:
            raise ValueError("state_format must be kv, json, or values")
        self.provider_id = provider_id
        self.base_url = base_url.rstrip("/")
        self.checkpoint = checkpoint
        self.role = role
        self.timeout_seconds = timeout_seconds
        self.max_state_characters = max_state_characters
        self.state_format = state_format
        self.model = model
        self._api_key = api_key
        self._client = client
        self._owns_client = client is None

    def _make_client(self) -> Any:
        try:
            from httpx2 import AsyncHTTPTransport
            from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy
        except ImportError as error:
            raise ForemanModelError(
                "typesafe-sdk is not installed; install the project dependencies"
            ) from error
        # A supplied transport disables proxy discovery while retaining TLS/CA defaults.
        transport = (
            AsyncHTTPTransport()
            if urlsplit(self.base_url).hostname in {"127.0.0.1", "localhost", "::1"}
            else None
        )
        return AsyncTypeSafeClient(
            transport=transport,
            api_key=self._api_key,
            base_url=self.base_url,
            model=self.model,
            timeout=self.timeout_seconds,
            retry=RetryPolicy(
                max_retries=2,
                http_statuses={429, 500, 502, 503, 504},
                respect_retry_after=True,
                timeout=self.timeout_seconds,
            ),
        )

    async def assess_values(
        self,
        *,
        state: Mapping[str, Any],
        questions: Mapping[str, str],
        question_version: str,
    ) -> tuple[dict[str, float], AssessmentProvenance]:
        try:
            from typesafe_sdk import Noul
        except ImportError as error:
            if self._client is None:
                raise ForemanModelError("typesafe-sdk is not installed") from error

            class Noul:  # type: ignore[no-redef]
                def __init__(self, *, instructions: str) -> None:
                    self.instructions = instructions

        nouls = {name: Noul(instructions=instructions) for name, instructions in questions.items()}
        serialized = dict(state)
        state_characters = len(_serialized_state(serialized, self.state_format))
        if self.max_state_characters is not None and state_characters > self.max_state_characters:
            raise ForemanModelError(
                f"{self.provider_id} state preflight failed: {state_characters} characters "
                f"exceeds provider limit {self.max_state_characters}"
            )
        client = self._client or self._make_client()
        if self._client is None:
            self._client = client
        started = perf_counter()
        try:
            response = await asyncio.wait_for(
                client.system_one(
                    state=serialized,
                    questions=nouls,
                    model=self.model,
                    timeout=self.timeout_seconds,
                ),
                timeout=self.timeout_seconds + 0.5,
            )
            values = parse_jev_scores(response, questions)
            provenance = AssessmentProvenance(
                provider_id=self.provider_id,
                role=self.role,
                implementation="typesafe-sdk",
                endpoint=self.base_url,
                request_model=self.model,
                checkpoint=self.checkpoint,
                question_version=question_version,
                inference=InferenceMetadata(
                    timeout_seconds=self.timeout_seconds,
                    max_retries=2,
                    latency_seconds=perf_counter() - started,
                    state_characters=state_characters,
                    max_state_characters=self.max_state_characters,
                    state_format=self.state_format,
                ),
            )
            return values, provenance
        except TimeoutError as error:
            raise ForemanModelError("Jev assessment timed out") from error
        except ForemanModelError:
            raise
        except Exception as error:
            message = f"Jev assessment failed: {type(error).__name__}: {error}"
            raise ForemanModelError(message) from error

    async def assess(self, observation: FactoryObservation) -> FactoryAssessment:
        values, provenance = await self.assess_values(
            state=observation.model_dump(mode="json"),
            questions=ASSESSMENT_QUESTIONS,
            question_version=ASSESSMENT_QUESTIONS_VERSION,
        )
        return FactoryAssessment(**values, provenance=provenance)

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            close = getattr(self._client, "aclose", None)
            if close is not None:
                await close()
        self._client = None
