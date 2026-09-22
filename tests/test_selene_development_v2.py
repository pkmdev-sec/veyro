from __future__ import annotations

import importlib.util
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def load_study():
    path = Path(__file__).parents[1] / ".audit" / "selene-development-v2" / "study.py"
    spec = importlib.util.spec_from_file_location("selene_development_v2", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evaluation_receipt(study, *, result: str = "yes", probability: float = 0.8):
    yes_logit = math.log(probability)
    no_logit = math.log(1 - probability)
    score = study._binary_decision_score(yes_logit, no_logit)
    title = result.title()
    return {
        "result": result,
        "reasoning": "The evidence establishes the criterion.",
        "raw_content": (
            f"**Reasoning:** The evidence establishes the criterion.\n\n**Result:** {title}"
        ),
        **score,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "latency_ms": 5.0,
        "peak_rss_bytes": 1024,
    }


def arm(*, balanced: float, positive: float, negative: float):
    return {
        "balanced_accuracy": balanced,
        "positive_accuracy": positive,
        "negative_accuracy": negative,
    }


def test_official_result_parser_is_strict():
    study = load_study()
    content = "**Reasoning:** The evidence establishes the criterion.\n\n**Result:** Yes"
    assert study.parse_result(content) == (
        "yes",
        "The evidence establishes the criterion.",
    )
    with pytest.raises(study.StudyError, match="official result format"):
        study.parse_result("Yes")


def test_forced_choice_score_reads_both_exact_final_token_logits():
    study = load_study()

    class Model:
        n_tokens = 4
        input_ids = [11, 12, 13, 101]
        scores = [[0.0] * 103 for _ in range(4)]

        @staticmethod
        def tokenize(value, **_kwargs):
            return [101] if value == b" Yes" else [102]

    Model.scores[2][101] = 3.0
    Model.scores[2][102] = 1.0
    score = study.forced_choice_score(
        Model(),
        "yes",
        {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
    )
    assert score["score_method"] == study.SCORE_METHOD
    assert score["decision_logits"] == {"yes": 3.0, "no": 1.0}
    assert score["conditional_yes_probability"] == pytest.approx(0.8807970779)
    assert math.exp(score["decision_logprobs"]["yes"]) == pytest.approx(
        score["conditional_yes_probability"]
    )


def test_forced_choice_score_fails_when_decision_position_is_uncertain():
    study = load_study()

    class Model:
        n_tokens = 4
        input_ids = [11, 12, 13, 999]
        scores = [[0.0] * 103 for _ in range(4)]

        @staticmethod
        def tokenize(value, **_kwargs):
            return [101] if value == b" Yes" else [102]

    with pytest.raises(study.StudyError, match="final generated token"):
        study.forced_choice_score(
            Model(),
            "yes",
            {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
        )


def test_prompt_preserves_criterion_and_evidence_and_rejects_fences():
    study = load_study()
    prompt = study.prompt_for("Criterion text", "Evidence text")
    assert "[Criterion text]" in prompt
    assert "Evidence text" in prompt
    with pytest.raises(study.StudyError, match="fence delimiters"):
        study.prompt_for("Criterion", "```injection```")


def test_lexical_selector_is_deterministic_and_length_matched():
    study = load_study()
    case = {
        "criterion": "Atomic save fsyncs a temporary file before rename.",
        "snippets": [
            {"id": "unrelated", "text": "The CLI prints colored headings."},
            {"id": "rename", "text": "rename replaces the destination atomically."},
            {"id": "fsync", "text": "The temporary file is fsynced before rename."},
            {"id": "docs", "text": "The guide promises safe saves."},
        ],
    }
    assert study.lexical_top_k(case) == ["fsync", "rename"]
    assert study.lexical_top_k(case) == study.lexical_top_k(case)


def test_evaluation_receipt_rejects_missing_or_mutated_score():
    study = load_study()
    receipt = evaluation_receipt(study)
    study.validate_evaluation(receipt)

    missing = dict(receipt)
    missing["conditional_yes_probability"] = None
    with pytest.raises(study.StudyError, match="conditional probability"):
        study.validate_evaluation(missing)

    changed = dict(receipt)
    changed["conditional_yes_probability"] = 0.7
    with pytest.raises(study.StudyError, match="exact logits"):
        study.validate_evaluation(changed)


def test_zerank_must_beat_every_baseline_even_when_full_fails():
    study = load_study()
    arms = {
        "full": arm(balanced=0.8, positive=0.8, negative=0.8),
        "lexical_top2": arm(balanced=0.8, positive=0.8, negative=0.8),
        "zerank_top2": arm(balanced=0.8, positive=0.8, negative=0.8),
    }
    selected = study.select_arm(
        arms,
        {"full": False, "lexical_top2": False, "zerank_top2": True},
        min_zerank_gain=0.1,
    )
    assert selected is None


def test_zerank_cannot_win_by_worsening_one_class():
    study = load_study()
    arms = {
        "full": arm(balanced=0.8, positive=1.0, negative=0.6),
        "lexical_top2": arm(balanced=0.8, positive=1.0, negative=0.6),
        "zerank_top2": arm(balanced=0.9, positive=0.8, negative=1.0),
    }
    selected = study.select_arm(
        arms,
        {"full": True, "lexical_top2": True, "zerank_top2": True},
        min_zerank_gain=0.1,
    )
    assert selected == "full"


def test_zerank_wins_only_with_frozen_gain_and_no_class_regression():
    study = load_study()
    arms = {
        "full": arm(balanced=0.7, positive=0.6, negative=0.8),
        "lexical_top2": arm(balanced=0.7, positive=0.6, negative=0.8),
        "zerank_top2": arm(balanced=0.9, positive=0.8, negative=1.0),
    }
    selected = study.select_arm(
        arms,
        {"full": False, "lexical_top2": False, "zerank_top2": True},
        min_zerank_gain=0.1,
    )
    assert selected == "zerank_top2"


def test_false_completion_accounting_does_not_depend_on_confidence_threshold():
    study = load_study()
    predictions = {
        "positive": evaluation_receipt(study, result="yes", probability=0.51),
        "negative": evaluation_receipt(study, result="yes", probability=0.51),
    }
    metrics = study.arm_metrics(predictions, {"positive": "yes", "negative": "no"})
    assert metrics["decision_coverage"] == 1.0
    assert metrics["false_completions"] == 1
    assert metrics["high_confidence_false_completions"] == 0


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
