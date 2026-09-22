from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from veyro.grounding import GroundingResponse
from veyro.selene_calibration import BinaryPrediction
from veyro.selene_training import _claim_json


def test_grounding_result_parser_is_strict():
    payload = {
        "schema_version": 1,
        "protocol": "veyro-grounding-v1",
        "case_id": "parser-case",
        "clauses": [
            {
                "clause_id": "criterion",
                "status": "established",
                "evidence_ids": ["implementation"],
                "rationale": "The implementation establishes the criterion.",
            }
        ],
        "result": "yes",
    }

    parsed = GroundingResponse.model_validate_json(json.dumps(payload))
    assert parsed.result == "yes"
    assert parsed.clauses[0].rationale == "The implementation establishes the criterion."

    payload["result"] = "Yes"
    with pytest.raises(ValidationError):
        GroundingResponse.model_validate_json(json.dumps(payload))


def test_binary_prediction_requires_both_exact_decision_logits():
    prediction = BinaryPrediction(
        case_id="probability-case",
        case_sha256="a" * 64,
        model_identity="selene-test-model",
        yes_logit=-0.1,
        no_logit=-2.1,
        latency_ms=1,
        peak_memory_bytes=1024,
    )
    assert prediction.probability == pytest.approx(0.8807970779)

    with pytest.raises(ValidationError, match="both be present"):
        BinaryPrediction(
            case_id="probability-case",
            case_sha256="a" * 64,
            model_identity="selene-test-model",
            yes_logit=-0.1,
            no_logit=None,
            latency_ms=1,
            peak_memory_bytes=1024,
        )


def test_attempt_claim_is_exclusive_under_concurrency(tmp_path):
    path = tmp_path / "attempt.json"
    barrier = threading.Barrier(2)

    def claim(index: int) -> str:
        barrier.wait()
        try:
            _claim_json(path, {"index": index})
            return "claimed"
        except FileExistsError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, range(2)))

    assert sorted(outcomes) == ["claimed", "rejected"]
    assert json.loads(path.read_text())["index"] in {0, 1}
