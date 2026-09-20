"""Run reusable typed evaluators against the persistent local numeric readout."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from veyro.evaluators import (
    CorrectionRecord,
    DistributionCalibrator,
    EvaluationCase,
    EvaluatorDefinition,
    NoulQuestion,
    ScoreQuestion,
    build_evaluation_request,
    few_shot_preview,
    normalize_result,
    outcome_labels,
)
from veyro.local_models import ModelProfile, load_profiles
from veyro.readout import READOUT_PROTOCOL


class _Prediction(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    outcomes: list[str]
    probabilities: list[Annotated[float, Field(strict=True, ge=0, le=1)]]


class _Response(BaseModel):
    protocol: str
    predictions: dict[str, _Prediction]
    metrics: dict[str, JsonValue]


def model_identity(profile: ModelProfile) -> str:
    return f"{profile.ollama_model}@sha256:{profile.expected_digest}"


def calibration_schema(
    definition: EvaluatorDefinition,
    question: str,
    corrections: Sequence[CorrectionRecord] = (),
) -> str:
    value = {
        "protocol": READOUT_PROTOCOL,
        "evaluator": definition.model_dump(mode="json"),
        "question": question,
        "few_shot_examples": json.loads(few_shot_preview(definition, corrections, holdout_ids=())),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def wire_questions(definition: EvaluatorDefinition) -> dict:
    return {
        name: {
            "text": question.prompt,
            "outcomes": (
                [
                    {"name": "no", "description": "The criterion is not fully established."},
                    {"name": "yes", "description": "The entire criterion is established."},
                ]
                if isinstance(question, NoulQuestion)
                else [
                    {"name": outcome.label, "description": outcome.description}
                    for outcome in question.outcomes
                ]
            ),
        }
        for name, question in definition.questions.items()
        if len(outcome_labels(question)) > 1
    }


def run_evaluator(
    definition: EvaluatorDefinition,
    case: EvaluationCase,
    profile_id: str,
    *,
    corrections: Sequence[CorrectionRecord] = (),
    holdout_ids: Sequence[str] = (),
    calibrations: dict[str, DistributionCalibrator] | None = None,
    state_dir: Path | None = None,
    timeout: float = 60,
) -> dict:
    from veyro.local_server import evaluate_local

    profiles = load_profiles()
    if profile_id not in profiles:
        raise ValueError(f"unknown local model profile: {profile_id}")
    profile = profiles[profile_id]
    identity = model_identity(profile)
    calibrations = calibrations or {}
    if set(calibrations) - set(definition.questions):
        raise ValueError("calibration names must belong to the evaluator")
    for name, calibrator in calibrations.items():
        labels = outcome_labels(definition.questions[name])
        calibrator.apply(
            dict.fromkeys(labels, 1 / len(labels)),
            model=identity,
            profile=profile_id,
            schema_sha256=calibration_schema(definition, name, corrections),
        )
    request = build_evaluation_request(
        definition,
        case,
        corrections=corrections,
        holdout_ids=[
            *holdout_ids,
            *(item for cal in calibrations.values() for item in cal.holdout_ids),
        ],
    )
    questions = wire_questions(definition)
    response = (
        evaluate_local(
            profile_id, request["state"], questions, timeout=timeout, state_dir=state_dir
        )
        if questions
        else {"protocol": READOUT_PROTOCOL, "predictions": {}, "metrics": {}}
    )
    try:
        response = _Response.model_validate(response)
    except ValidationError:
        raise ValueError(
            "local readout returned invalid outcome distribution or malformed response"
        ) from None
    if response.protocol != READOUT_PROTOCOL or set(response.predictions) != set(questions):
        raise ValueError("local readout protocol or prediction keys do not match")
    results = {}
    for name, question in definition.questions.items():
        labels = outcome_labels(question)
        source = "single_outcome" if len(labels) == 1 else "uncalibrated_label_logits"
        if len(labels) == 1:
            probabilities = {labels[0]: 1.0}
        else:
            prediction = response.predictions[name]
            values = prediction.probabilities
            if (
                prediction.outcomes != labels
                or len(values) != len(labels)
                or any(
                    type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                    for v in values
                )
                or not math.isclose(math.fsum(values), 1, abs_tol=1e-8)
            ):
                raise ValueError("local readout returned an invalid outcome distribution")
            probabilities = dict(zip(labels, values, strict=True))
        if name in calibrations:
            probabilities = calibrations[name].apply(
                probabilities,
                model=identity,
                profile=profile_id,
                schema_sha256=calibration_schema(definition, name, corrections),
            )
            source = "posthoc_temperature_scaling"
        result = normalize_result(question, probabilities)
        if isinstance(question, NoulQuestion):
            feedback = {"type": "boolean", "value": result.label == "yes"}
        elif isinstance(question, ScoreQuestion):
            feedback = {
                "type": "continuous",
                "value": result.value,
                "min": question.outcomes[0].value,
                "max": question.outcomes[-1].value,
            }
        else:
            feedback = {"type": "categorical", "value": result.label}
        results[name] = {
            **result.model_dump(mode="json"),
            "feedback": feedback,
            "source": source,
            "calibration_schema_sha256": calibration_schema(definition, name, corrections),
        }
    return {
        "protocol": READOUT_PROTOCOL,
        "evaluator": definition.name,
        "version": definition.version,
        "case_id": case.id,
        "profile": profile_id,
        "model": identity,
        "calibrated": all(name in calibrations for name in questions),
        "results": results,
        "metrics": response.metrics,
    }
