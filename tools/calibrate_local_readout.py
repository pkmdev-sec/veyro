"""Fit per-question temperatures from a labelled benchmark with a case-level split.

This demonstrates the calibration workflow. Partitioning an already-used development
benchmark does not turn it into an untouched or production holdout.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from veyro.evaluators import CalibrationExample, DistributionCalibrator, EvaluatorDefinition
from veyro.local_evaluation import calibration_schema, model_identity
from veyro.local_models import ModelProfile
from veyro.readout import READOUT_PROTOCOL


def calibrate_report(report: dict, split: dict, directory: Path) -> dict:
    if report["protocol"] != READOUT_PROTOCOL:
        raise ValueError("benchmark readout protocol is stale")
    profile = ModelProfile.model_validate(report["profile"])
    definition = EvaluatorDefinition.model_validate(report["rubric"])
    training, holdout = split["training_ids"], split["holdout_ids"]
    if not training or not holdout or set(training) & set(holdout):
        raise ValueError("require nonempty disjoint case-level partitions")
    if len(training) != len(set(training)) or len(holdout) != len(set(holdout)):
        raise ValueError("split IDs must be unique")
    first_runs = {run["id"]: run for run in report["runs"] if run["repetition"] == 0}
    if set(training) | set(holdout) != set(first_runs):
        raise ValueError("split must account for every benchmark case exactly once")
    directory.mkdir(parents=True, exist_ok=True)
    definition.save(directory / "evaluator.json")
    result = {
        "profile": profile.id,
        "model": model_identity(profile),
        "data_role": "partitioned synthetic development study; NOT an untouched holdout",
        "model_weights_trained": False,
        "method": "post-hoc per-question temperature",
        "training_ids": training,
        "holdout_ids": holdout,
        "questions": {},
    }
    for name in definition.questions:

        def example(case_id, question=name):
            run = first_runs[case_id]
            prediction = run["predictions"][question]
            return CalibrationExample(
                id=case_id,
                probabilities=dict(
                    zip(prediction["outcomes"], prediction["probabilities"], strict=True)
                ),
                label="yes" if run["expected"][question] else "no",
            )

        artifact = DistributionCalibrator.fit(
            [example(case_id) for case_id in training],
            holdout_ids=holdout,
            model=model_identity(profile),
            profile=profile.id,
            schema_sha256=calibration_schema(definition, name),
        )
        artifact.save(directory / f"{name}.json")
        result["questions"][name] = {
            "inverse_temperature": artifact.inverse_temperature,
            "training_log_loss_before": artifact.training_log_loss_before,
            "training_log_loss_after": artifact.training_log_loss_after,
            "heldout_partition": artifact.evaluate_holdout(
                [example(case_id) for case_id in holdout]
            ),
        }
    (directory / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("--split", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            calibrate_report(
                json.loads(args.benchmark.read_text()),
                json.loads(args.split.read_text()),
                args.output_dir,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
