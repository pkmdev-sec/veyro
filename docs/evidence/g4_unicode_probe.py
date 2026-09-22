#!/usr/bin/env python3
r"""Reproduce the G4 Unicode slug defect against a candidate slug.py.

The original probe hardcoded a disposable canary workspace that no longer exists.
Pass the directory holding the agent-generated slug.py instead.

    .venv/bin/python docs/evidence/g4_unicode_probe.py <dir-with-slug.py>

The defect: a model wrote re.sub(r"[\W_]+", "-", text). Python's \W is
Unicode-aware, so E-acute is a word character and "CAFE-acute" becomes "cafe-acute"
rather than "caf". Case 7 below is the held-out assertion the live verifier does
not check.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

CASES = [
    (" Hello, World! ", "hello-world"),
    ("A___B---C", "a-b-c"),
    ("...", ""),
    ("", ""),
    ("abc123", "abc123"),
    ("a\nb\tc", "a-b-c"),
    ("CAF\u00c9", "caf"),
    ("a  -- !! b", "a-b"),
]


def load(directory: Path):
    path = directory / "slug.py"
    if not path.is_file():
        raise SystemExit(f"No slug.py in {directory}")
    spec = importlib.util.spec_from_file_location("slug_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.slug


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    slug = load(args.directory)
    failures = 0
    for text, expected in CASES:
        got = slug(text)
        ok = got == expected
        failures += not ok
        print(f"{'OK  ' if ok else 'FAIL'} {text!r} -> {got!r} expected {expected!r}")
    print(f"{len(CASES) - failures}/{len(CASES)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
