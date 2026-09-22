from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path

import pytest


def load_study():
    path = Path(__file__).parents[1] / ".audit" / "zerank-study-v1" / "study.py"
    spec = importlib.util.spec_from_file_location("zerank_study", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def installation_identity():
    return {
        "python_sha256": "a" * 64,
        "runtime_sha256": "b" * 64,
        "runtime_layout_sha256": "c" * 64,
        "model_sha256": "d" * 64,
        "model_layout_sha256": "e" * 64,
    }


def laya_receipt(probability: float = 0.8):
    return {
        "yes_probability": probability,
        "label": "yes" if probability > 0.5 else "no",
        "probabilities": {"no": 1 - probability, "yes": probability},
        "source": "uncalibrated_model_distribution",
        "model": "laya-manifest",
        "protocol": "typed-label-readout-v5",
        "metrics": {
            "backend": "laya",
            "probability_source": "laya_rounded_4dp_renormalized",
            "latency_ms": 10.0,
            "input_tokens": 20,
            "generated_tokens": 0,
        },
    }


def valid_prediction_receipt(study, contract_path: Path, installation_path: Path):
    contract = {
        "study_id": "zerank-study-v1",
        "selection": {"query_template": study.RANK_QUERY, "top_k": study.TOP_K},
        "worker_identity": {
            "device": "mps",
            "torch": "2.11.0",
            "transformers": "5.3.0",
            "true_token_id": 9454,
        },
        "laya_model_identity": "laya-manifest",
        "laya_readout_protocol": "typed-label-readout-v5",
    }
    identity = installation_identity()
    installation_path.write_text(json.dumps(identity))
    contract_path.write_text(json.dumps(contract))
    cases = [
        {
            "id": "case",
            "criterion": "Criterion",
            "snippets": [
                {"id": "first", "text": "First"},
                {"id": "second", "text": "Second"},
            ],
        }
    ]
    receipt = {
        "schema_version": 1,
        "status": "complete",
        "attempt_started_ns": 1,
        "study_id": "zerank-study-v1",
        "contract_sha256": study.sha256_file(contract_path),
        "installation_sha256": study.sha256_file(installation_path),
        "model_revision": study.MODEL_REVISION,
        "selection": contract["selection"],
        "execution_attestation": {
            "network_sandbox_profile": study.NETWORK_SANDBOX,
            "zerank_network_outbound_denied": True,
            "zerank_preflight_installation_identity": identity,
            "zerank_postflight_installation_identity": identity,
            "laya_offline_enforced": True,
            "postflight_verified": True,
        },
        "ranker": {
            "status": "ready",
            **contract["worker_identity"],
            "network_outbound_denied": True,
            "offline_environment": {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
            },
            "load_ms": 100.0,
        },
        "cases": [
            {
                "case_id": "case",
                "ranking": [
                    {
                        "snippet_id": "first",
                        "raw_yes_logit": 2.0,
                        "vendor_transformed_relevance": study.transformed_relevance(2.0),
                        "latency_ms": 2.0,
                    },
                    {
                        "snippet_id": "second",
                        "raw_yes_logit": 1.0,
                        "vendor_transformed_relevance": study.transformed_relevance(1.0),
                        "latency_ms": 1.0,
                    },
                ],
                "selected_snippet_ids": ["first", "second"],
                "ranker_peak_rss_bytes": 1024,
                "baseline": laya_receipt(),
                "candidate": laya_receipt(),
            }
        ],
    }
    return contract, cases, receipt


def test_probability_metrics_rejects_out_of_range_input():
    study = load_study()
    with pytest.raises(study.StudyError, match="finite values"):
        study.probability_metrics({"case": 2.0}, {"case": "yes"})


def test_prediction_receipt_rejects_probability_and_selection_mutation(tmp_path, monkeypatch):
    study = load_study()
    contract_path = tmp_path / "contract.json"
    installation_path = tmp_path / "installation.json"
    contract, cases, receipt = valid_prediction_receipt(
        study, contract_path, installation_path
    )
    monkeypatch.setattr(study, "CONTRACT_PATH", contract_path)
    monkeypatch.setattr(study, "INSTALLATION_PATH", installation_path)
    study.validate_prediction_receipt(
        receipt, contract, cases, require_immutable_file=False
    )

    changed_probability = copy.deepcopy(receipt)
    changed_probability["cases"][0]["candidate"]["yes_probability"] = 2.0
    with pytest.raises(study.StudyError, match="probabilities are invalid"):
        study.validate_prediction_receipt(
            changed_probability, contract, cases, require_immutable_file=False
        )

    changed_selection = copy.deepcopy(receipt)
    changed_selection["cases"][0]["selected_snippet_ids"].reverse()
    with pytest.raises(study.StudyError, match="selection does not match"):
        study.validate_prediction_receipt(
            changed_selection, contract, cases, require_immutable_file=False
        )


def test_first_attempt_marker_precedes_worker_start(tmp_path, monkeypatch):
    study = load_study()
    partial = tmp_path / "predictions.partial.json"
    predictions = tmp_path / "predictions.json"
    contract_path = tmp_path / "contract.json"
    installation_path = tmp_path / "installation.json"
    contract_path.write_text("{}")
    identity = installation_identity()
    installation_path.write_text(json.dumps(identity))
    contract = {
        "study_id": "zerank-study-v1",
        "selection": {"query_template": study.RANK_QUERY, "top_k": study.TOP_K},
    }
    monkeypatch.setattr(study, "PARTIAL_PATH", partial)
    monkeypatch.setattr(study, "PREDICTIONS_PATH", predictions)
    monkeypatch.setattr(study, "CONTRACT_PATH", contract_path)
    monkeypatch.setattr(study, "INSTALLATION_PATH", installation_path)
    monkeypatch.setattr(study, "validate_contract", lambda **_kwargs: (contract, []))

    class FailingWorker:
        def __init__(self):
            assert partial.is_file()
            raise study.StudyError("worker failed after custody began")

    monkeypatch.setattr(study, "ZerankProcess", FailingWorker)
    with pytest.raises(study.StudyError, match="custody began"):
        study.run()
    marker = json.loads(partial.read_text())
    assert marker["status"] == "started"
    assert marker["ranker"] is None


@pytest.mark.parametrize("partial", [b"", b"{"])
def test_worker_read_has_a_deadline(monkeypatch, partial):
    study = load_study()
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    writer = os.fdopen(write_fd, "wb", buffering=0)
    process = study.ZerankProcess.__new__(study.ZerankProcess)
    process.process = type("Process", (), {"stdout": reader})()
    process._stdout_buffer = b""
    closed = []
    monkeypatch.setattr(process, "close", lambda **_kwargs: closed.append(True))
    try:
        writer.write(partial)
        with pytest.raises(study.StudyError, match="deadline"):
            process._readline(0.01, "deadline")
        assert closed == [True]
    finally:
        writer.close()
        reader.close()
