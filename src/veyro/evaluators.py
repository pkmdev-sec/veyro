"""Typed evaluator configuration and post-hoc calibration; no inference or network IO.

Concentration is not correctness. Temperature fitting changes output distributions,
not model weights. Reliability still needs independent, workflow-specific labels.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class Config(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False, validate_default=True
    )


class Outcome(Config):
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)


class ScoreOutcome(Outcome):
    value: float


class NoulQuestion(Config):
    type: Literal["noul"] = "noul"
    prompt: str = Field(min_length=1)


class ChoiceQuestion(Config):
    type: Literal["choice"] = "choice"
    prompt: str = Field(min_length=1)
    outcomes: list[Outcome] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_labels(self) -> Self:
        _unique([outcome.label for outcome in self.outcomes], "outcome labels")
        return self


class ScoreQuestion(Config):
    type: Literal["score"] = "score"
    prompt: str = Field(min_length=1)
    outcomes: list[ScoreOutcome] = Field(min_length=2)

    @model_validator(mode="after")
    def ordered_outcomes(self) -> Self:
        _unique([outcome.label for outcome in self.outcomes], "outcome labels")
        if any(a.value >= b.value for a, b in zip(self.outcomes, self.outcomes[1:], strict=False)):
            raise ValueError("score outcomes must have strictly increasing values")
        return self


Question = Annotated[NoulQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="type")]


def _unique(values: Sequence[str], name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must be unique")


def outcome_labels(question: Question) -> list[str]:
    return (
        ["no", "yes"]
        if isinstance(question, NoulQuestion)
        else [outcome.label for outcome in question.outcomes]
    )


def normalize_distribution(
    values: Mapping[str, float], *, logits: bool = False
) -> dict[str, float]:
    """Normalize nonnegative weights, or softmax finite logits, preserving label order."""
    if not values or any(not isinstance(key, str) or not key for key in values):
        raise ValueError("distribution needs named outcomes")
    if any(isinstance(value, bool) or not math.isfinite(value) for value in values.values()):
        raise ValueError("distribution values must be finite numbers")
    numbers = list(values.values())
    if logits:
        maximum = max(numbers)
        weights = [math.exp(value - maximum) for value in numbers]
    else:
        if min(numbers) < 0 or max(numbers) <= 0:
            raise ValueError("distribution weights must be nonnegative with positive mass")
        # Scaling first avoids overflow when several finite weights are near float max.
        maximum = max(numbers)
        weights = [value / maximum for value in numbers]
    total = math.fsum(weights)
    return dict(zip(values, (value / total for value in weights), strict=True))


def choice_confidence(probabilities: Mapping[str, float]) -> float:
    """Source-reported Choice concentration, NOT an estimate of answer correctness."""
    probabilities = _probabilities(probabilities)
    count = len(probabilities)
    if count == 1:
        return 1.0
    return max(0.0, min(1.0, (max(probabilities.values()) - 1 / count) / (1 - 1 / count)))


def _probabilities(values: Mapping[str, float]) -> dict[str, float]:
    normalize_distribution(values)
    if any(value > 1 for value in values.values()) or not math.isclose(
        math.fsum(values.values()), 1.0, rel_tol=1e-9, abs_tol=1e-9
    ):
        raise ValueError("probabilities must be in [0, 1] and sum to one")
    return dict(values)


class QuestionResult(Config):
    probabilities: dict[str, float]
    label: str
    top_probability: float = Field(ge=0, le=1)
    value: float | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    confidence_semantics: Literal["choice_concentration", "not_available"] = "not_available"


def normalize_result(
    question: Question, values: Mapping[str, float], *, logits: bool = False
) -> QuestionResult:
    labels = outcome_labels(question)
    if set(values) != set(labels):
        raise ValueError("result labels must exactly match the question outcomes")
    probabilities = normalize_distribution(
        {label: values[label] for label in labels}, logits=logits
    )
    label = max(probabilities, key=probabilities.__getitem__)
    value = None
    confidence = None
    semantics: Literal["choice_concentration", "not_available"] = "not_available"
    if isinstance(question, NoulQuestion):
        value = probabilities["yes"]
    elif isinstance(question, ScoreQuestion):
        value = math.fsum(
            probabilities[outcome.label] * outcome.value for outcome in question.outcomes
        )
    else:
        confidence = choice_confidence(probabilities)
        semantics = "choice_concentration"
    # The source does not reproduce Score's modal-distance confidence formula.
    return QuestionResult(
        probabilities=probabilities,
        label=label,
        top_probability=probabilities[label],
        value=value,
        confidence=confidence,
        confidence_semantics=semantics,
    )


class EvaluationCase(Config):
    id: str = Field(min_length=1)
    input: JsonValue
    output: JsonValue
    reference: JsonValue = None


class VariableMapping(Config):
    source: Literal["input", "output", "reference"]
    path: list[str | int] = Field(default_factory=list)

    def resolve(self, case: EvaluationCase) -> JsonValue:
        value = getattr(case, self.source)
        for part in self.path:
            if isinstance(value, dict) and isinstance(part, str) and part in value:
                value = value[part]
            elif isinstance(value, list) and type(part) is int and 0 <= part < len(value):
                value = value[part]
            else:
                raise ValueError(f"missing JSON path {self.source}:{self.path!r}")
        return value


_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}")


class EvaluatorDefinition(Config):
    version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    variables: dict[str, VariableMapping]
    questions: dict[str, Question] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_template(self) -> Self:
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) for name in self.variables):
            raise ValueError("variable names must be simple identifiers")
        if any(not name.strip() for name in self.questions):
            raise ValueError("question identifiers must be nonempty")
        unknown = set(_PLACEHOLDER.findall(self.prompt)) - set(self.variables)
        if unknown:
            raise ValueError(f"unmapped prompt variables: {sorted(unknown)}")
        remainder = _PLACEHOLDER.sub("", self.prompt)
        if "{{" in remainder or "}}" in remainder:
            raise ValueError("only literal {{variable}} placeholders are supported")
        return self

    def preview(self, case: EvaluationCase) -> str:
        values = {name: mapping.resolve(case) for name, mapping in self.variables.items()}
        return _PLACEHOLDER.sub(lambda match: _display(values[match.group(1)]), self.prompt)

    def schema_sha256(self) -> str:
        return _hash(self.model_dump(mode="json"))

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.model_validate_json(path.read_text())

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2) + "\n")


def _display(value: JsonValue) -> str:
    return (
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
    )


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


class CorrectionRecord(Config):
    case: EvaluationCase
    labels: dict[str, str] = Field(min_length=1)

    def validate_for(self, definition: EvaluatorDefinition) -> None:
        if set(self.labels) != set(definition.questions):
            raise ValueError("correction labels must cover exactly the evaluator questions")
        for name, label in self.labels.items():
            if label not in outcome_labels(definition.questions[name]):
                raise ValueError(f"invalid corrected outcome for {name}")


def load_corrections(path: Path, definition: EvaluatorDefinition) -> list[CorrectionRecord]:
    records = [
        CorrectionRecord.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    _validate_corrections(records, definition)
    return records


def save_corrections(
    path: Path, records: Sequence[CorrectionRecord], definition: EvaluatorDefinition
) -> None:
    _validate_corrections(records, definition)
    path.write_text("".join(record.model_dump_json() + "\n" for record in records))


def _validate_corrections(
    records: Sequence[CorrectionRecord], definition: EvaluatorDefinition
) -> None:
    _unique([record.case.id for record in records], "correction IDs")
    for record in records:
        record.validate_for(definition)


def few_shot_preview(
    definition: EvaluatorDefinition,
    records: Sequence[CorrectionRecord],
    *,
    holdout_ids: Sequence[str],
    max_examples: int = 4,
    max_chars: int = 12_000,
) -> str:
    """Render whole corrected examples only; reject holdout contamination before truncation."""
    if max_examples < 0 or max_chars < 2:
        raise ValueError("few-shot bounds must be nonnegative examples and at least two characters")
    _validate_corrections(records, definition)
    if set(holdout_ids) & {record.case.id for record in records}:
        raise ValueError("holdout leakage: correction IDs overlap holdout IDs")
    examples = []
    for record in records[:max_examples]:
        example = {
            "id": record.case.id,
            "state": definition.preview(record.case),
            "corrected_labels": record.labels,
        }
        candidate = json.dumps([*examples, example], ensure_ascii=False)
        if len(candidate) > max_chars:
            break
        examples.append(example)
    return json.dumps(examples, ensure_ascii=False)


def build_evaluation_request(
    definition: EvaluatorDefinition,
    case: EvaluationCase,
    *,
    corrections: Sequence[CorrectionRecord] = (),
    holdout_ids: Sequence[str] = (),
    max_examples: int = 4,
    max_chars: int = 12_000,
) -> dict:
    """Keep expected labels out of the live case; only disjoint corrections carry labels.

    Explicit reference mappings remain allowed for reference-based evaluation. Callers
    must not hide benchmark labels inside case input/output/reference or evaluator text.
    """
    examples = few_shot_preview(
        definition,
        corrections,
        holdout_ids=[*holdout_ids, case.id],
        max_examples=max_examples,
        max_chars=max_chars,
    )
    return {
        "state": {"case": definition.preview(case), "few_shot_examples": json.loads(examples)},
        "questions": {
            name: question.model_dump(mode="json")
            for name, question in definition.questions.items()
        },
    }


class CalibrationExample(Config):
    id: str = Field(min_length=1)
    probabilities: dict[str, float]
    label: str

    @model_validator(mode="after")
    def valid_distribution(self) -> Self:
        self.probabilities = _probabilities(self.probabilities)
        if self.label not in self.probabilities:
            raise ValueError("calibration label must be an outcome")
        return self


def _scaled(probabilities: Mapping[str, float], inverse_temperature: float) -> dict[str, float]:
    if inverse_temperature == 1:
        return dict(probabilities)
    return normalize_distribution(
        {
            label: inverse_temperature * math.log(max(probability, 1e-15))
            for label, probability in probabilities.items()
        },
        logits=True,
    )


def _loss(examples: Sequence[CalibrationExample], inverse_temperature: float) -> float:
    losses = []
    for example in examples:
        logits = {
            label: inverse_temperature * math.log(max(probability, 1e-15))
            for label, probability in example.probabilities.items()
        }
        maximum = max(logits.values())
        log_partition = math.log(math.fsum(math.exp(value - maximum) for value in logits.values()))
        losses.append(log_partition + maximum - logits[example.label])
    return math.fsum(losses) / len(losses)


class DistributionCalibrator(Config):
    """Scalar temperature fitted by multiclass log loss, with auditable split provenance.

    Zero probabilities are floored at 1e-15 before taking logs. The identity transform
    preserves zeros. A fitted artifact is not proof of calibration on unseen data.
    """

    version: Literal[1] = 1
    method: Literal["temperature_scaling"] = "temperature_scaling"
    objective: Literal["log_loss"] = "log_loss"
    inverse_temperature: float = Field(default=1, ge=0, le=20)
    outcomes: list[str] = Field(min_length=1)
    model: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    training_ids: list[str] = Field(min_length=1)
    holdout_ids: list[str] = Field(min_length=1)
    training_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    training_log_loss_before: float = Field(ge=0)
    training_log_loss_after: float = Field(ge=0)

    @model_validator(mode="after")
    def disjoint_split(self) -> Self:
        for name in ("outcomes", "training_ids", "holdout_ids"):
            values = getattr(self, name)
            _unique(values, name)
            if any(not value for value in values):
                raise ValueError(f"{name} must not contain empty identifiers")
        if set(self.training_ids) & set(self.holdout_ids):
            raise ValueError("holdout leakage: training and holdout IDs overlap")
        return self

    @classmethod
    def fit(
        cls,
        examples: Sequence[CalibrationExample],
        *,
        holdout_ids: Sequence[str],
        model: str,
        profile: str,
        schema_sha256: str,
    ) -> Self:
        if not examples:
            raise ValueError("calibration requires training examples")
        ids = [example.id for example in examples]
        _unique(ids, "training IDs")
        if not holdout_ids or set(ids) & set(holdout_ids):
            raise ValueError("holdout leakage: require nonempty disjoint holdout IDs")
        outcomes = list(examples[0].probabilities)
        if any(set(example.probabilities) != set(outcomes) for example in examples):
            raise ValueError("training distributions must use identical outcome labels")
        # Log loss is convex in inverse temperature; bounded golden-section search
        # needs no optimizer dependency. Bounds are local policy, not source claims.
        lower, upper = 0.0, 20.0
        ratio = (math.sqrt(5) - 1) / 2
        left, right = upper * (1 - ratio), upper * ratio
        left_loss, right_loss = _loss(examples, left), _loss(examples, right)
        for _ in range(80):
            if left_loss <= right_loss:
                upper, right, right_loss = right, left, left_loss
                left = upper - ratio * (upper - lower)
                left_loss = _loss(examples, left)
            else:
                lower, left, left_loss = left, right, right_loss
                right = lower + ratio * (upper - lower)
                right_loss = _loss(examples, right)
        before = _loss(examples, 1)
        candidates = [1.0, 0.0, 20.0, (lower + upper) / 2]
        best = min(candidates, key=lambda beta: _loss(examples, beta))
        after = _loss(examples, best)
        if before - after < 1e-12:
            best, after = 1.0, before
        return cls(
            inverse_temperature=best,
            outcomes=outcomes,
            model=model,
            profile=profile,
            schema_sha256=schema_sha256,
            training_ids=ids,
            holdout_ids=list(holdout_ids),
            training_sha256=_hash([example.model_dump(mode="json") for example in examples]),
            training_log_loss_before=before,
            training_log_loss_after=after,
        )

    def apply(
        self, probabilities: Mapping[str, float], *, model: str, profile: str, schema_sha256: str
    ) -> dict[str, float]:
        if (model, profile, schema_sha256) != (self.model, self.profile, self.schema_sha256):
            raise ValueError("calibration provenance does not match model/profile/schema")
        probabilities = _probabilities(probabilities)
        if set(probabilities) != set(self.outcomes):
            raise ValueError("calibration outcome labels do not match")
        return _scaled(probabilities, self.inverse_temperature)

    def evaluate_holdout(self, examples: Sequence[CalibrationExample]) -> dict[str, float | int]:
        ids = [example.id for example in examples]
        _unique(ids, "holdout evaluation IDs")
        if not ids or set(ids) & set(self.training_ids):
            raise ValueError("holdout leakage: require nonempty independent evaluation examples")
        if not set(ids) <= set(self.holdout_ids):
            raise ValueError("evaluation IDs were not reserved for holdout")
        if any(set(example.probabilities) != set(self.outcomes) for example in examples):
            raise ValueError("holdout outcome labels do not match")
        distributions = [
            _scaled(example.probabilities, self.inverse_temperature) for example in examples
        ]
        return {
            "count": len(examples),
            "log_loss_before": _loss(examples, 1),
            "log_loss_after": _loss(examples, self.inverse_temperature),
            "brier_before": _brier(examples, [example.probabilities for example in examples]),
            "brier_after": _brier(examples, distributions),
        }

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.model_validate_json(path.read_text())


def _brier(examples: Sequence[CalibrationExample], distributions: list[dict[str, float]]) -> float:
    return math.fsum(
        math.fsum(
            (probability - (label == example.label)) ** 2
            for label, probability in distribution.items()
        )
        for example, distribution in zip(examples, distributions, strict=True)
    ) / len(examples)


class DecisionHeadExample(Config):
    """One reviewed binary outcome with frozen evaluator features."""

    id: str = Field(min_length=1)
    features: dict[str, Annotated[float, Field(ge=0, le=1)]] = Field(min_length=1)
    label: Literal["no", "yes"]

    @model_validator(mode="after")
    def named_features(self) -> Self:
        if any(not name for name in self.features):
            raise ValueError("decision-head features must have non-empty names")
        return self


class DecisionHeadMetrics(Config):
    count: int = Field(gt=0)
    accuracy: float = Field(ge=0, le=1)
    positive_accuracy: float = Field(ge=0, le=1)
    negative_accuracy: float = Field(ge=0, le=1)
    brier: float = Field(ge=0)
    log_loss: float = Field(ge=0)


class BinaryDecisionHead(Config):
    """Auditable post-hoc logistic head over frozen evaluator probabilities.

    This fits a small decision function, not model weights. Reliability still requires an
    independent reserved holdout. Features are logit-transformed and standardized with the
    recorded training statistics before the linear decision is applied.
    """

    version: Literal[1] = 1
    method: Literal["l2_logistic_regression"] = "l2_logistic_regression"
    transform: Literal["logit"] = "logit"
    feature_names: list[str] = Field(min_length=1)
    feature_means: list[float]
    feature_scales: list[Annotated[float, Field(gt=0)]]
    weights: list[float]
    intercept: float
    regularization: float = Field(gt=0)
    model: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    training_ids: list[str] = Field(min_length=2)
    holdout_ids: list[str] = Field(min_length=1)
    training_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    training_metrics: DecisionHeadMetrics

    @model_validator(mode="after")
    def valid_shape_and_split(self) -> Self:
        _unique(self.feature_names, "decision-head feature names")
        _unique(self.training_ids, "decision-head training IDs")
        _unique(self.holdout_ids, "decision-head holdout IDs")
        if any(not value for value in (*self.feature_names, *self.training_ids, *self.holdout_ids)):
            raise ValueError("decision-head names and IDs must not be empty")
        width = len(self.feature_names)
        if not all(
            len(values) == width
            for values in (self.feature_means, self.feature_scales, self.weights)
        ):
            raise ValueError("decision-head parameter shape does not match feature names")
        if set(self.training_ids) & set(self.holdout_ids):
            raise ValueError("holdout leakage: decision-head training and holdout IDs overlap")
        return self

    @classmethod
    def fit(
        cls,
        examples: Sequence[DecisionHeadExample],
        *,
        feature_names: Sequence[str],
        holdout_ids: Sequence[str],
        model: str,
        profile: str,
        schema_sha256: str,
        regularization: float = 1.0,
    ) -> Self:
        if len(examples) < 2:
            raise ValueError("decision-head fitting requires at least two examples")
        names = list(feature_names)
        if not names:
            raise ValueError("decision-head fitting requires selected features")
        _unique(names, "decision-head feature names")
        ids = [example.id for example in examples]
        _unique(ids, "decision-head training IDs")
        reserved = list(holdout_ids)
        _unique(reserved, "decision-head holdout IDs")
        if not reserved or set(ids) & set(reserved):
            raise ValueError("holdout leakage: require nonempty disjoint decision-head holdout IDs")
        if (
            isinstance(regularization, bool)
            or not isinstance(regularization, (int, float))
            or not math.isfinite(regularization)
            or regularization <= 0
        ):
            raise ValueError("decision-head regularization must be finite and positive")
        if any(set(example.features) != set(names) for example in examples):
            raise ValueError("decision-head training features must exactly match selected features")
        outcomes = [example.label == "yes" for example in examples]
        if all(outcomes) or not any(outcomes):
            raise ValueError("decision-head fitting requires both outcome classes")

        transformed = [
            [_probability_logit(example.features[name]) for name in names] for example in examples
        ]
        means = [
            math.fsum(row[index] for row in transformed) / len(transformed)
            for index in range(len(names))
        ]
        scales = []
        for index, mean in enumerate(means):
            variance = math.fsum((row[index] - mean) ** 2 for row in transformed) / len(transformed)
            scales.append(math.sqrt(variance) or 1.0)
        rows = [
            [
                1.0,
                *(
                    (value - mean) / scale
                    for value, mean, scale in zip(row, means, scales, strict=True)
                ),
            ]
            for row in transformed
        ]
        weights = _fit_logistic(rows, outcomes, float(regularization))
        probabilities = [_sigmoid(_dot(row, weights)) for row in rows]
        metrics = _decision_metrics(examples, probabilities)
        return cls(
            feature_names=names,
            feature_means=means,
            feature_scales=scales,
            weights=weights[1:],
            intercept=weights[0],
            regularization=float(regularization),
            model=model,
            profile=profile,
            schema_sha256=schema_sha256,
            training_ids=ids,
            holdout_ids=reserved,
            training_sha256=_hash(
                {
                    "examples": [example.model_dump(mode="json") for example in examples],
                    "feature_names": names,
                    "regularization": float(regularization),
                }
            ),
            training_metrics=metrics,
        )

    def predict(
        self,
        features: Mapping[str, float],
        *,
        model: str,
        profile: str,
        schema_sha256: str,
    ) -> float:
        if (model, profile, schema_sha256) != (self.model, self.profile, self.schema_sha256):
            raise ValueError("decision-head provenance does not match model/profile/schema")
        if set(features) != set(self.feature_names):
            raise ValueError("decision-head feature names do not match")
        values = []
        for name in self.feature_names:
            value = features[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError("decision-head features must be finite probabilities")
            values.append(float(value))
        standardized = [
            (_probability_logit(value) - mean) / scale
            for value, mean, scale in zip(
                values, self.feature_means, self.feature_scales, strict=True
            )
        ]
        return _sigmoid(self.intercept + _dot(standardized, self.weights))

    def evaluate_holdout(self, examples: Sequence[DecisionHeadExample]) -> DecisionHeadMetrics:
        ids = [example.id for example in examples]
        _unique(ids, "decision-head holdout evaluation IDs")
        if not ids or set(ids) & set(self.training_ids):
            raise ValueError("holdout leakage: require independent decision-head examples")
        if not set(ids) <= set(self.holdout_ids):
            raise ValueError("decision-head evaluation IDs were not reserved for holdout")
        probabilities = [
            self.predict(
                example.features,
                model=self.model,
                profile=self.profile,
                schema_sha256=self.schema_sha256,
            )
            for example in examples
        ]
        return _decision_metrics(examples, probabilities)

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.model_validate_json(path.read_text())


def _probability_logit(probability: float) -> float:
    clipped = min(1 - 1e-12, max(1e-12, probability))
    return math.log(clipped / (1 - clipped))


def _sigmoid(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-min(value, 40))
        return 1 / (1 + inverse)
    exponent = math.exp(max(value, -40))
    return exponent / (1 + exponent)


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return math.fsum(a * b for a, b in zip(left, right, strict=True))


def _fit_logistic(
    rows: list[list[float]], outcomes: list[bool], regularization: float
) -> list[float]:
    width = len(rows[0])
    positive = sum(outcomes)
    weights = [math.log(positive / (len(outcomes) - positive)), *([0.0] * (width - 1))]
    for _ in range(100):
        probabilities = [_sigmoid(_dot(row, weights)) for row in rows]
        gradient = [
            math.fsum(
                (probability - outcome) * row[index]
                for row, outcome, probability in zip(rows, outcomes, probabilities, strict=True)
            )
            + (regularization * weights[index] if index else 0.0)
            for index in range(width)
        ]
        hessian = [
            [
                math.fsum(
                    probability * (1 - probability) * row[left] * row[right]
                    for row, probability in zip(rows, probabilities, strict=True)
                )
                + (regularization if left == right and left else 0.0)
                + (1e-12 if left == right else 0.0)
                for right in range(width)
            ]
            for left in range(width)
        ]
        delta = _solve_linear(hessian, gradient)
        baseline = _logistic_objective(rows, outcomes, weights, regularization)
        step = 1.0
        while step > 1e-8:
            candidate = [
                value - step * change for value, change in zip(weights, delta, strict=True)
            ]
            if _logistic_objective(rows, outcomes, candidate, regularization) <= baseline:
                break
            step /= 2
        candidate = [value - step * change for value, change in zip(weights, delta, strict=True)]
        if max(abs(a - b) for a, b in zip(candidate, weights, strict=True)) < 1e-10:
            return candidate
        weights = candidate
    return weights


def _logistic_objective(
    rows: Sequence[Sequence[float]],
    outcomes: Sequence[bool],
    weights: Sequence[float],
    regularization: float,
) -> float:
    loss = math.fsum(
        -math.log(max(1e-15, probability if outcome else 1 - probability))
        for row, outcome in zip(rows, outcomes, strict=True)
        for probability in [_sigmoid(_dot(row, weights))]
    )
    return loss + regularization * math.fsum(value * value for value in weights[1:]) / 2


def _solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float]:
    augmented = [row[:] + [value] for row, value in zip(matrix, vector, strict=True)]
    width = len(vector)
    for column in range(width):
        pivot = max(range(column, width), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-15:
            raise ValueError("decision-head fit is singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(width):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * base
                for value, base in zip(augmented[row], augmented[column], strict=True)
            ]
    return [augmented[index][-1] for index in range(width)]


def _decision_metrics(
    examples: Sequence[DecisionHeadExample], probabilities: Sequence[float]
) -> DecisionHeadMetrics:
    positives = [index for index, example in enumerate(examples) if example.label == "yes"]
    negatives = [index for index, example in enumerate(examples) if example.label == "no"]
    if not positives or not negatives:
        raise ValueError("decision-head metrics require both outcome classes")
    outcomes = [example.label == "yes" for example in examples]
    correct = [
        (probability >= 0.5) == outcome
        for probability, outcome in zip(probabilities, outcomes, strict=True)
    ]
    return DecisionHeadMetrics(
        count=len(examples),
        accuracy=sum(correct) / len(correct),
        positive_accuracy=sum(correct[index] for index in positives) / len(positives),
        negative_accuracy=sum(correct[index] for index in negatives) / len(negatives),
        brier=math.fsum(
            (probability - outcome) ** 2
            for probability, outcome in zip(probabilities, outcomes, strict=True)
        )
        / len(outcomes),
        log_loss=math.fsum(
            -math.log(max(1e-15, probability if outcome else 1 - probability))
            for probability, outcome in zip(probabilities, outcomes, strict=True)
        )
        / len(outcomes),
    )
