"""Score complete internal pilot predictions without treating them as qualification."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

from veyro.repository_dataset import canonical, frozen_file, sha256


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def criterion_metrics(
    predictions: list[dict], labels: dict[str, str], *, high_confidence: float
) -> dict:
    if len(predictions) != len(labels) or {row["id"] for row in predictions} != set(labels):
        raise ValueError("predictions must cover every label exactly once")
    if set(labels.values()) != {"yes", "no"}:
        raise ValueError("both outcome classes are required")
    probabilities = [row["probability_yes"] for row in predictions]
    if any(
        type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1
        for value in probabilities
    ):
        raise ValueError("invalid prediction probability")
    expected = [labels[row["id"]] == "yes" for row in predictions]
    decisions = [value >= 0.5 for value in probabilities]
    positives = sum(expected)
    negatives = len(expected) - positives
    true_positives = sum(
        label and decision for label, decision in zip(expected, decisions, strict=True)
    )
    true_negatives = sum(
        not label and not decision for label, decision in zip(expected, decisions, strict=True)
    )
    positive_accuracy, negative_accuracy = true_positives / positives, true_negatives / negatives
    return {
        "count": len(expected),
        "positive_count": positives,
        "negative_count": negatives,
        "accuracy": (true_positives + true_negatives) / len(expected),
        "positive_accuracy": positive_accuracy,
        "negative_accuracy": negative_accuracy,
        "balanced_accuracy": (positive_accuracy + negative_accuracy) / 2,
        "false_accepts": negatives - true_negatives,
        "false_rejects": positives - true_positives,
        "high_confidence_false_accepts": sum(
            not label and p >= high_confidence
            for label, p in zip(expected, probabilities, strict=True)
        ),
        "brier": sum((p - label) ** 2 for p, label in zip(probabilities, expected, strict=True))
        / len(expected),
        "log_loss": -sum(
            math.log(max(1e-15, min(1 - 1e-15, p if label else 1 - p)))
            for p, label in zip(probabilities, expected, strict=True)
        )
        / len(expected),
    }


def verified_predictions(study: Path, stage: str, cases: list[dict]) -> list[dict]:
    result = json.loads((study / f"{stage}.result.json").read_text())
    attempt_path = study / f"{stage}.attempt.json"
    prediction_path = study / f"{stage}-predictions.jsonl"
    attempt = json.loads(attempt_path.read_text())
    experiment_sha = sha256((study / "experiment.json").read_bytes())
    if (
        result.get("status") != "succeeded"
        or result.get("count") != len(cases)
        or result.get("attempt_sha256") != sha256(attempt_path.read_bytes())
        or attempt.get("experiment_sha256") != experiment_sha
        or result.get("predictions_sha256") != sha256(prediction_path.read_bytes())
    ):
        raise ValueError(f"incomplete or unbound prediction stage: {stage}")
    predictions = rows(prediction_path)
    case_index = {row["id"]: row for row in cases}
    if len(predictions) != len(case_index) or {row["id"] for row in predictions} != set(case_index):
        raise ValueError("prediction coverage mismatch")
    expected = {
        "experiment_sha256": experiment_sha,
        "stage": stage,
        "model_manifest_sha256": sha256((study / "conversion.result.json").read_bytes()),
    }
    if stage.endswith("after"):
        expected["adapter_manifest_sha256"] = sha256((study / "adapter-manifest.json").read_bytes())
    if stage.startswith("test-"):
        expected["test_contract_sha256"] = sha256((study / "test-contract.json").read_bytes())
    for row in predictions:
        if any(row.get(key) != value for key, value in expected.items()):
            raise ValueError("prediction provenance mismatch")
        case = case_index[row["id"]]
        if any(row[key] != case[key] for key in ("candidate_sha256", "prompt_sha256")):
            raise ValueError("prediction input binding mismatch")
        probability = row.get("probability_yes")
        if (
            type(probability) not in (float, int)
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
        ):
            raise ValueError("invalid prediction probability")
    return predictions


def report_study(study: Path) -> dict:
    experiment_path = study / "experiment.json"
    experiment = json.loads(experiment_path.read_text())
    test_contract_path = study / "test-contract.json"
    contract = json.loads(test_contract_path.read_text())
    cases_path = study / "test-cases.jsonl"
    if contract["experiment_sha256"] != sha256(experiment_path.read_bytes()):
        raise ValueError("test belongs to a different experiment")
    if contract["adapter_manifest_sha256"] != sha256(
        (study / "adapter-manifest.json").read_bytes()
    ):
        raise ValueError("test adapter manifest changed")
    reservation = frozen_file(study, experiment, "test-reservation.json")
    if contract["reservation_sha256"] != sha256(reservation):
        raise ValueError("test reservation binding mismatch")
    if sha256(cases_path.read_bytes()) != contract["cases_sha256"]:
        raise ValueError("test inputs changed")
    cases = rows(cases_path)
    if len(cases) != contract["case_count"]:
        raise ValueError("test contract case coverage mismatch")
    frozen_file(study, experiment, "dataset/valid.jsonl")
    development = rows(study / "dataset/valid.jsonl")
    development_predictions = {
        name: verified_predictions(study, name, development) for name in ("baseline", "after")
    }
    test_predictions = {
        name: verified_predictions(study, name, cases) for name in ("test-baseline", "test-after")
    }
    seal = {
        "experiment_sha256": sha256(experiment_path.read_bytes()),
        "test_contract_sha256": sha256(test_contract_path.read_bytes()),
        "prediction_sha256": {
            name: sha256((study / f"{name}-predictions.jsonl").read_bytes())
            for name in test_predictions
        },
        "sealed_at": datetime.now(UTC).isoformat(),
    }
    with (study / "test-prediction-seal.json").open("xb") as target:
        target.write(canonical(seal))
    # The label file is opened only after both complete prediction sets have been sealed.
    label_path = study / "test-labels.jsonl"
    if sha256(label_path.read_bytes()) != contract["labels_sha256"]:
        raise ValueError("test label commitment mismatch")
    label_rows = rows(label_path)
    labels = {row["id"]: row["label"] for row in label_rows}
    if len(labels) != len(label_rows):
        raise ValueError("duplicate labels")
    high_confidence = experiment["high_confidence_threshold"]
    metrics = {
        name: criterion_metrics(values, labels, high_confidence=high_confidence)
        for name, values in test_predictions.items()
    }
    development_labels = {row["id"]: row["messages"][-1]["content"].lower() for row in development}
    for name in ("baseline", "after"):
        metrics[name] = criterion_metrics(
            development_predictions[name],
            development_labels,
            high_confidence=high_confidence,
        )
    report = {
        "scope": "internal_mutation_pilot_not_independent_qualification",
        "unit": "individual_criterion_not_artifact_completion",
        "probabilities": "uncalibrated_conditional_yes_no",
        "experiment_sha256": sha256(experiment_path.read_bytes()),
        "prediction_seal_sha256": sha256((study / "test-prediction-seal.json").read_bytes()),
        "test_task_count": len({row["task"] for row in cases}),
        "test_repositories": sorted({row["repository"] for row in cases}),
        "metrics": metrics,
        "qualifies_g6": False,
        "limits": [
            "Related variants are correlated, not independent repair tasks.",
            "Cases and executable labels were authored in the development workspace.",
            "No independent custodian, calibration or replicated qualification.",
            "Only criterion decisions were measured; no completion probability was fitted.",
        ],
    }
    with (study / "report.json").open("xb") as target:
        target.write(canonical(report))
    return report
