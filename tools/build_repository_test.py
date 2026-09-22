#!/usr/bin/env python3
"""Create reserved internal test cases after a fixed adapter has completed training."""

import argparse
import json
from pathlib import Path

from veyro.repository_dataset import RecipeSet, build_reserved_test


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipes", type=Path)
    parser.add_argument("--study", type=Path, required=True)
    args = parser.parse_args()
    tasks = RecipeSet.model_validate_json(args.recipes.read_text()).tasks
    print(json.dumps(build_reserved_test(tasks, args.study), indent=2))


if __name__ == "__main__":
    main()
