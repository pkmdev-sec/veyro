#!/usr/bin/env python3
"""Seal complete test predictions before scoring a local training pilot."""

import argparse
import json
from pathlib import Path

from veyro.repository_report import report_study


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", type=Path)
    args = parser.parse_args()
    print(json.dumps(report_study(args.study), indent=2))


if __name__ == "__main__":
    main()
