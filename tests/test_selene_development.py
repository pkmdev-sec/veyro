from __future__ import annotations

import importlib.util
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def load_study():
    path = Path(__file__).parents[1] / ".audit" / "selene-development-v1" / "study.py"
    spec = importlib.util.spec_from_file_location("selene_development", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_official_result_parser_is_strict():
    study = load_study()
    content = "**Reasoning:** The evidence establishes the criterion.\n\n**Result:** Yes"
    assert study.parse_result(content) == (
        "yes",
        "The evidence establishes the criterion.",
    )
    with pytest.raises(study.StudyError, match="official result format"):
        study.parse_result("Yes")


def test_conditional_probability_requires_both_answer_tokens():
    study = load_study()
    content = [
        {
            "token": " Yes",
            "top_logprobs": [
                {"token": " Yes", "logprob": -0.1},
                {"token": " No", "logprob": -2.1},
            ],
        }
    ]
    probability, alternatives = study.conditional_yes_probability(content, "yes")
    assert probability == pytest.approx(0.8807970779)
    assert alternatives == {"yes": -0.1, "no": -2.1}
    assert study.conditional_yes_probability(
        [{"token": " Yes", "top_logprobs": [{"token": " Yes", "logprob": -0.1}]}],
        "yes",
    )[0] is None


def test_prompt_preserves_criterion_and_evidence_and_rejects_fences():
    study = load_study()
    prompt = study.prompt_for("Criterion text", "Evidence text")
    assert "[Criterion text]" in prompt
    assert "Evidence text" in prompt
    with pytest.raises(study.StudyError, match="fence delimiters"):
        study.prompt_for("Criterion", "```injection```")


def test_evaluation_receipt_rejects_parsed_result_mutation():
    study = load_study()
    receipt = {
        "result": "yes",
        "reasoning": "The evidence establishes the criterion.",
        "raw_content": (
            "**Reasoning:** The evidence establishes the criterion.\n\n**Result:** Yes"
        ),
        "conditional_yes_probability": 0.8,
        "decision_logprobs": {"yes": -0.1, "no": -1.4862943611198907},
        "logprob_token_count": 10,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "latency_ms": 5.0,
        "peak_rss_bytes": 1024,
    }
    study.validate_evaluation(receipt)
    receipt["result"] = "no"
    with pytest.raises(study.StudyError, match="parsed result"):
        study.validate_evaluation(receipt)

    receipt["result"] = "yes"
    receipt["conditional_yes_probability"] = None
    with pytest.raises(study.StudyError, match="suppresses"):
        study.validate_evaluation(receipt)


def test_zerank_arm_cannot_win_by_worsening_one_class():
    study = load_study()
    arms = {
        "full": {
            "balanced_accuracy": 0.8,
            "positive_accuracy": 1.0,
            "negative_accuracy": 0.6,
        },
        "zerank_top2": {
            "balanced_accuracy": 0.9,
            "positive_accuracy": 0.8,
            "negative_accuracy": 1.0,
        },
    }
    selected = study.select_arm(
        arms, {"full": True, "zerank_top2": True}, min_zerank_gain=0.1
    )
    assert selected == "full"


def test_attempt_claim_is_exclusive_under_concurrency(tmp_path):
    study = load_study()
    path = tmp_path / "attempt.json"
    barrier = threading.Barrier(2)

    def claim(index):
        barrier.wait()
        try:
            study.claim_json(path, {"index": index})
            return "claimed"
        except study.StudyError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, range(2)))
    assert sorted(outcomes) == ["claimed", "rejected"]


def test_first_attempt_marker_precedes_model_start(tmp_path, monkeypatch):
    study = load_study()
    partial = tmp_path / "predictions.partial.json"
    predictions = tmp_path / "predictions.json"
    contract_path = tmp_path / "contract.json"
    contract_path.write_text("{}")
    zerank_path = tmp_path / "zerank.json"
    zerank_path.write_text(json.dumps({"cases": []}))
    monkeypatch.setattr(study, "PARTIAL_PATH", partial)
    monkeypatch.setattr(study, "PREDICTIONS_PATH", predictions)
    monkeypatch.setattr(study, "CONTRACT_PATH", contract_path)
    monkeypatch.setattr(study, "ZERANK_PREDICTIONS_PATH", zerank_path)
    monkeypatch.setattr(study, "validate_contract", lambda **_kwargs: ({}, []))

    class FailingProcess:
        def __init__(self):
            assert partial.is_file()
            raise study.StudyError("model startup failed after custody began")

    monkeypatch.setattr(study, "SeleneProcess", FailingProcess)
    with pytest.raises(study.StudyError, match="custody began"):
        study.run()
    marker = json.loads(partial.read_text())
    assert marker["status"] == "started"
    assert marker["ready"] is None
