#!/usr/bin/env python3
"""Validate release names, metadata, archive paths, and private-file exclusions."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import json
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def inspect_files(files: dict[str, bytes], retired_name: str | None) -> None:
    private = {".git", ".env", ".token", ".venv", ".veyro", "__pycache__"}
    retired = retired_name.lower().encode() if retired_name else None
    for name, data in files.items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or private.intersection(path.parts):
            raise ValueError("Unsafe or private archive path: " + name)
        if path.suffix in {".pyc", ".pyo"}:
            raise ValueError("Bytecode cache in archive: " + name)
        if retired and (retired in name.lower().encode() or retired in data.lower()):
            raise ValueError("Retired name in archive: " + name)


def check(wheel: Path, sdist: Path, retired_name: str | None = None) -> dict[str, object]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    with zipfile.ZipFile(wheel) as archive:
        wheel_files = {name: archive.read(name) for name in archive.namelist()}
    with tarfile.open(sdist, "r:gz") as archive:
        source_files = {}
        prefixes = set()
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError("Nonregular source archive member")
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe source archive path")
            if retired_name and retired_name.lower() in member.name.lower():
                raise ValueError("Retired name in source archive path")
            prefix, separator, relative = member.name.partition("/")
            if not separator:
                raise ValueError("Missing source archive root")
            prefixes.add(prefix)
            stream = archive.extractfile(member)
            assert stream is not None
            source_files[relative] = stream.read()
        if len(prefixes) != 1:
            raise ValueError("Multiple source archive roots")
    inspect_files(wheel_files, retired_name)
    inspect_files(source_files, retired_name)
    metadata_names = [name for name in wheel_files if name.endswith(".dist-info/METADATA")]
    if len(metadata_names) != 1:
        raise ValueError("Expected one wheel distribution")
    metadata = BytesParser().parsebytes(wheel_files[metadata_names[0]])
    if (metadata["Name"], metadata["Version"]) != (project["name"], project["version"]):
        raise ValueError("Wheel identity differs from pyproject")
    entry_path = metadata_names[0].removesuffix("METADATA") + "entry_points.txt"
    entries = configparser.ConfigParser()
    entries.read_file(io.StringIO(wheel_files[entry_path].decode()))
    if dict(entries["console_scripts"]) != project["scripts"]:
        raise ValueError("Wheel entry points differ from pyproject")
    source_project = tomllib.loads(source_files["pyproject.toml"].decode())["project"]
    for field in ("name", "version", "scripts"):
        if source_project[field] != project[field]:
            raise ValueError("Source distribution metadata mismatch")
    required = {
        "README.md",
        ".env.example",
        "src/veyro/__init__.py",
        "docs/assets/veyro-logo.svg",
        "docs/assets/veyro-logo.png",
        "docs/assets/veyro-logo-animated.gif",
        "docs/assets/veyro-supervision.gif",
        "docs/assets/veyro-supervision.png",
        "docs/release-scope.md",
        "docs/qwen-models.md",
        "tools/generate_supervision_diagram.py",
        "docs/assets/veyro-relay-logo.svg",
        "tools/generate_header_logo.py",
        "docs/assets/veyro-logo-animated-poster.png",
        "tools/generate_brand_assets.py",
        "tests/conftest.py",
        "docs/localjev.md",
        "examples/assess_localjev.py",
        "examples/failed-verification.json",
        "config/baselines/localjev-qwen3-14b.json",
    }
    if not required <= source_files.keys():
        raise ValueError("Source distribution is missing verification inputs")
    return {
        "status": "passed",
        "distribution": project["name"],
        "version": project["version"],
        "wheel_files": len(wheel_files),
        "sdist_files": len(source_files),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "sdist_sha256": hashlib.sha256(sdist.read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("sdist", type=Path)
    parser.add_argument("--retired-name")
    args = parser.parse_args()
    print(json.dumps(check(args.wheel, args.sdist, args.retired_name), sort_keys=True))


if __name__ == "__main__":
    main()
