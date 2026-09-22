"""Check finite-label scoring against fixed, independently authored cases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from veyro.local_models import LocalModels, load_profiles
from veyro.local_server import evaluate_local
from veyro.readout import READOUT_PROTOCOL, ReadoutEngine, ReadoutQuestion


def benchmark(profile_id: str, cases: list[dict], output: Path, *, service: bool = False) -> dict:
    assert cases and len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        assert set(case["expected"]) == set(case["questions"])
        for name, expected in case["expected"].items():
            assert expected in {value["name"] for value in case["questions"][name]["outcomes"]}
    profile = load_profiles()[profile_id]
    engine = (
        None
        if service
        else ReadoutEngine(LocalModels().gguf_path(profile), family=profile.model_family)
    )
    report = {
        "protocol": READOUT_PROTOCOL,
        "profile": profile.model_dump(),
        "transport": "service" if service else "direct",
        "runs": [],
    }
    try:
        for case in cases:
            for repeat in range(2):
                items = list(case["questions"].items())
                if repeat:
                    items.reverse()
                try:
                    if service:
                        result = evaluate_local(profile_id, case["state"], dict(items), timeout=30)
                    else:
                        wire = tuple(
                            ReadoutQuestion(
                                name,
                                value["text"],
                                tuple(
                                    (outcome["name"], outcome["description"])
                                    for outcome in value["outcomes"]
                                ),
                            )
                            for name, value in items
                        )
                        result = engine.evaluate(case["state"], wire, timeout=30)
                    assert result["metrics"]["generated_tokens"] == 0
                    scored = {}
                    for name, prediction in result["predictions"].items():
                        probabilities = prediction["probabilities"]
                        index = max(range(len(probabilities)), key=probabilities.__getitem__)
                        chosen = prediction["outcomes"][index]
                        scored[name] = {
                            "chosen": chosen,
                            "expected": case["expected"][name],
                            "correct": chosen == case["expected"][name],
                            "top_probability": probabilities[index],
                        }
                    row = {"id": case["id"], "repeat": repeat, "scored": scored, **result}
                except (RuntimeError, ValueError, TimeoutError) as error:
                    row = {"id": case["id"], "repeat": repeat, "error": str(error), "scored": {}}
                report["runs"].append(row)
                output.write_text(json.dumps(report, indent=2) + "\n")
    finally:
        if engine is not None:
            engine.close()
    scored = [score for run in report["runs"] for score in run["scored"].values()]
    report["summary"] = {
        "correct": sum(score["correct"] for score in scored),
        "total": 2 * sum(len(case["questions"]) for case in cases),
        "errors": sum("error" in run for run in report["runs"]),
        "confident_wrong": sum(
            not score["correct"] and score["top_probability"] >= 0.9 for score in scored
        ),
        "generated_tokens": sum(
            run.get("metrics", {}).get("generated_tokens", 0) for run in report["runs"]
        ),
    }
    output.write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("small", "14b"), required=True)
    parser.add_argument(
        "--cases", type=Path, default=Path("examples/readout-reliability-cases.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--service", action="store_true")
    args = parser.parse_args()
    summary = benchmark(
        args.profile, json.loads(args.cases.read_text()), args.output, service=args.service
    )["summary"]
    print(json.dumps(summary, indent=2))
    raise SystemExit(0 if summary["correct"] == summary["total"] else 1)
