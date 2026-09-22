#!/usr/bin/env python3
"""Build development-only repository examples with sandboxed executable labels."""

import argparse
import json
from pathlib import Path

from veyro.repository_dataset import RecipeSet, build_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipes", nargs="+", type=Path)
    parser.add_argument("--reservation", type=Path, required=True)
    parser.add_argument("--exclusions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tasks = [
        task
        for path in args.recipes
        for task in RecipeSet.model_validate_json(path.read_text()).tasks
    ]
    print(
        json.dumps(
            build_dataset(
                tasks,
                json.loads(args.reservation.read_text()),
                set(json.loads(args.exclusions.read_text())),
                args.output,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
