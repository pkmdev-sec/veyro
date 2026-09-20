from __future__ import annotations

import runpy
from pathlib import Path

import pytest

BENCHMARK = runpy.run_path(str(Path(__file__).parents[1] / "tools/benchmark_native_judge.py"))


def test_confusion_counts_and_undefined_precision():
    metrics = BENCHMARK["confusion"]([(True, True), (False, True), (False, False), (True, False)])
    assert metrics["accuracy"] == metrics["precision"] == metrics["recall"] == 0.5
    assert metrics["false_accepts"] == metrics["false_rejects"] == 1
    assert BENCHMARK["confusion"]([(False, False)])["precision"] is None


def test_errors_are_not_valid_calibration_probabilities():
    rows = [
        {
            "case_id": "one",
            "repeat": 0,
            "expected": {"a": True, "b": False},
            "result": {"status": "failed", "scores": {"a": 1, "b": 0.2}},
            "latency_ms": 10,
        },
        {
            "case_id": "two",
            "repeat": 0,
            "expected": {"a": True, "b": True},
            "result": {"status": "error"},
            "latency_ms": 20,
        },
    ]
    result = BENCHMARK["summarize"](rows, 0.9)
    assert result["model_errors"] == 1
    assert result["brier_samples"] == 2
    assert result["brier_score"] == pytest.approx(0.02)
    assert result["completion"]["false_rejects"] == 1
    assert result["criterion"]["samples"] == 2
    assert result["per_criterion"]["a"]["samples"] == 1
    assert sum(b["samples"] for b in result["calibration_bins"]) == 2
    assert result["p95_ms"] == 20
