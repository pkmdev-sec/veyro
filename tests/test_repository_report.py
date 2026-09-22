from __future__ import annotations

import math

import pytest

from veyro import repository_report as report


def test_binary_metrics_match_hand_calculation():
    predictions = [{"id": "good", "probability_yes": 0.9}, {"id": "bad", "probability_yes": 0.2}]
    metrics = report.criterion_metrics(
        predictions, {"good": "yes", "bad": "no"}, high_confidence=0.9
    )
    assert metrics["balanced_accuracy"] == 1
    assert metrics["brier"] == pytest.approx(0.025)
    assert metrics["log_loss"] == pytest.approx(-(math.log(0.9) + math.log(0.8)) / 2)
    assert metrics["high_confidence_false_accepts"] == 0
    predictions[1]["probability_yes"] = 0.95
    metrics = report.criterion_metrics(
        predictions, {"good": "yes", "bad": "no"}, high_confidence=0.9
    )
    assert metrics["false_accepts"] == 1
    assert metrics["high_confidence_false_accepts"] == 1
    assert metrics["balanced_accuracy"] == 0.5


@pytest.mark.parametrize("probability", [True, float("nan"), float("inf"), -0.1, 1.1])
def test_invalid_probabilities_never_become_metrics(probability):
    with pytest.raises(ValueError, match="probability"):
        report.criterion_metrics(
            [{"id": "a", "probability_yes": probability}, {"id": "b", "probability_yes": 0.5}],
            {"a": "yes", "b": "no"},
            high_confidence=0.9,
        )


def test_duplicate_predictions_cannot_replace_missing_cases():
    with pytest.raises(ValueError, match="cover"):
        report.criterion_metrics(
            [{"id": "a", "probability_yes": 0.9}] * 2, {"a": "yes", "b": "no"}, high_confidence=0.9
        )


def write_json(path, value):
    path.write_bytes(report.canonical(value))
    return report.sha256(path.read_bytes())


def write_rows(path, values):
    path.write_bytes(b"".join(report.canonical(row) + b"\n" for row in values))
    return report.sha256(path.read_bytes())


@pytest.fixture
def completed_study(tmp_path):
    study = tmp_path
    experiment_sha = write_json(study / "experiment.json", {"high_confidence_threshold": 0.9})
    model_sha = write_json(study / "conversion.result.json", {})
    adapter_sha = write_json(study / "adapter-manifest.json", {})
    cases = [
        {
            "id": name,
            "task": "fixture",
            "repository": "github.com/example/test",
            "candidate_sha256": name * 64,
            "prompt_sha256": name * 64,
            "messages": [{"role": "user", "content": name}],
        }
        for name in ("a", "b")
    ]
    cases_sha = write_rows(study / "test-cases.jsonl", cases)
    labels_sha = write_rows(
        study / "test-labels.jsonl", [{"id": "a", "label": "yes"}, {"id": "b", "label": "no"}]
    )
    contract_sha = write_json(
        study / "test-contract.json",
        {
            "experiment_sha256": experiment_sha,
            "cases_sha256": cases_sha,
            "labels_sha256": labels_sha,
        },
    )
    (study / "dataset").mkdir()
    development = [
        {
            **case,
            "messages": [
                *case["messages"],
                {"role": "assistant", "content": "Yes" if case["id"] == "a" else "No"},
            ],
        }
        for case in cases
    ]
    write_rows(study / "dataset/valid.jsonl", development)
    reservation_sha = write_json(study / "test-reservation.json", {"test_repositories": []})
    experiment_sha = write_json(
        study / "experiment.json",
        {
            "high_confidence_threshold": 0.9,
            "bound_files": {
                "test-reservation.json": reservation_sha,
                "dataset/valid.jsonl": report.sha256((study / "dataset/valid.jsonl").read_bytes()),
            },
        },
    )
    contract_sha = write_json(
        study / "test-contract.json",
        {
            "experiment_sha256": experiment_sha,
            "cases_sha256": cases_sha,
            "labels_sha256": labels_sha,
            "adapter_manifest_sha256": adapter_sha,
            "reservation_sha256": reservation_sha,
            "case_count": len(cases),
        },
    )
    for stage in ("baseline", "after", "test-baseline", "test-after"):
        binding = {
            "experiment_sha256": experiment_sha,
            "stage": stage,
            "model_manifest_sha256": model_sha,
        }
        if stage.endswith("after"):
            binding["adapter_manifest_sha256"] = adapter_sha
        if stage.startswith("test-"):
            binding["test_contract_sha256"] = contract_sha
        attempt_sha = write_json(study / f"{stage}.attempt.json", binding)
        predictions_sha = write_rows(
            study / f"{stage}-predictions.jsonl",
            [
                {
                    **binding,
                    **{k: case[k] for k in ("id", "candidate_sha256", "prompt_sha256")},
                    "probability_yes": 0.9 if case["id"] == "a" else 0.1,
                }
                for case in cases
            ],
        )
        write_json(
            study / f"{stage}.result.json",
            {
                "status": "succeeded",
                "count": 2,
                "attempt_sha256": attempt_sha,
                "predictions_sha256": predictions_sha,
            },
        )
    return study


def test_report_seals_predictions_before_reading_labels(completed_study, monkeypatch):
    original = report.rows

    def read(path):
        if path.name == "test-labels.jsonl":
            assert (completed_study / "test-prediction-seal.json").is_file()
        return original(path)

    monkeypatch.setattr(report, "rows", read)
    result = report.report_study(completed_study)
    assert result["metrics"]["test-after"]["balanced_accuracy"] == 1
    assert result["qualifies_g6"] is False


def test_incomplete_predictions_cannot_be_scored(completed_study):
    (completed_study / "test-after.result.json").unlink()
    with pytest.raises(FileNotFoundError):
        report.report_study(completed_study)
    assert not (completed_study / "test-prediction-seal.json").exists()


def test_prediction_tampering_fails_before_reveal(completed_study):
    with (completed_study / "test-after-predictions.jsonl").open("a") as output:
        output.write("{}\n")
    with pytest.raises(ValueError, match="unbound"):
        report.report_study(completed_study)
    assert not (completed_study / "test-prediction-seal.json").exists()


def test_development_label_tampering_fails_before_seal(completed_study):
    path = completed_study / "dataset/valid.jsonl"
    values = report.rows(path)
    values[0]["messages"][-1]["content"] = "No"
    write_rows(path, values)
    with pytest.raises(ValueError, match="frozen study file"):
        report.report_study(completed_study)
    assert not (completed_study / "test-prediction-seal.json").exists()


def test_missing_development_result_does_not_consume_seal(completed_study):
    (completed_study / "after.result.json").unlink()
    with pytest.raises(FileNotFoundError):
        report.report_study(completed_study)
    assert not (completed_study / "test-prediction-seal.json").exists()


def test_report_rejects_changed_adapter_manifest(completed_study):
    write_json(completed_study / "adapter-manifest.json", {"changed": True})
    with pytest.raises(ValueError, match="adapter manifest"):
        report.report_study(completed_study)
    assert not (completed_study / "test-prediction-seal.json").exists()
