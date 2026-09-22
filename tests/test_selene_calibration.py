from __future__ import annotations

from datetime import UTC, datetime

import pytest

from veyro.selene_calibration import (
    BinaryLabel,
    BinaryPrediction,
    CalibrationContract,
    CalibrationMethod,
    QuantizationContract,
    binary_metrics,
    calibrated_probability,
    compare_quantization,
    fit_calibration,
    records_sha256,
)

MODEL = "AtlaAI/Selene@bf16:test"
QUANTIZED = "AtlaAI/Selene@q4:test"


def prediction(
    case_id: str,
    difference: float | None,
    *,
    model: str = MODEL,
    latency: float = 10,
    memory: int = 100,
) -> BinaryPrediction:
    return BinaryPrediction(
        case_id=case_id,
        case_sha256=(case_id[0] * 64),
        model_identity=model,
        yes_logit=difference,
        no_logit=0.0 if difference is not None else None,
        latency_ms=latency,
        peak_memory_bytes=memory,
    )


def label(case_id: str, expected: str) -> BinaryLabel:
    return BinaryLabel(
        case_id=case_id,
        case_sha256=(case_id[0] * 64),
        expected=expected,
        author=f"author-{case_id}",
        reviewer=f"reviewer-{case_id}",
    )


def corpus() -> tuple[list[BinaryPrediction], list[BinaryLabel]]:
    predictions = [
        prediction("a-positive", 2.0, latency=10),
        prediction("b-positive", 1.0, latency=20),
        prediction("c-negative", -2.0, latency=30),
        prediction("d-negative", -1.0, latency=40),
    ]
    labels = [
        label("a-positive", "yes"),
        label("b-positive", "yes"),
        label("c-negative", "no"),
        label("d-negative", "no"),
    ]
    return predictions, labels


def calibration_contract(
    predictions: list[BinaryPrediction], labels: list[BinaryLabel]
) -> CalibrationContract:
    return CalibrationContract(
        id="calibration-contract",
        created_at=datetime(2026, 9, 22, tzinfo=UTC),
        implementer="training-implementer",
        model_identity=MODEL,
        predictions_sha256=records_sha256(predictions),
        labels_sha256=records_sha256(labels),
        methods=frozenset(
            {
                CalibrationMethod.TEMPERATURE,
                CalibrationMethod.PLATT,
                CalibrationMethod.ISOTONIC,
            }
        ),
        min_raw_balanced_accuracy=1.0,
        max_raw_false_accepts=0,
    )


def quantization_contract(
    bf16: list[BinaryPrediction],
    quantized: list[BinaryPrediction],
    labels: list[BinaryLabel],
) -> QuantizationContract:
    return QuantizationContract(
        id="quantization-contract",
        created_at=datetime(2026, 9, 22, tzinfo=UTC),
        implementer="training-implementer",
        bf16_identity=MODEL,
        quantized_identity=QUANTIZED,
        bf16_predictions_sha256=records_sha256(bf16),
        quantized_predictions_sha256=records_sha256(quantized),
        labels_sha256=records_sha256(labels),
        max_p95_ms=100,
        max_peak_memory_bytes=1000,
    )


def test_binary_metrics_count_every_false_accept_and_exact_score_coverage():
    predictions, labels = corpus()
    predictions[3] = prediction("d-negative", 0.1, latency=40)
    metrics = binary_metrics(predictions, labels)
    assert metrics.cases == 4
    assert metrics.score_coverage == 1
    assert metrics.false_accepts == 1
    assert metrics.positive_accuracy == 1
    assert metrics.negative_accuracy == 0.5
    assert metrics.balanced_accuracy == 0.75
    assert metrics.p95_ms == 40

    predictions[0] = prediction("a-positive", None)
    incomplete = binary_metrics(predictions, labels)
    assert incomplete.score_coverage == 0.75


def test_calibration_compares_frozen_methods_without_changing_selected_classes():
    predictions, labels = corpus()
    artifact = fit_calibration(calibration_contract(predictions, labels), predictions, labels)
    assert artifact.raw_metrics.balanced_accuracy == 1
    assert artifact.raw_metrics.false_accepts == 0
    assert artifact.selected_method in {"identity", "temperature", "platt", "isotonic"}
    selected = next(
        candidate
        for candidate in artifact.candidates
        if candidate.method == artifact.selected_method
    )
    assert selected.eligible is True
    assert selected.class_predictions_preserved is True
    assert selected.metrics.brier <= artifact.raw_metrics.brier + 1e-15
    assert selected.metrics.log_loss <= artifact.raw_metrics.log_loss + 1e-15
    probability = calibrated_probability(artifact, 2.0, 0.0)
    assert 0 <= probability <= 1


def test_calibration_refuses_bad_classification_incomplete_scores_and_nonindependent_labels():
    predictions, labels = corpus()
    bad = list(predictions)
    bad[3] = prediction("d-negative", 1.0)
    contract = calibration_contract(bad, labels).model_copy(
        update={"min_raw_balanced_accuracy": 1.0, "max_raw_false_accepts": 0}
    )
    with pytest.raises(ValueError, match="calibration prerequisite"):
        fit_calibration(contract, bad, labels)

    incomplete = list(predictions)
    incomplete[0] = prediction("a-positive", None)
    contract = calibration_contract(incomplete, labels).model_copy(
        update={"min_raw_balanced_accuracy": 0}
    )
    with pytest.raises(ValueError, match="100% decision-score coverage"):
        fit_calibration(contract, incomplete, labels)

    dependent = list(labels)
    dependent[0] = dependent[0].model_copy(update={"author": "training-implementer"})
    contract = calibration_contract(predictions, dependent)
    with pytest.raises(ValueError, match="not independent"):
        fit_calibration(contract, predictions, dependent)


def test_calibration_contract_hashes_are_enforced():
    predictions, labels = corpus()
    contract = calibration_contract(predictions, labels)
    changed = list(predictions)
    changed[0] = prediction("a-positive", 2.1)
    with pytest.raises(ValueError, match="frozen contract"):
        fit_calibration(contract, changed, labels)


def test_quantization_passes_only_without_class_or_probability_regression():
    bf16, labels = corpus()
    quantized = [
        prediction(item.case_id, item.yes_logit, model=QUANTIZED, latency=5, memory=50)
        for item in bf16
    ]
    report = compare_quantization(
        quantization_contract(bf16, quantized, labels), bf16, quantized, labels
    )
    assert report.passes is True
    assert report.rejection_reasons == []
    assert report.quantized.false_accepts == 0


def test_quantization_rejects_new_false_accept_and_class_regression():
    bf16, labels = corpus()
    quantized = [prediction(item.case_id, item.yes_logit, model=QUANTIZED) for item in bf16]
    quantized[3] = prediction("d-negative", 0.5, model=QUANTIZED)
    contract = quantization_contract(bf16, quantized, labels).model_copy(
        update={"max_brier_increase": 1.0, "max_log_loss_increase": 1.0}
    )
    report = compare_quantization(contract, bf16, quantized, labels)
    assert report.passes is False
    assert "new_false_accept" in report.rejection_reasons
    assert "negative_class_regression" in report.rejection_reasons


def test_quantization_rejects_coverage_latency_memory_and_metric_regression():
    bf16, labels = corpus()
    quantized = [
        prediction(item.case_id, item.yes_logit, model=QUANTIZED, latency=200, memory=2000)
        for item in bf16
    ]
    quantized[0] = prediction("a-positive", None, model=QUANTIZED, latency=200, memory=2000)
    contract = quantization_contract(bf16, quantized, labels)
    report = compare_quantization(contract, bf16, quantized, labels)
    assert report.passes is False
    assert {
        "decision_score_coverage",
        "latency_limit",
        "memory_limit",
    } <= set(report.rejection_reasons)


def test_prediction_requires_both_exact_logits_and_finite_values():
    with pytest.raises(ValueError, match="both be present"):
        BinaryPrediction(
            case_id="a-case",
            case_sha256="a" * 64,
            model_identity=MODEL,
            yes_logit=1.0,
            no_logit=None,
            latency_ms=1,
            peak_memory_bytes=1,
        )
    with pytest.raises(ValueError, match="finite"):
        prediction("a-case", float("inf"))


def test_isotonic_calibration_handles_repeated_raw_probabilities_deterministically():
    predictions, labels = corpus()
    predictions[1] = prediction("b-positive", 2.0)
    predictions[3] = prediction("d-negative", -2.0)
    artifact = fit_calibration(calibration_contract(predictions, labels), predictions, labels)
    isotonic = next(item for item in artifact.candidates if item.method == "isotonic")
    thresholds = isotonic.parameters["thresholds"]
    assert isinstance(thresholds, list)
    assert thresholds == sorted(set(thresholds))


def test_calibration_artifact_rejects_unbound_or_invalid_selected_parameters():
    predictions, labels = corpus()
    artifact = fit_calibration(calibration_contract(predictions, labels), predictions, labels)
    with pytest.raises(ValueError, match="parameters do not match"):
        artifact.model_copy(update={"parameters": {"temperature": -1.0}}).__class__.model_validate(
            {
                **artifact.model_dump(mode="python"),
                "parameters": {"temperature": -1.0},
            }
        )
