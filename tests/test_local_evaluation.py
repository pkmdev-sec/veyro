from __future__ import annotations

import copy
import json
import threading
from types import SimpleNamespace

import pytest

from veyro import local_evaluation, local_server
from veyro.evaluators import (
    CalibrationExample,
    CorrectionRecord,
    DistributionCalibrator,
    EvaluationCase,
    EvaluatorDefinition,
)
from veyro.local_models import load_profiles
from veyro.readout import READOUT_PROTOCOL


@pytest.fixture
def definition():
    return EvaluatorDefinition.model_validate(
        {
            "name": "review",
            "version": "1",
            "prompt": "Task: {{task}} Artifact: {{artifact}}",
            "variables": {"task": {"source": "input"}, "artifact": {"source": "output"}},
            "questions": {
                "done": {"type": "noul", "prompt": "Complete?"},
                "kind": {
                    "type": "choice",
                    "prompt": "Kind?",
                    "outcomes": [
                        {"label": "code", "description": "Source code"},
                        {"label": "text", "description": "Plain text"},
                    ],
                },
                "quality": {
                    "type": "score",
                    "prompt": "Quality?",
                    "outcomes": [
                        {"label": "low", "description": "Incomplete", "value": 0.0},
                        {"label": "high", "description": "Complete", "value": 10.0},
                    ],
                },
                "constant": {
                    "type": "choice",
                    "prompt": "Only option",
                    "outcomes": [{"label": "only", "description": "The only option"}],
                },
            },
        }
    )


@pytest.fixture
def case():
    return EvaluationCase(id="live", input="Implement", output="print(1)")


@pytest.fixture
def response():
    return {
        "protocol": READOUT_PROTOCOL,
        "calibrated": False,
        "metrics": {"generated_tokens": 0},
        "predictions": {
            "done": {"outcomes": ["no", "yes"], "probabilities": [0.1, 0.9]},
            "kind": {"outcomes": ["code", "text"], "probabilities": [0.75, 0.25]},
            "quality": {"outcomes": ["low", "high"], "probabilities": [0.2, 0.8]},
        },
    }


@pytest.fixture
def service(tmp_path, response):
    profile = load_profiles()["small"]
    directory = local_server._directory(tmp_path / "worker")
    metadata = {
        "profile": profile.model_dump(),
        "model_manifest_sha256": profile.manifest_sha256,
        "model_blob_sha256": profile.blob_sha256,
        "protocol": READOUT_PROTOCOL,
        "context_size": 8192,
        "instance_id": "b" * 32,
        "token": "c" * 64,
        "port": 0,
    }
    calls = []

    def evaluate(state, questions, *, timeout):
        calls.append((state, questions, timeout))
        return copy.deepcopy(response)

    server = local_server._Server(0, metadata)
    metadata["port"] = server.server_address[1]
    local_server._write_metadata(directory / "small.json", metadata)
    server.engine = SimpleNamespace(evaluate=evaluate)
    server.status = "ready"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield directory, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def fit(definition, *, question="done", **overrides):
    identity = local_evaluation.model_identity(load_profiles()["small"])
    arguments = {
        "model": identity,
        "profile": "small",
        "schema_sha256": local_evaluation.calibration_schema(definition, question),
        "holdout_ids": ["held"],
    }
    arguments.update(overrides)
    return DistributionCalibrator.fit(
        [CalibrationExample(id="train", probabilities={"no": 0.2, "yes": 0.8}, label="yes")],
        **arguments,
    )


def test_typed_evaluator_uses_authenticated_http_and_preserves_semantics(definition, case, service):
    directory, calls = service
    result = local_evaluation.run_evaluator(definition, case, "small", state_dir=directory)
    assert result["calibrated"] is False
    assert result["model"] == local_evaluation.model_identity(load_profiles()["small"])
    assert result["metrics"] == {"generated_tokens": 0}
    assert result["results"]["done"]["value"] == pytest.approx(0.9)
    assert result["results"]["done"]["feedback"] == {"type": "boolean", "value": True}
    choice = result["results"]["kind"]
    assert choice["confidence"] == pytest.approx(0.5)
    assert choice["top_probability"] == 0.75
    assert choice["confidence_semantics"] == "choice_concentration"
    assert choice["source"] == "uncalibrated_label_logits"
    score = result["results"]["quality"]
    assert score["feedback"] == {"type": "continuous", "value": 8.0, "min": 0.0, "max": 10.0}
    assert score["confidence"] is None
    assert result["results"]["constant"]["source"] == "single_outcome"
    assert len(calls) == 1
    state, questions, timeout = calls[0]
    assert state == {"case": "Task: Implement Artifact: print(1)", "few_shot_examples": []}
    assert [question.name for question in questions] == ["done", "kind", "quality"]
    assert questions[1].outcomes == (("code", "Source code"), ("text", "Plain text"))
    assert timeout == 60


@pytest.mark.parametrize(
    "probabilities",
    [
        [True, False],
        ["0.1", "0.9"],
        [None, 1],
        [float("nan"), 1],
        [float("inf"), 0],
        [-0.1, 1.1],
        [0, 0],
        [0.1, 0.8],
        [1],
        [0, 0, 1],
    ],
)
def test_invalid_probabilities_are_not_normalized_into_success(
    monkeypatch, definition, case, response, probabilities
):
    response["predictions"]["done"]["probabilities"] = probabilities
    monkeypatch.setattr(local_server, "evaluate_local", lambda *a, **k: response)
    with pytest.raises(ValueError, match="invalid outcome distribution"):
        local_evaluation.run_evaluator(definition, case, "small")


@pytest.mark.parametrize("corruption", ["protocol", "missing", "extra", "order", "labels"])
def test_protocol_and_outcome_mismatch_fail_closed(
    monkeypatch, definition, case, response, corruption
):
    if corruption == "protocol":
        response["protocol"] = "old-protocol"
    elif corruption == "missing":
        response["predictions"].pop("done")
    elif corruption == "extra":
        response["predictions"]["other"] = response["predictions"]["done"]
    else:
        response["predictions"]["done"]["outcomes"] = (
            ["yes", "no"] if corruption == "order" else ["no", "maybe"]
        )
    monkeypatch.setattr(local_server, "evaluate_local", lambda *a, **k: response)
    with pytest.raises(ValueError):
        local_evaluation.run_evaluator(definition, case, "small")


@pytest.mark.parametrize(
    "override", [{"model": "another-model"}, {"profile": "14b"}, {"schema_sha256": "f" * 64}]
)
def test_calibration_identity_rejected_before_inference(monkeypatch, definition, case, override):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid provenance must fail before inference")

    monkeypatch.setattr(local_server, "evaluate_local", forbidden)
    with pytest.raises(ValueError, match="provenance"):
        local_evaluation.run_evaluator(
            definition, case, "small", calibrations={"done": fit(definition, **override)}
        )


def test_calibration_round_trip_marks_only_fitted_question(tmp_path, definition, case, service):
    artifact = fit(definition)
    path = tmp_path / "calibration.json"
    artifact.save(path)
    loaded = DistributionCalibrator.load(path)
    assert loaded == artifact
    assert loaded.training_ids == ["train"]
    assert loaded.holdout_ids == ["held"]
    assert len(loaded.training_sha256) == 64
    result = local_evaluation.run_evaluator(
        definition, case, "small", state_dir=service[0], calibrations={"done": loaded}
    )
    assert result["calibrated"] is False
    assert result["results"]["done"]["source"] == "posthoc_temperature_scaling"
    assert result["results"]["kind"]["source"] == "uncalibrated_label_logits"
    assert result["results"]["done"]["value"] > 0.9


@pytest.mark.parametrize("id", ["live", "held"])
def test_correction_leakage_rejected_before_inference(monkeypatch, definition, case, id):
    def forbidden(*args, **kwargs):
        pytest.fail("leaked labels must never reach inference")

    monkeypatch.setattr(local_server, "evaluate_local", forbidden)
    correction = CorrectionRecord(
        case=case.model_copy(update={"id": id}),
        labels={"done": "yes", "kind": "code", "quality": "high", "constant": "only"},
    )
    with pytest.raises(ValueError, match="holdout leakage"):
        local_evaluation.run_evaluator(
            definition, case, "small", corrections=[correction], holdout_ids=["held"]
        )


def test_disjoint_correction_labels_never_become_live_labels(definition, case, service):
    correction = CorrectionRecord(
        case=case.model_copy(update={"id": "example"}),
        labels={"done": "no", "kind": "text", "quality": "low", "constant": "only"},
    )
    local_evaluation.run_evaluator(
        definition,
        case,
        "small",
        corrections=[correction],
        holdout_ids=["held"],
        state_dir=service[0],
    )
    state = service[1][0][0]
    assert "corrected_labels" not in state["case"]
    assert state["few_shot_examples"][0]["corrected_labels"] == correction.labels
    assert "reference" not in json.dumps(state)


def test_calibration_is_bound_to_actual_correction_examples(definition, case, service):
    correction = CorrectionRecord(
        case=case.model_copy(update={"id": "example"}),
        labels={"done": "no", "kind": "text", "quality": "low", "constant": "only"},
    )
    artifact = fit(
        definition,
        schema_sha256=local_evaluation.calibration_schema(definition, "done", [correction]),
    )
    local_evaluation.run_evaluator(
        definition,
        case,
        "small",
        corrections=[correction],
        calibrations={"done": artifact},
        state_dir=service[0],
    )
    assert len(service[1]) == 1
    with pytest.raises(ValueError, match="schema"):
        local_evaluation.run_evaluator(
            definition,
            case,
            "small",
            calibrations={"done": artifact},
            state_dir=service[0],
        )
    assert len(service[1]) == 1


def test_calibrator_holdout_cannot_be_used_as_few_shot(definition, case, service):
    correction = CorrectionRecord(
        case=case.model_copy(update={"id": "held"}),
        labels={"done": "no", "kind": "text", "quality": "low", "constant": "only"},
    )
    artifact = fit(
        definition,
        schema_sha256=local_evaluation.calibration_schema(definition, "done", [correction]),
    )
    with pytest.raises(ValueError, match="holdout leakage"):
        local_evaluation.run_evaluator(
            definition,
            case,
            "small",
            corrections=[correction],
            calibrations={"done": artifact},
            state_dir=service[0],
        )
    assert service[1] == []


def test_changed_readout_protocol_rejects_old_calibration(monkeypatch, definition, case):
    current_protocol = local_evaluation.READOUT_PROTOCOL
    monkeypatch.setattr(local_evaluation, "READOUT_PROTOCOL", "qwen-label-readout-v1")
    artifact = fit(definition)
    monkeypatch.setattr(local_evaluation, "READOUT_PROTOCOL", current_protocol)

    def forbidden(*args, **kwargs):
        pytest.fail("stale calibration must fail before inference")

    monkeypatch.setattr(local_server, "evaluate_local", forbidden)
    with pytest.raises(ValueError, match="schema"):
        local_evaluation.run_evaluator(definition, case, "small", calibrations={"done": artifact})
