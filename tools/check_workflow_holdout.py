#!/usr/bin/env python3
"""Validate frozen, executable-labelled native-workflow holdout artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

CASES = {
    " Hello, World! ": "hello-world",
    "A___B---C": "a-b-c",
    "...": "",
    "": "",
    "abc123": "abc123",
    "a\nb\tc": "a-b-c",
    "CAFÉ": "caf",
    "a  -- !! b": "a-b",
}

RUNNER = r"""
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("slug", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
cases = json.loads(sys.argv[2])
actual = {value: module.slug(value) for value in cases}
print(json.dumps(actual, sort_keys=True))
"""


def _load(path: Path):
    return json.loads(path.read_text())


def _expected_label(record: dict) -> str:
    return (
        "yes"
        if record["phase"] == "completed"
        and record["assertions_passed"] == record["assertions_total"] == len(CASES)
        and record["verifier_unchanged"]
        and record["adapter_error"] is None
        and record["native_cleanup_success"]
        else "no"
    )


def _assertions_passed(artifact: Path) -> int:
    result = subprocess.run(
        [sys.executable, "-I", "-c", RUNNER, str(artifact), json.dumps(CASES)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode:
        return 0
    actual = json.loads(result.stdout)
    return sum(actual[value] == expected for value, expected in CASES.items())


def check(root: Path) -> None:
    manifest = _load(root / "manifest.json")
    labels = {item["case"]["id"]: item for item in _load(root / "labels.json")}
    ids: set[str] = set()
    hashes: set[str] = set()
    for record in manifest["cases"]:
        case_id = record["id"]
        digest = record["artifact_sha256"]
        if case_id in ids or digest in hashes:
            raise ValueError("holdout IDs and artifact hashes must be unique")
        ids.add(case_id)
        hashes.add(digest)
        artifact = root / record["artifact"]
        content = artifact.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError(f"artifact hash drift: {case_id}")
        label = labels[case_id]
        if label["case"]["output"] != {"slug.py": content.decode()}:
            raise ValueError(f"case does not contain exact frozen artifact: {case_id}")
        expected = _expected_label(record)
        if record["label"] != expected or label["labels"] != {"workflow_accepted": expected}:
            raise ValueError(f"label drift: {case_id}")
        report_data = _load(Path(record["report"]))
        report = report_data.get("result", report_data)
        checks = {
            "assertions_passed": report.get("assertions_passed"),
            "assertions_total": report.get("assertions_total"),
            "phase": report.get("phase"),
            "verifier_unchanged": report.get("verifier_unchanged"),
            "adapter_error": report.get("adapter_error"),
            "native_cleanup_success": bool(report.get("native_cleanup", {}).get("success")),
        }
        if any(record[name] != value for name, value in checks.items()):
            raise ValueError(f"report provenance drift: {case_id}")
        actual_passed = _assertions_passed(artifact)
        if actual_passed != record["assertions_passed"]:
            raise ValueError(f"executable label drift: {case_id}")
    if set(labels) != ids:
        raise ValueError("labels must cover exactly the frozen holdout")
    result = {
        "cases": len(ids),
        "yes": sum(item["label"] == "yes" for item in manifest["cases"]),
        "no": sum(item["label"] == "no" for item in manifest["cases"]),
        "status": "passed",
    }
    print(json.dumps(result))


def check_development(root: Path, source_path: Path) -> None:
    source = _load(source_path)
    source_cases = {item["id"]: item for item in source["cases"]}
    manifest = _load(root / "manifest.json")
    labels = {item["case"]["id"]: item for item in _load(root / "labels.json")}
    for record in manifest["cases"]:
        item = source_cases[record["source_case"]]
        content = item["artifacts"]["slug.py"]
        if hashlib.sha256(content.encode()).hexdigest() != record["artifact_sha256"]:
            raise ValueError(f"development artifact drift: {record['id']}")
        case = labels[record["id"]]
        if case["case"]["output"] != item["artifacts"]:
            raise ValueError(f"development case drift: {record['id']}")
        artifact = root / f".{record['source_case']}.py"
        artifact.write_text(content)
        try:
            expected = "yes" if _assertions_passed(artifact) == len(CASES) else "no"
        finally:
            artifact.unlink()
        if record["label"] != expected or case["labels"] != {"workflow_accepted": expected}:
            raise ValueError(f"development label drift: {record['id']}")
    print(json.dumps({"development_cases": len(manifest["cases"]), "status": "passed"}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--development", type=Path)
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    check(args.root)
    if args.development:
        if args.source is None:
            parser.error("--development requires --source")
        check_development(args.development, args.source)


if __name__ == "__main__":
    main()
