from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from veyro import evaluation_capture as capture
from veyro.local_evaluation import calibration_schema


@pytest.fixture
def setup(monkeypatch):
    task = capture.TaskContract(
        task_id="ascii-slug-development",
        repository="synthetic-development-only",
        requirement="Produce ASCII slugs.",
        criteria={
            "ascii": "Only ASCII letters and digits remain.",
            "separators": "Separator runs collapse to one dash.",
        },
        protected_suite_sha256="a" * 64,
    )
    contract = capture.make_contract(task, "small", "development")
    candidate = {"slug.py": "def slug(text): return text"}
    evidence = capture.CheckEvidence(
        candidate_sha256=capture.digest(candidate),
        protected_suite_sha256="a" * 64,
        criterion_evidence={"ascii": "Unicode check failed", "separators": "Run check failed"},
    )
    definition = task.evaluator()
    response = {
        "case_id": "case-1",
        "model": contract.model,
        "profile": contract.profile,
        "protocol": contract.protocol,
        "evaluator": definition.name,
        "version": definition.version,
        "calibrated": False,
        "results": {
            name: {
                "calibration_schema_sha256": calibration_schema(definition, name),
                "probabilities": {"no": 0.8, "yes": 0.2},
            }
            for name in definition.questions
        },
        "metrics": {"generated_tokens": 0},
    }
    inference = Mock(return_value=response)
    monkeypatch.setattr(capture, "run_evaluator", inference)
    return contract, candidate, evidence, inference


def test_capture_retains_full_response_and_exact_inputs(tmp_path, setup):
    contract, candidate, evidence, inference = setup
    destination = tmp_path / "attempt"

    def evaluate(definition, case, profile, **kwargs):
        assert (destination / "attempt.json").is_file()
        assert case.output == candidate
        assert case.input["checks"] == evidence.model_dump(mode="json")
        assert set(definition.questions) == set(contract.task.criteria)
        return inference.return_value

    inference.side_effect = evaluate
    receipt = capture.capture(contract, "case-1", candidate, evidence, destination)
    assert receipt["response"] == inference.return_value
    assert receipt["response_sha256"] == capture.digest(inference.return_value)
    assert receipt["candidate_sha256"] == evidence.candidate_sha256
    assert receipt["contract_sha256"] == capture.digest(contract.model_dump(mode="json"))
    assert json.loads((destination / "prediction.json").read_text()) == receipt
    assert receipt["case_sha256"] == capture.digest(
        json.loads((destination / "case.json").read_text())
    )
    with pytest.raises(FileExistsError):
        capture.capture(contract, "case-1", candidate, evidence, destination)
    assert inference.call_count == 1


@pytest.mark.parametrize(
    "field",
    [
        "model",
        "protocol",
        "evaluator_schema_sha256",
        "feature_schema_sha256",
    ],
)
def test_frozen_contract_drift_is_rejected_before_attempt(tmp_path, setup, field):
    contract, candidate, evidence, inference = setup
    contract = contract.model_copy(update={field: "b" * 64})
    with pytest.raises(ValueError, match="capture contract"):
        capture.capture(contract, "case-1", candidate, evidence, tmp_path / "attempt")
    inference.assert_not_called()
    assert not (tmp_path / "attempt").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("candidate_sha256", "b" * 64),
        ("protected_suite_sha256", "b" * 64),
        ("criterion_evidence", {"ascii": "failed"}),
        ("criterion_evidence", {"ascii": "failed", "separators": "failed", "extra": "passed"}),
    ],
)
def test_check_binding_rejected_before_inference(tmp_path, setup, field, value):
    contract, candidate, evidence, inference = setup
    evidence = evidence.model_copy(update={field: value})
    with pytest.raises(ValueError):
        capture.capture(contract, "case-1", candidate, evidence, tmp_path / "attempt")
    inference.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("case_id", "different"),
        ("model", "different"),
        ("protocol", "old"),
        ("profile", "14b"),
        ("evaluator", "different"),
        ("version", "old"),
        ("calibrated", True),
        ("results", {}),
        ("results", {"ascii": {}, "separators": {}}),
    ],
)
def test_wrong_response_never_becomes_receipt_or_allows_retry(tmp_path, setup, field, value):
    contract, candidate, evidence, inference = setup
    inference.return_value[field] = value
    destination = tmp_path / "attempt"
    with pytest.raises(ValueError):
        capture.capture(contract, "case-1", candidate, evidence, destination)
    assert (destination / "response.json").is_file()
    assert not (destination / "prediction.json").exists()
    with pytest.raises(FileExistsError):
        capture.capture(contract, "case-1", candidate, evidence, destination)
    assert inference.call_count == 1


def test_model_failure_consumes_attempt(tmp_path, setup):
    contract, candidate, evidence, inference = setup
    inference.side_effect = TimeoutError("model timeout")
    destination = tmp_path / "attempt"
    with pytest.raises(TimeoutError):
        capture.capture(contract, "case-1", candidate, evidence, destination)
    assert (destination / "attempt.json").is_file()
    assert not (destination / "prediction.json").exists()
    with pytest.raises(FileExistsError):
        capture.capture(contract, "case-1", candidate, evidence, destination)
    assert inference.call_count == 1


def test_holdout_partition_is_not_available(setup):
    contract, *_ = setup
    with pytest.raises(ValueError):
        capture.make_contract(contract.task, "small", "holdout")


def test_freeze_never_overwrites_existing_contract(tmp_path, setup):
    contract, *_ = setup
    path = tmp_path / "contract.json"
    capture.write_new(path, contract.model_dump(mode="json"))
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        capture.write_new(path, {})
    assert path.read_bytes() == before


def test_capture_with_real_evaluator_response(tmp_path, setup, monkeypatch):
    from veyro import local_evaluation, local_server

    contract, candidate, evidence, _ = setup
    monkeypatch.setattr(capture, "run_evaluator", local_evaluation.run_evaluator)
    monkeypatch.setattr(
        local_server,
        "evaluate_local",
        lambda *args, **kwargs: {
            "protocol": contract.protocol,
            "predictions": {
                name: {"outcomes": ["no", "yes"], "probabilities": [0.8, 0.2]}
                for name in contract.task.criteria
            },
            "metrics": {"generated_tokens": 0},
        },
    )
    receipt = capture.capture(contract, "case-1", candidate, evidence, tmp_path / "attempt")
    assert receipt["response"]["results"]["ascii"]["value"] == 0.2
    assert receipt["response"]["results"]["ascii"]["source"] == "uncalibrated_label_logits"
