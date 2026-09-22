#!/usr/bin/env python3
"""Evaluate the optional native judge on synthetic labels; no model calls without --live."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from veyro.native_judge import JudgeConfig, evaluate
from veyro.veyro.base import VeyroModelError
from veyro.veyro.jev import JevVeyroModel

ROOT = Path(__file__).resolve().parents[1]


def confusion(pairs: list[tuple[bool, bool]]) -> dict:
    tp = sum(expected and actual for expected, actual in pairs)
    fp = sum(not expected and actual for expected, actual in pairs)
    tn = sum(not expected and not actual for expected, actual in pairs)
    fn = sum(expected and not actual for expected, actual in pairs)
    return {
        "samples": len(pairs),
        "true_accepts": tp,
        "false_accepts": fp,
        "true_rejects": tn,
        "false_rejects": fn,
        "accuracy": (tp + tn) / len(pairs) if pairs else None,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "false_acceptance_rate": fp / (fp + tn) if fp + tn else None,
        "false_rejection_rate": fn / (fn + tp) if fn + tp else None,
    }


def summarize(rows: list[dict], threshold: float) -> dict:
    labels = []
    predictions = []
    pairs = []
    for row in rows:
        expected = row["expected"]
        result = row["result"]
        pairs.append((all(expected.values()), result["status"] == "passed"))
        for name, score in result.get("scores", {}).items():
            labels.append(int(expected[name]))
            predictions.append(score)
    bins = []
    for start in range(5):
        lower, upper = start / 5, (start + 1) / 5
        members = [
            (label, score)
            for label, score in zip(labels, predictions, strict=True)
            if lower <= score and (score < upper or upper == 1 and score == 1)
        ]
        bins.append(
            {
                "lower": lower,
                "upper": upper,
                "samples": len(members),
                "mean_score": statistics.mean(p[1] for p in members) if members else None,
                "positive_fraction": statistics.mean(p[0] for p in members) if members else None,
            }
        )
    latency = sorted(row["latency_ms"] for row in rows)
    baseline = [row for row in rows if row["repeat"] == 0]
    drift = []
    by_id = {row["case_id"]: row for row in baseline}
    for row in rows:
        if row["repeat"] == 0:
            continue
        first = by_id[row["case_id"]]["result"].get("scores", {})
        drift.extend(
            abs(score - first[name])
            for name, score in row["result"].get("scores", {}).items()
            if name in first
        )
    per_criterion = {}
    for name in rows[0]["expected"]:
        named_pairs = [
            (
                bool(row["expected"][name]),
                row["result"].get("scores", {}).get(name, -1) >= threshold,
            )
            for row in rows
            if name in row["result"].get("scores", {})
        ]
        per_criterion[name] = confusion(named_pairs)
    return {
        "completion": confusion(pairs),
        "per_criterion": per_criterion,
        "criterion": confusion(
            [
                (bool(label), score >= threshold)
                for label, score in zip(labels, predictions, strict=True)
            ]
        ),
        "model_errors": sum(row["result"]["status"] == "error" for row in rows),
        "uncertain_results": sum(row["result"]["status"] == "uncertain" for row in rows),
        "brier_score": statistics.mean(
            (s - y) ** 2 for y, s in zip(labels, predictions, strict=True)
        )
        if labels
        else None,
        "brier_samples": len(labels),
        "calibration_bins": bins,
        "p50_ms": latency[math.ceil(len(latency) * 0.5) - 1],
        "p95_ms": latency[math.ceil(len(latency) * 0.95) - 1],
        "mean_repeated_score_delta": statistics.mean(drift) if drift else None,
        "max_repeated_score_delta": max(drift) if drift else None,
        "without_semantic_gate": confusion([(expected, True) for expected, _ in pairs]),
    }


async def run(dataset: dict, repeats: int) -> dict:
    config = JudgeConfig.model_validate(dataset["rubric"])
    provider = config.provider
    key = os.getenv(provider.api_key_env)
    if not key:
        raise ValueError(f"missing {provider.api_key_env}; configure the selected judge provider")
    model = JevVeyroModel(
        provider_id=provider.provider_id,
        base_url=str(provider.base_url),
        api_key=key,
        model=provider.request_model,
        checkpoint=provider.checkpoint,
        timeout_seconds=provider.timeout_seconds,
        max_state_characters=provider.max_state_characters,
        state_format=provider.state_format,
        strict_scores=True,
    )
    rows = []
    try:
        for repeat in range(repeats):
            criteria = (
                dict(reversed(list(config.criteria.items()))) if repeat % 2 else config.criteria
            )
            selected = config.model_copy(update={"criteria": criteria})
            for case in dataset["cases"]:
                started = time.monotonic()
                try:
                    result = await evaluate(
                        selected,
                        dataset["task"],
                        case["artifacts"],
                        [{"exit_code": 0, "timed_out": False}],
                        model,
                    )
                except VeyroModelError:
                    result = {"status": "error", "error": "judge_model_error"}
                rows.append(
                    {
                        "case_id": case["id"],
                        "repeat": repeat,
                        "criterion_order": list(criteria),
                        "expected": case["expected"],
                        "result": result,
                        "latency_ms": (time.monotonic() - started) * 1000,
                    }
                )
                print(f"{case['id']} repeat={repeat}: {result['status']}", flush=True)
    finally:
        await model.close()
    return {
        "schema_version": 1,
        "timestamp": datetime.now(UTC).isoformat(),
        "dataset_version": dataset["dataset_version"],
        "label_source": dataset["label_source"],
        "pass_threshold": config.pass_threshold,
        "repeats": repeats,
        "metrics": summarize(rows, config.pass_threshold),
        "results": rows,
        "limits": [
            "Synthetic labels authored before model calls, not human-calibrated workflow labels.",
            "Fixtures assume shallow executable checks passed; this isolates semantic judgment.",
            "Model errors count as rejected completions; they have no Brier score.",
            "Repeats reverse criteria; score drift combines stochasticity and order effects.",
            "Tiny calibration bins do not establish calibrated production probabilities.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--dataset", type=Path, default=ROOT / "examples/native-judge-cases.json")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("--repeats must be in [1, 10]")
    dataset = json.loads(args.dataset.read_text())
    load_dotenv(ROOT / ".env", override=False)
    if args.live:
        result = asyncio.run(run(dataset, args.repeats))
    else:
        result = {
            "mode": "preview",
            "model_calls": 0,
            "dataset_version": dataset["dataset_version"],
            "cases": len(dataset["cases"]),
            "label_source": dataset["label_source"],
            "accuracy": None,
            "instruction": "Use --live to measure actual judge accuracy.",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if args.live and result["metrics"]["model_errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
