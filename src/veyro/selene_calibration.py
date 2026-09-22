"""Forced-choice calibration and quantization regression checks for Selene."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_HASH_PATTERN = r"^[0-9a-f]{64}$"
_ID_PATTERN = r"^[a-z][a-z0-9_.-]{0,127}$"
_EPSILON = 1e-12


class Config(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
        validate_default=True,
    )


class CalibrationMethod(StrEnum):
    TEMPERATURE = "temperature"
    PLATT = "platt"
    ISOTONIC = "isotonic"


class BinaryPrediction(Config):
    case_id: str = Field(pattern=_ID_PATTERN)
    case_sha256: str = Field(pattern=_HASH_PATTERN)
    model_identity: str = Field(min_length=1, max_length=2000)
    yes_logit: float | None = None
    no_logit: float | None = None
    latency_ms: float = Field(ge=0)
    peak_memory_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def paired_logits(self) -> Self:
        if (self.yes_logit is None) != (self.no_logit is None):
            raise ValueError("binary decision logits must both be present or absent")
        if self.yes_logit is not None and not all(
            math.isfinite(value) for value in (self.yes_logit, self.no_logit)
        ):
            raise ValueError("binary decision logits must be finite")
        return self

    @property
    def probability(self) -> float | None:
        if self.yes_logit is None or self.no_logit is None:
            return None
        return _sigmoid(self.yes_logit - self.no_logit)


class BinaryLabel(Config):
    case_id: str = Field(pattern=_ID_PATTERN)
    case_sha256: str = Field(pattern=_HASH_PATTERN)
    expected: Literal["yes", "no"]
    author: str = Field(min_length=1, max_length=500)
    reviewer: str = Field(min_length=1, max_length=500)
    model_generated: Literal[False] = False

    @model_validator(mode="after")
    def independent_label(self) -> Self:
        if self.author == self.reviewer:
            raise ValueError("label author and reviewer must differ")
        return self


class BinaryMetrics(Config):
    cases: int = Field(ge=0)
    score_coverage: float = Field(ge=0, le=1)
    accuracy: float = Field(ge=0, le=1)
    balanced_accuracy: float = Field(ge=0, le=1)
    positive_accuracy: float = Field(ge=0, le=1)
    negative_accuracy: float = Field(ge=0, le=1)
    false_accepts: int = Field(ge=0)
    brier: float = Field(ge=0)
    log_loss: float = Field(ge=0)
    p95_ms: float = Field(ge=0)
    peak_memory_bytes: int = Field(ge=0)


class CalibrationContract(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-calibration-v1"] = "selene-calibration-v1"
    id: str = Field(pattern=_ID_PATTERN)
    created_at: datetime
    implementer: str = Field(min_length=1, max_length=500)
    model_identity: str = Field(min_length=1, max_length=2000)
    predictions_sha256: str = Field(pattern=_HASH_PATTERN)
    labels_sha256: str = Field(pattern=_HASH_PATTERN)
    methods: frozenset[CalibrationMethod] = Field(min_length=1)
    min_raw_balanced_accuracy: float = Field(ge=0, le=1)
    max_raw_false_accepts: int = Field(ge=0)
    require_class_preservation: bool = True
    require_non_worsening_brier: bool = True
    require_non_worsening_log_loss: bool = True

    def sha256(self) -> str:
        return records_sha256([self])


class CalibrationCandidate(Config):
    method: Literal["identity", "temperature", "platt", "isotonic"]
    parameters: dict[str, float | list[float]]
    metrics: BinaryMetrics
    class_predictions_preserved: bool
    eligible: bool
    rejection_reasons: list[str]

    @model_validator(mode="after")
    def valid_method_parameters(self) -> Self:
        _validate_calibration_parameters(self.method, self.parameters)
        if self.eligible == bool(self.rejection_reasons):
            raise ValueError("calibration eligibility must match rejection reasons")
        return self


class CalibrationArtifact(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-calibration-artifact-v1"] = "selene-calibration-artifact-v1"
    contract_sha256: str = Field(pattern=_HASH_PATTERN)
    model_identity: str
    predictions_sha256: str = Field(pattern=_HASH_PATTERN)
    labels_sha256: str = Field(pattern=_HASH_PATTERN)
    raw_metrics: BinaryMetrics
    selected_method: Literal["identity", "temperature", "platt", "isotonic"]
    parameters: dict[str, float | list[float]]
    candidates: list[CalibrationCandidate] = Field(min_length=1)

    @model_validator(mode="after")
    def selected_candidate_is_bound(self) -> Self:
        matches = [item for item in self.candidates if item.method == self.selected_method]
        if len(matches) != 1 or not matches[0].eligible:
            raise ValueError("selected calibration candidate must be uniquely eligible")
        if matches[0].parameters != self.parameters:
            raise ValueError("selected calibration parameters do not match the candidate")
        identities = [item for item in self.candidates if item.method == "identity"]
        if len(identities) != 1 or identities[0].metrics != self.raw_metrics:
            raise ValueError("calibration artifact requires its exact identity baseline")
        return self


class QuantizationContract(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-quantization-comparison-v1"] = "selene-quantization-comparison-v1"
    id: str = Field(pattern=_ID_PATTERN)
    created_at: datetime
    implementer: str = Field(min_length=1, max_length=500)
    bf16_identity: str = Field(min_length=1, max_length=2000)
    quantized_identity: str = Field(min_length=1, max_length=2000)
    bf16_predictions_sha256: str = Field(pattern=_HASH_PATTERN)
    quantized_predictions_sha256: str = Field(pattern=_HASH_PATTERN)
    labels_sha256: str = Field(pattern=_HASH_PATTERN)
    min_score_coverage: float = Field(default=1.0, ge=0, le=1)
    max_brier_increase: float = Field(default=0, ge=0)
    max_log_loss_increase: float = Field(default=0, ge=0)
    max_p95_ms: float = Field(gt=0)
    max_peak_memory_bytes: int = Field(gt=0)

    def sha256(self) -> str:
        return records_sha256([self])


class QuantizationComparison(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-quantization-report-v1"] = "selene-quantization-report-v1"
    contract_sha256: str = Field(pattern=_HASH_PATTERN)
    bf16: BinaryMetrics
    quantized: BinaryMetrics
    passes: bool
    rejection_reasons: list[str]


def records_sha256(values: list[BaseModel]) -> str:
    payload = [item.model_dump(mode="json") for item in values]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def binary_metrics(
    predictions: list[BinaryPrediction],
    labels: list[BinaryLabel],
    *,
    probabilities: dict[str, float] | None = None,
) -> BinaryMetrics:
    indexed_predictions, indexed_labels = _matched(predictions, labels)
    available = {
        case_id: prediction.probability
        for case_id, prediction in indexed_predictions.items()
        if prediction.probability is not None
    }
    if probabilities is not None:
        if set(probabilities) != set(available):
            raise ValueError("calibrated probabilities must match available decision scores")
        available = probabilities
    total = len(labels)
    if not available:
        return BinaryMetrics(
            cases=total,
            score_coverage=0,
            accuracy=0,
            balanced_accuracy=0,
            positive_accuracy=0,
            negative_accuracy=0,
            false_accepts=0,
            brier=0,
            log_loss=0,
            p95_ms=_p95([item.latency_ms for item in predictions]),
            peak_memory_bytes=max((item.peak_memory_bytes for item in predictions), default=0),
        )

    expected = {case_id: label.expected == "yes" for case_id, label in indexed_labels.items()}
    positives = [case_id for case_id in available if expected[case_id]]
    negatives = [case_id for case_id in available if not expected[case_id]]
    if not positives or not negatives:
        raise ValueError("binary metrics require scored positive and negative examples")
    correct = {
        case_id: (probability >= 0.5) == expected[case_id]
        for case_id, probability in available.items()
    }
    positive_accuracy = sum(correct[case_id] for case_id in positives) / len(positives)
    negative_accuracy = sum(correct[case_id] for case_id in negatives) / len(negatives)
    false_accepts = sum(available[case_id] >= 0.5 for case_id in negatives)
    brier = statistics.fmean(
        (probability - float(expected[case_id])) ** 2 for case_id, probability in available.items()
    )
    log_loss = statistics.fmean(
        -math.log(_clip(probability if expected[case_id] else 1 - probability))
        for case_id, probability in available.items()
    )
    return BinaryMetrics(
        cases=total,
        score_coverage=len(available) / total,
        accuracy=sum(correct.values()) / len(correct),
        balanced_accuracy=(positive_accuracy + negative_accuracy) / 2,
        positive_accuracy=positive_accuracy,
        negative_accuracy=negative_accuracy,
        false_accepts=false_accepts,
        brier=brier,
        log_loss=log_loss,
        p95_ms=_p95([item.latency_ms for item in predictions]),
        peak_memory_bytes=max((item.peak_memory_bytes for item in predictions), default=0),
    )


def fit_calibration(
    contract: CalibrationContract,
    predictions: list[BinaryPrediction],
    labels: list[BinaryLabel],
) -> CalibrationArtifact:
    _validate_contract_inputs(
        contract.predictions_sha256,
        contract.labels_sha256,
        predictions,
        labels,
    )
    if any(item.model_identity != contract.model_identity for item in predictions):
        raise ValueError("calibration prediction model identity mismatch")
    if any(contract.implementer in {item.author, item.reviewer} for item in labels):
        raise ValueError("calibration labels are not independent of the implementer")

    raw = binary_metrics(predictions, labels)
    if raw.score_coverage != 1:
        raise ValueError("calibration requires 100% decision-score coverage")
    if raw.balanced_accuracy < contract.min_raw_balanced_accuracy:
        raise ValueError("raw classification has not met the frozen calibration prerequisite")
    if raw.false_accepts > contract.max_raw_false_accepts:
        raise ValueError("raw false accepts exceed the frozen calibration prerequisite")

    indexed_predictions, indexed_labels = _matched(predictions, labels)
    logits = {
        case_id: prediction.yes_logit - prediction.no_logit
        for case_id, prediction in indexed_predictions.items()
        if prediction.yes_logit is not None and prediction.no_logit is not None
    }
    outcomes = {
        case_id: 1.0 if indexed_labels[case_id].expected == "yes" else 0.0 for case_id in logits
    }
    raw_probabilities = {case_id: _sigmoid(value) for case_id, value in logits.items()}
    candidates = [
        _candidate(
            contract,
            "identity",
            {},
            raw_probabilities,
            raw_probabilities,
            predictions,
            labels,
            raw,
        )
    ]
    for method in sorted(contract.methods, key=lambda item: item.value):
        parameters, calibrated = _fit_method(method, logits, outcomes)
        candidates.append(
            _candidate(
                contract,
                method.value,
                parameters,
                raw_probabilities,
                calibrated,
                predictions,
                labels,
                raw,
            )
        )
    eligible = [item for item in candidates if item.eligible]
    selected = min(
        eligible,
        key=lambda item: (item.metrics.log_loss, item.metrics.brier, item.method),
    )
    return CalibrationArtifact(
        contract_sha256=contract.sha256(),
        model_identity=contract.model_identity,
        predictions_sha256=contract.predictions_sha256,
        labels_sha256=contract.labels_sha256,
        raw_metrics=raw,
        selected_method=selected.method,
        parameters=selected.parameters,
        candidates=candidates,
    )


def calibrated_probability(
    artifact: CalibrationArtifact, yes_logit: float, no_logit: float
) -> float:
    difference = yes_logit - no_logit
    if artifact.selected_method == "identity":
        return _sigmoid(difference)
    if artifact.selected_method == "temperature":
        return _sigmoid(difference / float(artifact.parameters["temperature"]))
    if artifact.selected_method == "platt":
        return _sigmoid(
            float(artifact.parameters["slope"]) * difference
            + float(artifact.parameters["intercept"])
        )
    thresholds = artifact.parameters["thresholds"]
    values = artifact.parameters["values"]
    assert isinstance(thresholds, list) and isinstance(values, list)
    raw = _sigmoid(difference)
    for threshold, value in zip(thresholds, values, strict=True):
        if raw <= threshold:
            return value
    return values[-1]


def compare_quantization(
    contract: QuantizationContract,
    bf16: list[BinaryPrediction],
    quantized: list[BinaryPrediction],
    labels: list[BinaryLabel],
) -> QuantizationComparison:
    _validate_contract_inputs(
        contract.bf16_predictions_sha256,
        contract.labels_sha256,
        bf16,
        labels,
    )
    if records_sha256(quantized) != contract.quantized_predictions_sha256:
        raise ValueError("quantized predictions do not match the frozen contract")
    if any(contract.implementer in {item.author, item.reviewer} for item in labels):
        raise ValueError("quantization labels are not independent of the implementer")
    if any(item.model_identity != contract.bf16_identity for item in bf16):
        raise ValueError("BF16 prediction identity mismatch")
    if any(item.model_identity != contract.quantized_identity for item in quantized):
        raise ValueError("quantized prediction identity mismatch")
    if {item.case_id for item in bf16} != {item.case_id for item in quantized}:
        raise ValueError("BF16 and quantized predictions must cover the same cases")

    baseline = binary_metrics(bf16, labels)
    candidate = binary_metrics(quantized, labels)
    reasons = []
    if candidate.score_coverage < contract.min_score_coverage:
        reasons.append("decision_score_coverage")
    if candidate.false_accepts > baseline.false_accepts:
        reasons.append("new_false_accept")
    if candidate.positive_accuracy < baseline.positive_accuracy:
        reasons.append("positive_class_regression")
    if candidate.negative_accuracy < baseline.negative_accuracy:
        reasons.append("negative_class_regression")
    if candidate.brier > baseline.brier + contract.max_brier_increase:
        reasons.append("brier_regression")
    if candidate.log_loss > baseline.log_loss + contract.max_log_loss_increase:
        reasons.append("log_loss_regression")
    if candidate.p95_ms > contract.max_p95_ms:
        reasons.append("latency_limit")
    if candidate.peak_memory_bytes > contract.max_peak_memory_bytes:
        reasons.append("memory_limit")
    return QuantizationComparison(
        contract_sha256=contract.sha256(),
        bf16=baseline,
        quantized=candidate,
        passes=not reasons,
        rejection_reasons=reasons,
    )


def _validate_calibration_parameters(
    method: str, parameters: dict[str, float | list[float]]
) -> None:
    if method == "identity":
        if parameters:
            raise ValueError("identity calibration cannot have parameters")
        return
    if method == "temperature":
        if set(parameters) != {"temperature"}:
            raise ValueError("temperature calibration requires one temperature")
        temperature = parameters["temperature"]
        if (
            type(temperature) not in (int, float)
            or not math.isfinite(temperature)
            or temperature <= 0
        ):
            raise ValueError("calibration temperature must be finite and positive")
        return
    if method == "platt":
        if set(parameters) != {"slope", "intercept"}:
            raise ValueError("Platt calibration requires slope and intercept")
        slope, intercept = parameters["slope"], parameters["intercept"]
        if (
            type(slope) not in (int, float)
            or type(intercept) not in (int, float)
            or not math.isfinite(slope)
            or not math.isfinite(intercept)
            or slope <= 0
        ):
            raise ValueError("Platt slope must be positive and all parameters finite")
        return
    if set(parameters) != {"thresholds", "values"}:
        raise ValueError("isotonic calibration requires thresholds and values")
    thresholds, values = parameters["thresholds"], parameters["values"]
    if (
        not isinstance(thresholds, list)
        or not isinstance(values, list)
        or not thresholds
        or len(thresholds) != len(values)
        or any(type(item) not in (int, float) or not math.isfinite(item) for item in thresholds)
        or any(type(item) not in (int, float) or not 0 <= item <= 1 for item in values)
        or any(left >= right for left, right in zip(thresholds, thresholds[1:], strict=False))
        or any(left > right for left, right in zip(values, values[1:], strict=False))
    ):
        raise ValueError("isotonic parameters must be finite ordered paired lists")


def _candidate(
    contract: CalibrationContract,
    method: str,
    parameters: dict[str, float | list[float]],
    raw_probabilities: dict[str, float],
    calibrated: dict[str, float],
    predictions: list[BinaryPrediction],
    labels: list[BinaryLabel],
    raw: BinaryMetrics,
) -> CalibrationCandidate:
    metrics = binary_metrics(predictions, labels, probabilities=calibrated)
    preserved = all(
        (raw_probabilities[case_id] >= 0.5) == (probability >= 0.5)
        for case_id, probability in calibrated.items()
    )
    reasons = []
    if contract.require_class_preservation and not preserved:
        reasons.append("class_prediction_changed")
    if contract.require_non_worsening_brier and metrics.brier > raw.brier + 1e-15:
        reasons.append("brier_worsened")
    if contract.require_non_worsening_log_loss and metrics.log_loss > raw.log_loss + 1e-15:
        reasons.append("log_loss_worsened")
    return CalibrationCandidate(
        method=method,
        parameters=parameters,
        metrics=metrics,
        class_predictions_preserved=preserved,
        eligible=not reasons,
        rejection_reasons=reasons,
    )


def _fit_method(
    method: CalibrationMethod,
    logits: dict[str, float],
    outcomes: dict[str, float],
) -> tuple[dict[str, float | list[float]], dict[str, float]]:
    if method is CalibrationMethod.TEMPERATURE:
        temperature = _fit_temperature(list(logits.values()), list(outcomes.values()))
        return {"temperature": temperature}, {
            case_id: _sigmoid(value / temperature) for case_id, value in logits.items()
        }
    if method is CalibrationMethod.PLATT:
        slope, intercept = _fit_platt(list(logits.values()), list(outcomes.values()))
        return {"slope": slope, "intercept": intercept}, {
            case_id: _sigmoid(slope * value + intercept) for case_id, value in logits.items()
        }
    raw = {case_id: _sigmoid(value) for case_id, value in logits.items()}
    thresholds, values = _fit_isotonic(raw, outcomes)
    calibrated = {
        case_id: _isotonic_value(probability, thresholds, values)
        for case_id, probability in raw.items()
    }
    return {"thresholds": thresholds, "values": values}, calibrated


def _fit_temperature(logits: list[float], outcomes: list[float]) -> float:
    def loss(log_temperature: float) -> float:
        temperature = math.exp(log_temperature)
        return statistics.fmean(
            -math.log(
                _clip(
                    _sigmoid(logit / temperature) if outcome else 1 - _sigmoid(logit / temperature)
                )
            )
            for logit, outcome in zip(logits, outcomes, strict=True)
        )

    left, right = math.log(0.05), math.log(20.0)
    ratio = (math.sqrt(5) - 1) / 2
    x1 = right - ratio * (right - left)
    x2 = left + ratio * (right - left)
    for _ in range(100):
        if loss(x1) <= loss(x2):
            right, x2 = x2, x1
            x1 = right - ratio * (right - left)
        else:
            left, x1 = x1, x2
            x2 = left + ratio * (right - left)
    return math.exp((left + right) / 2)


def _fit_platt(logits: list[float], outcomes: list[float]) -> tuple[float, float]:
    slope, intercept = 1.0, 0.0
    regularization = 1e-6
    for _ in range(100):
        probabilities = [_sigmoid(slope * value + intercept) for value in logits]
        weights = [probability * (1 - probability) for probability in probabilities]
        gradient_slope = sum(
            (probability - outcome) * value
            for probability, outcome, value in zip(probabilities, outcomes, logits, strict=True)
        ) + regularization * (slope - 1)
        gradient_intercept = (
            sum(
                probability - outcome
                for probability, outcome in zip(probabilities, outcomes, strict=True)
            )
            + regularization * intercept
        )
        hessian_ss = (
            sum(weight * value * value for weight, value in zip(weights, logits, strict=True))
            + regularization
        )
        hessian_si = sum(weight * value for weight, value in zip(weights, logits, strict=True))
        hessian_ii = sum(weights) + regularization
        determinant = hessian_ss * hessian_ii - hessian_si * hessian_si
        if determinant <= 1e-15:
            break
        delta_slope = (hessian_ii * gradient_slope - hessian_si * gradient_intercept) / determinant
        delta_intercept = (
            hessian_ss * gradient_intercept - hessian_si * gradient_slope
        ) / determinant
        next_slope = max(1e-6, slope - delta_slope)
        next_intercept = intercept - delta_intercept
        if abs(next_slope - slope) + abs(next_intercept - intercept) < 1e-10:
            slope, intercept = next_slope, next_intercept
            break
        slope, intercept = next_slope, next_intercept
    return slope, intercept


def _fit_isotonic(
    probabilities: dict[str, float], outcomes: dict[str, float]
) -> tuple[list[float], list[float]]:
    grouped: dict[float, list[float]] = {}
    for case_id, probability in probabilities.items():
        values = grouped.setdefault(probability, [0.0, 0.0])
        values[0] += 1
        values[1] += outcomes[case_id]
    blocks: list[list[float]] = []
    for probability, (count, total) in sorted(grouped.items()):
        blocks.append([probability, probability, count, total])
        while len(blocks) >= 2 and blocks[-2][3] / blocks[-2][2] > blocks[-1][3] / blocks[-1][2]:
            right = blocks.pop()
            left = blocks.pop()
            blocks.append([left[0], right[1], left[2] + right[2], left[3] + right[3]])
    return (
        [block[1] for block in blocks],
        [block[3] / block[2] for block in blocks],
    )


def _isotonic_value(probability: float, thresholds: list[float], values: list[float]) -> float:
    for threshold, value in zip(thresholds, values, strict=True):
        if probability <= threshold:
            return value
    return values[-1]


def _validate_contract_inputs(
    predictions_sha256: str,
    labels_sha256: str,
    predictions: list[BinaryPrediction],
    labels: list[BinaryLabel],
) -> None:
    if records_sha256(predictions) != predictions_sha256:
        raise ValueError("predictions do not match the frozen contract")
    if records_sha256(labels) != labels_sha256:
        raise ValueError("labels do not match the frozen contract")


def _matched(
    predictions: list[BinaryPrediction], labels: list[BinaryLabel]
) -> tuple[dict[str, BinaryPrediction], dict[str, BinaryLabel]]:
    indexed_predictions = {item.case_id: item for item in predictions}
    indexed_labels = {item.case_id: item for item in labels}
    if len(indexed_predictions) != len(predictions) or len(indexed_labels) != len(labels):
        raise ValueError("binary prediction and label IDs must be unique")
    if set(indexed_predictions) != set(indexed_labels):
        raise ValueError("binary predictions and labels must cover the same cases")
    for case_id, prediction in indexed_predictions.items():
        if prediction.case_sha256 != indexed_labels[case_id].case_sha256:
            raise ValueError(f"binary label is bound to a different case: {case_id}")
    return indexed_predictions, indexed_labels


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1 + exponential)


def _clip(value: float) -> float:
    return min(1 - _EPSILON, max(_EPSILON, value))


def _p95(values: list[float]) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[rank]
