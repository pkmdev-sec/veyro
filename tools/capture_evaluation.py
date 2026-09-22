#!/usr/bin/env python3
"""Freeze a development capture contract or collect one auditable prediction."""

import argparse
import json
from pathlib import Path

from veyro.evaluation_capture import (
    CaptureContract,
    CheckEvidence,
    TaskContract,
    capture,
    make_contract,
    write_new,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("task", type=Path)
    freeze.add_argument("output", type=Path)
    freeze.add_argument("--profile", required=True)
    freeze.add_argument("--partition", choices=["development", "calibration"], required=True)
    collect = commands.add_parser("capture")
    collect.add_argument("contract", type=Path)
    collect.add_argument("candidate", type=Path, help="JSON candidate content, not a file path")
    collect.add_argument("evidence", type=Path)
    collect.add_argument("destination", type=Path, help="New per-case attempt directory")
    collect.add_argument("--case-id", required=True)
    collect.add_argument("--state-dir", type=Path)
    collect.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    if args.command == "freeze":
        task = TaskContract.model_validate_json(args.task.read_text())
        contract = make_contract(task, args.profile, args.partition)
        write_new(args.output, contract.model_dump(mode="json"))
        print(json.dumps({"status": "frozen", "path": str(args.output)}))
    else:
        capture(
            CaptureContract.model_validate_json(args.contract.read_text()),
            args.case_id,
            json.loads(args.candidate.read_text()),
            CheckEvidence.model_validate_json(args.evidence.read_text()),
            args.destination,
            state_dir=args.state_dir,
            timeout=args.timeout,
        )
        print(json.dumps({"status": "captured", "path": str(args.destination)}))


if __name__ == "__main__":
    main()
