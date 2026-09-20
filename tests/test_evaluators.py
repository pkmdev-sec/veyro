"""Offline contracts for dynamic evaluators, correction examples and calibration."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from veyro.evaluators import (
    CalibrationExample,
    ChoiceQuestion,
    CorrectionRecord,
    DistributionCalibrator,
    EvaluationCase,
    EvaluatorDefinition,
    NoulQuestion,
    Outcome,
    ScoreOutcome,
    ScoreQuestion,
    VariableMapping,
    build_evaluation_request,
    choice_confidence,
    few_shot_preview,
    load_corrections,
    normalize_distribution,
    normalize_result,
    outcome_labels,
    save_corrections,
)


@pytest.fixture
def definition():
    return EvaluatorDefinition.load(
        Path(__file__).parents[1] / "examples/evaluator-definition.json"
    )


@pytest.fixture
def case():
    return EvaluationCase(
        id="case-1", input={"task": "Add 2 + 2"}, output={"answer": "4"}, reference="4"
    )


def correction(case, identifier="train-1"):
    return CorrectionRecord(
        case=case.model_copy(update={"id": identifier}),
        labels={"correct": "yes", "route": "accept", "quality": "complete"},
    )


def training_examples(probability=0.9):
    return [
        CalibrationExample(
            id=f"train-{index}",
            probabilities={"no": 1 - probability, "yes": probability},
            label="yes" if index < 8 else "no",
        )
        for index in range(10)
    ]


def fit(examples=None):
    return DistributionCalibrator.fit(
        training_examples() if examples is None else examples,
        holdout_ids=[f"holdout-{index}" for index in range(10)],
        model="qwen2.5:7b@sha256:fixture",
        profile="small",
        schema_sha256="a" * 64,
    )


def apply(calibrator, probabilities):
    return calibrator.apply(
        probabilities,
        model=calibrator.model,
        profile=calibrator.profile,
        schema_sha256=calibrator.schema_sha256,
    )


def test_shipped_definition_roundtrip_and_typed_questions(definition, tmp_path):
    assert isinstance(definition.questions["correct"], NoulQuestion)
    assert isinstance(definition.questions["route"], ChoiceQuestion)
    assert isinstance(definition.questions["quality"], ScoreQuestion)
    path = tmp_path / "definition.json"
    definition.save(path)
    restored = EvaluatorDefinition.load(path)
    assert restored == definition
    assert restored.schema_sha256() == definition.schema_sha256()
    restored.questions["correct"].prompt += " Changed rubric."
    assert restored.schema_sha256() != definition.schema_sha256()


@pytest.mark.parametrize(
    "override",
    [
        {"questions": {}},
        {"questions": {"q": {"type": "free_text", "prompt": "hi"}}},
        {"prompt": "{{unknown}}"},
        {"prompt": "{{task.__class__}}"},
        {"prompt": "{{task()}}"},
        {"variables": {"bad.name": {"source": "input"}}},
        {"variables": {"task": {"source": "environment"}}},
        {"unknown": True},
        {"version": ""},
    ],
)
def test_invalid_definition_schema(definition, override):
    data = definition.model_dump(mode="json") | override
    with pytest.raises(ValidationError):
        EvaluatorDefinition.model_validate(data)


@pytest.mark.parametrize(
    "outcomes",
    [
        [],
        [
            {"label": "same", "description": "a"},
            {"label": "same", "description": "b"},
        ],
    ],
)
def test_choice_rejects_empty_or_duplicate_outcomes(outcomes):
    with pytest.raises(ValidationError):
        ChoiceQuestion(prompt="Select", outcomes=outcomes)


@pytest.mark.parametrize("values", [[0], [1, 1], [2, 1], [0, math.inf], [0, math.nan]])
def test_score_requires_finite_strictly_increasing_outcomes(values):
    with pytest.raises(ValidationError):
        ScoreQuestion(
            prompt="Rate",
            outcomes=[
                {"label": str(index), "description": "Level", "value": value}
                for index, value in enumerate(values)
            ],
        )


def test_preview_maps_json_data_without_evaluating_it(definition, case):
    assert definition.preview(case) == "Task: Add 2 + 2\nAnswer: 4\nReference: 4"
    case.output = {"answer": "{{task}} {task.__class__} ${shell}"}
    assert "Answer: {{task}} {task.__class__} ${shell}" in definition.preview(case)
    case.input = {"tasks": [{"name": "nested"}]}
    assert VariableMapping(source="input", path=["tasks", 0, "name"]).resolve(case) == "nested"
    assert VariableMapping(source="output").resolve(case) == case.output
    case.reference = None
    assert VariableMapping(source="reference").resolve(case) is None


@pytest.mark.parametrize("path", [["missing"], ["__class__"], ["items", -1], ["items", 4]])
def test_mapping_rejects_missing_keys_attributes_and_invalid_indices(path):
    case = EvaluationCase(id="a", input={"items": [1]}, output=None)
    with pytest.raises(ValueError, match="missing JSON path"):
        VariableMapping(source="input", path=path).resolve(case)


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"": 1},
        {"a": 0},
        {"a": -1, "b": 2},
        {"a": math.nan},
        {"a": math.inf},
        {"a": True},
    ],
)
def test_normalization_rejects_invalid_weights(values):
    with pytest.raises(ValueError):
        normalize_distribution(values)


def test_stable_normalization_and_logit_shift_invariance():
    assert normalize_distribution({"a": 1e308, "b": 1e308}) == {"a": 0.5, "b": 0.5}
    expected = normalize_distribution({"a": 1, "b": 2}, logits=True)
    assert normalize_distribution({"a": 1001, "b": 1002}, logits=True) == expected
    assert normalize_distribution({"a": -1000, "b": 1000}, logits=True) == {"a": 0, "b": 1}


def test_typed_result_semantics_and_choice_concentration(definition):
    noul = normalize_result(definition.questions["correct"], {"yes": 3, "no": 1})
    assert noul.value == 0.75
    assert noul.label == "yes"
    assert noul.confidence is None
    assert noul.confidence_semantics == "not_available"
    choice = normalize_result(definition.questions["route"], {"accept": 3, "revise": 1})
    assert choice.top_probability == 0.75
    assert choice.confidence == 0.5
    assert choice.confidence_semantics == "choice_concentration"
    score = normalize_result(
        definition.questions["quality"], {"poor": 0.1, "partial": 0.2, "complete": 0.7}
    )
    assert score.value == pytest.approx(1.6)
    assert score.confidence is None  # Unknown source Score formula is not fabricated.
    assert outcome_labels(definition.questions["correct"]) == ["no", "yes"]
    with pytest.raises(ValueError, match="exactly match"):
        normalize_result(definition.questions["correct"], {"true": 1, "false": 0})


@pytest.mark.parametrize(
    "probabilities,expected",
    [
        ({"a": 1}, 1),
        ({"a": 0.5, "b": 0.5}, 0),
        ({"a": 1, "b": 0}, 1),
        ({"a": 0.6, "b": 0.2, "c": 0.2}, 0.4),
    ],
)
def test_source_choice_confidence(probabilities, expected):
    assert choice_confidence(probabilities) == pytest.approx(expected)


def test_single_outcome_choice():
    question = ChoiceQuestion(prompt="Only", outcomes=[Outcome(label="only", description="Only")])
    assert normalize_result(question, {"only": -500}, logits=True).confidence == 1


def test_score_supports_arbitrary_ordered_numeric_range():
    question = ScoreQuestion(
        prompt="Rate",
        outcomes=[
            ScoreOutcome(label="bad", description="bad", value=-10),
            ScoreOutcome(label="good", description="good", value=30),
        ],
    )
    assert normalize_result(question, {"bad": 0.75, "good": 0.25}).value == 0


def test_correction_jsonl_lifecycle(definition, case, tmp_path):
    records = [correction(case), correction(case, "train-2")]
    path = tmp_path / "corrections.jsonl"
    save_corrections(path, records, definition)
    assert len(path.read_text().splitlines()) == 2
    assert load_corrections(path, definition) == records
    assert (
        json.loads(few_shot_preview(definition, records, holdout_ids=[case.id]))[0][
            "corrected_labels"
        ]
        == records[0].labels
    )
    path.write_text(path.read_text() + "not json\n")
    with pytest.raises(ValidationError):
        load_corrections(path, definition)


@pytest.mark.parametrize(
    "labels",
    [
        {"correct": "yes"},
        {"correct": "maybe", "route": "accept", "quality": "complete"},
        {"correct": "yes", "route": "accept", "quality": "complete", "extra": "yes"},
    ],
)
def test_corrections_validate_against_definition(definition, case, labels, tmp_path):
    record = CorrectionRecord(case=case, labels=labels)
    with pytest.raises(ValueError):
        save_corrections(tmp_path / "invalid.jsonl", [record], definition)
    assert not (tmp_path / "invalid.jsonl").exists()


def test_few_shot_bounds_and_leakage_before_truncation(definition, case):
    records = [correction(case), correction(case, "reserved")]
    one = json.loads(few_shot_preview(definition, records, holdout_ids=[], max_examples=1))
    assert len(one) == 1
    assert few_shot_preview(definition, records, holdout_ids=[], max_chars=2) == "[]"
    for bounds in ({"max_examples": -1}, {"max_chars": 1}):
        with pytest.raises(ValueError):
            few_shot_preview(definition, records, holdout_ids=[], **bounds)
    with pytest.raises(ValueError, match="leakage"):
        few_shot_preview(definition, records, holdout_ids=["reserved"], max_examples=0)
    with pytest.raises(ValueError, match="unique"):
        few_shot_preview(definition, [records[0], records[0]], holdout_ids=[])


def test_live_request_separates_holdout_case_from_expected_labels(definition, case):
    request = build_evaluation_request(
        definition, case, corrections=[correction(case)], holdout_ids=[case.id]
    )
    assert request["state"]["case"] == definition.preview(case)
    assert request["questions"]["correct"]["type"] == "noul"
    assert "corrected_labels" not in build_evaluation_request(definition, case)["state"]
    with pytest.raises(ValueError, match="leakage"):
        build_evaluation_request(definition, case, corrections=[correction(case, case.id)])
    with pytest.raises(ValidationError):
        EvaluationCase.model_validate(case.model_dump() | {"expected_labels": {"correct": "yes"}})


@pytest.mark.parametrize(
    "probabilities",
    [
        {"yes": 0.9, "no": 0.9},
        {"yes": -0.1, "no": 1.1},
        {"yes": math.nan},
        {},
    ],
)
def test_calibration_rejects_nonprobabilities(probabilities):
    with pytest.raises(ValidationError):
        CalibrationExample(id="x", probabilities=probabilities, label="yes")


def test_calibration_fits_distribution_and_preserves_probability_bounds():
    calibrator = fit()
    result = apply(calibrator, {"no": 0.1, "yes": 0.9})
    assert result["yes"] == pytest.approx(0.8, abs=1e-7)
    assert calibrator.inverse_temperature == pytest.approx(math.log(4) / math.log(9), abs=1e-7)
    assert calibrator.training_log_loss_after < calibrator.training_log_loss_before
    for probabilities in ({"no": 0, "yes": 1}, {"no": 1, "yes": 0}, {"no": 0.5, "yes": 0.5}):
        adjusted = apply(calibrator, probabilities)
        assert all(0 <= value <= 1 for value in adjusted.values())
        assert sum(adjusted.values()) == pytest.approx(1)


def test_calibration_identity_is_exact_for_already_matching_frequencies():
    calibrator = fit(training_examples(0.8))
    assert calibrator.inverse_temperature == 1
    assert apply(calibrator, {"no": 0, "yes": 1}) == {"no": 0, "yes": 1}
    assert apply(calibrator, {"no": 0.5, "yes": 0.5}) == {"no": 0.5, "yes": 0.5}


def test_multiclass_fit_uses_named_outcomes():
    examples = [
        CalibrationExample(
            id=f"train-{index}",
            probabilities={"a": 0.8, "b": 0.1, "c": 0.1},
            label="a" if index < 6 else "b" if index < 8 else "c",
        )
        for index in range(10)
    ]
    calibrator = fit(examples)
    assert apply(calibrator, {"c": 0.1, "b": 0.1, "a": 0.8}) == pytest.approx(
        {"a": 0.6, "b": 0.2, "c": 0.2}, abs=1e-7
    )


def test_extreme_zero_probabilities_fit_without_overflow_or_capped_loss():
    examples = [
        CalibrationExample(
            id=f"train-{index}",
            probabilities={"no": 0, "yes": 1},
            label="yes" if index % 2 else "no",
        )
        for index in range(10)
    ]
    calibrator = fit(examples)
    assert calibrator.inverse_temperature == pytest.approx(0, abs=1e-7)
    assert calibrator.training_log_loss_after == pytest.approx(math.log(2))


def test_calibration_serialization_and_provenance(tmp_path):
    calibrator = fit()
    path = tmp_path / "calibration.json"
    calibrator.save(path)
    restored = DistributionCalibrator.load(path)
    assert restored == calibrator
    assert len(restored.training_sha256) == 64
    assert restored.training_ids == [example.id for example in training_examples()]
    assert restored.training_sha256 != fit(training_examples(0.8)).training_sha256
    probabilities = {"no": 0.1, "yes": 0.9}
    assert apply(restored, probabilities) == apply(calibrator, probabilities)
    for key in ("model", "profile", "schema_sha256"):
        identity = {
            "model": calibrator.model,
            "profile": calibrator.profile,
            "schema_sha256": calibrator.schema_sha256,
        } | {key: "mismatch"}
        with pytest.raises(ValueError, match="provenance"):
            calibrator.apply(probabilities, **identity)
    with pytest.raises(ValueError, match="outcome labels"):
        apply(calibrator, {"wrong": 0.5, "yes": 0.5})


def test_calibration_train_holdout_leakage_and_schema_checks():
    examples = training_examples()
    kwargs = {"holdout_ids": ["holdout-1"], "model": "m", "profile": "p", "schema_sha256": "a" * 64}
    for holdout_ids in ([], ["train-0"], ["holdout-1", "holdout-1"]):
        with pytest.raises(ValueError):
            DistributionCalibrator.fit(examples, **(kwargs | {"holdout_ids": holdout_ids}))
    for data in (
        [],
        [examples[0], examples[0]],
        [
            examples[0],
            CalibrationExample(
                id="different",
                probabilities={"other": 1},
                label="other",
            ),
        ],
    ):
        with pytest.raises(ValueError):
            DistributionCalibrator.fit(data, **kwargs)
    artifact = fit().model_dump(mode="json")
    artifact["holdout_ids"] = ["train-0"]
    with pytest.raises(ValidationError, match="leakage"):
        DistributionCalibrator.model_validate(artifact)


def test_independent_holdout_metrics_and_leakage_checks():
    calibrator = fit()
    examples = [
        example.model_copy(update={"id": f"holdout-{index}"})
        for index, example in enumerate(training_examples())
    ]
    metrics = calibrator.evaluate_holdout(examples)
    assert metrics["count"] == 10
    assert metrics["log_loss_after"] < metrics["log_loss_before"]
    assert metrics["brier_after"] == pytest.approx(0.32)
    for invalid in (
        [],
        training_examples(),
        [examples[0], examples[0]],
        [
            examples[0].model_copy(update={"id": "unreserved"}),
        ],
    ):
        with pytest.raises(ValueError):
            calibrator.evaluate_holdout(invalid)
    changed = [CalibrationExample(id="holdout-0", probabilities={"other": 1}, label="other")]
    with pytest.raises(ValueError, match="outcome labels"):
        calibrator.evaluate_holdout(changed)


def test_holdout_can_disprove_training_improvement():
    calibrator = fit()
    examples = [
        CalibrationExample(
            id=f"holdout-{index}",
            probabilities={"no": 0.1, "yes": 0.9},
            label="yes",
        )
        for index in range(10)
    ]
    metrics = calibrator.evaluate_holdout(examples)
    assert metrics["log_loss_after"] > metrics["log_loss_before"]


@pytest.mark.parametrize("path", [[True], [1.5]])
def test_json_paths_reject_coerced_indices(path):
    with pytest.raises(ValidationError):
        VariableMapping(source="input", path=path)


@pytest.mark.parametrize("probability", [True, "1.0"])
def test_calibration_requires_numeric_probabilities_not_coercions(probability):
    with pytest.raises(ValidationError):
        CalibrationExample(id="x", probabilities={"yes": probability}, label="yes")


def test_calibration_identity_preserves_valid_probabilities_exactly():
    calibrator = fit(training_examples(0.8))
    probabilities = {"no": 0.1, "yes": 0.9}
    assert apply(calibrator, probabilities) == probabilities


@pytest.mark.parametrize("value", [math.inf, math.nan])
def test_case_json_rejects_nonfinite_values(value):
    with pytest.raises(ValidationError):
        EvaluationCase(id="x", input={"nested": [value]}, output=None)
