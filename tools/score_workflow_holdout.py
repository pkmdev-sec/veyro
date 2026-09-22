#!/usr/bin/env python3
"""Score frozen evaluator cases and emit labelled calibration examples."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from veyro.evaluators import CorrectionRecord, EvaluatorDefinition
from veyro.local_evaluation import run_evaluator


def score(
    definition_path: Path,
    labels_path: Path,
    profile: str,
    output: Path,
    state_dir: Path | None,
    timeout: float,
) -> None:
    definition = EvaluatorDefinition.load(definition_path)
    raw_records = json.loads(labels_path.read_text())
    records = [CorrectionRecord.model_validate(item) for item in raw_records]
    examples = {name: [] for name in definition.questions}
    for record in records:
        record.validate_for(definition)
        result = run_evaluator(
            definition,
            record.case,
            profile,
            holdout_ids=[item.case.id for item in records],
            state_dir=state_dir,
            timeout=timeout,
        )
        for question, label in record.labels.items():
            examples[question].append(
                {
                    "id": record.case.id,
                    "probabilities": result["results"][question]["probabilities"],
                    "label": label,
                }
            )
    if len(examples) == 1 and output.suffix == ".json":
        only = next(iter(examples.values()))
        output.write_text(json.dumps(only, indent=2) + "\n")
        outputs = [str(output)]
    else:
        output.mkdir(parents=True, exist_ok=True)
        outputs = []
        for question, values in examples.items():
            path = output / f"{question}.json"
            path.write_text(json.dumps(values, indent=2) + "\n")
            outputs.append(str(path))
    print(json.dumps({"examples": sum(map(len, examples.values())), "outputs": outputs}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("definition", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--profile", default="14b")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    score(
        args.definition,
        args.labels,
        args.profile,
        args.output,
        args.state_dir,
        args.timeout,
    )


if __name__ == "__main__":
    main()
