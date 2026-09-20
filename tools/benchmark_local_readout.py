"""Reproduce local readout quality, cache reuse and shape-sensitive latency.

The supplied slug cases are development regressions, not an untouched holdout.
Run sequentially across profiles to avoid competing for GPU/memory resources.
"""

from __future__ import annotations

import argparse
import json
import platform
import resource
import statistics
import time
from pathlib import Path

from veyro.evaluators import EvaluationCase
from veyro.local_evaluation import wire_questions
from veyro.local_models import LocalModels, load_profiles
from veyro.native_judge import JudgeConfig, native_definition
from veyro.readout import READOUT_PROTOCOL, ReadoutEngine, ReadoutQuestion


def distribution(values: list[float]) -> dict:
    ordered = sorted(values)

    def percentile(fraction):
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    return {
        "count": len(values),
        "median_ms": statistics.median(values),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
    }


def benchmark(profile_id: str, dataset: dict, repetitions: int, output: Path) -> dict:
    profile = load_profiles()[profile_id]
    model_path = LocalModels().gguf_path(profile)
    definition = native_definition(JudgeConfig.model_validate(dataset["rubric"]))
    wire = wire_questions(definition)
    questions = tuple(
        ReadoutQuestion(
            name,
            value["text"],
            tuple((outcome["name"], outcome["description"]) for outcome in value["outcomes"]),
        )
        for name, value in wire.items()
    )
    started = time.monotonic()
    engine = ReadoutEngine(model_path, family=profile.model_family)
    report = {
        "protocol": READOUT_PROTOCOL,
        "profile": profile.model_dump(mode="json"),
        "hardware": {"system": platform.system(), "machine": platform.machine()},
        "model_load_ms": (time.monotonic() - started) * 1000,
        "dataset": dataset["dataset_version"],
        "dataset_role": "synthetic development regression; NOT production holdout",
        "rubric": definition.model_dump(mode="json"),
        "runs": [],
        "shape_runs": [],
    }

    def save():
        output.write_text(json.dumps(report, indent=2) + "\n")

    try:
        for case in dataset["cases"]:
            live = EvaluationCase(
                id=case["id"],
                input={"task": dataset["task"], "checks": []},
                output=case["artifacts"],
            )
            state = {"case": definition.preview(live), "few_shot_examples": []}
            for repetition in range(repetitions):
                ordered = questions if repetition % 2 == 0 else tuple(reversed(questions))
                result = engine.evaluate(state, ordered)
                report["runs"].append(
                    {
                        "id": case["id"],
                        "repetition": repetition,
                        "expected": case["expected"],
                        **result,
                    }
                )
                save()
        for count in (1, 2, 8):
            for padding in (0, 4000):
                state = {"artifact": "def add(a, b): return a + b", "context": "note " * padding}
                shape_questions = tuple(
                    ReadoutQuestion(
                        f"q{i}",
                        "Does add return the sum of its arguments?",
                        (("no", "Not established"), ("yes", "Established")),
                    )
                    for i in range(count)
                )
                for repetition in range(2):
                    result = engine.evaluate(state, shape_questions)
                    report["shape_runs"].append(
                        {
                            "kind": "context_questions",
                            "count": count,
                            "padding_words": padding,
                            "repetition": repetition,
                            **result,
                        }
                    )
                    save()
        for count in (2, 8, 32, 128):
            question = ReadoutQuestion(
                "choice",
                "Which number equals the answer?",
                tuple((str(i), f"The answer equals {i}.") for i in range(count)),
            )
            for repetition in range(2):
                result = engine.evaluate({"answer": 1}, (question,))
                report["shape_runs"].append(
                    {"kind": "outcomes", "count": count, "repetition": repetition, **result}
                )
                save()
    finally:
        engine.close()
    completions, expected_completions, criterion_errors = [], [], []
    binary_errors = []
    threshold = dataset["rubric"]["pass_threshold"]
    max_repeat_delta = 0.0
    prior = {}
    for run in report["runs"]:
        scores = {
            name: prediction["probabilities"][prediction["outcomes"].index("yes")]
            for name, prediction in run["predictions"].items()
        }
        completions.append(all(value >= threshold for value in scores.values()))
        expected_completions.append(all(run["expected"].values()))
        for name, score in scores.items():
            expected = run["expected"][name]
            criterion_errors.append((score >= 0.5) != expected)
            binary_errors.append((score - expected) ** 2)
            key = (run["id"], name)
            if key in prior:
                max_repeat_delta = max(max_repeat_delta, abs(score - prior[key]))
            prior[key] = score
    pairs = list(zip(completions, expected_completions, strict=True))
    positives = sum(completions)
    report["summary"] = {
        "completion_correct": sum(actual == expected for actual, expected in pairs),
        "completion_total": len(pairs),
        "false_acceptances": sum(actual and not expected for actual, expected in pairs),
        "negative_cases": sum(not expected for expected in expected_completions),
        "acceptance_precision": (sum(a and e for a, e in pairs) / positives if positives else None),
        "criterion_correct": len(criterion_errors) - sum(criterion_errors),
        "criterion_total": len(criterion_errors),
        "binary_brier": statistics.mean(binary_errors),
        "max_repeat_order_score_delta": max_repeat_delta,
        "all_latency": distribution([r["metrics"]["latency_ms"] for r in report["runs"]]),
        "warm_prefix_latency": distribution(
            [r["metrics"]["latency_ms"] for r in report["runs"] if r["metrics"]["prefix_cache_hit"]]
        ),
        "generated_tokens": sum(r["metrics"]["generated_tokens"] for r in report["runs"]),
        "peak_process_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1 if platform.system() == "Darwin" else 1024),
    }
    save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("small", "14b"), required=True)
    parser.add_argument("--cases", type=Path, default=Path("examples/native-judge-cases.json"))
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repetitions < 2:
        parser.error("at least two repetitions are needed to measure prefix reuse")
    report = benchmark(
        args.profile, json.loads(args.cases.read_text()), args.repetitions, args.output
    )
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
