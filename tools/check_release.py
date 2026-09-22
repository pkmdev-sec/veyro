#!/usr/bin/env python3
"""Qualify wheel and sdist contents without granting semantic production authority."""

from __future__ import annotations

import argparse
import ast
import base64
import configparser
import csv
import hashlib
import importlib.util
import io
import json
import re
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

_PUBLIC_DOCS_PATH = Path(__file__).with_name("check_public_docs.py")
_PUBLIC_DOCS_SPEC = importlib.util.spec_from_file_location(
    "_veyro_check_public_docs", _PUBLIC_DOCS_PATH
)
if _PUBLIC_DOCS_SPEC is None or _PUBLIC_DOCS_SPEC.loader is None:
    raise RuntimeError("cannot load public-document validator")
_PUBLIC_DOCS_MODULE = importlib.util.module_from_spec(_PUBLIC_DOCS_SPEC)
_PUBLIC_DOCS_SPEC.loader.exec_module(_PUBLIC_DOCS_MODULE)
validate_public_documents = _PUBLIC_DOCS_MODULE.validate_public_documents


SUPPORTED_COMMANDS = ("agents", "sessions", "attach", "supervise")
EXPERIMENTAL_COMMANDS = (
    "agent",
    "local",
    "evaluator",
    "selene",
    "import-jeff-weights",
)
UNAVAILABLE_COMMANDS = ("run", "demo", "inspect", "runs")
EXPECTED_DISTRIBUTION = "veyro-factory"
EXPECTED_SCRIPTS = {"veyro": "veyro.cli:app"}
PRIVATE_ARCHIVE_PARTS = {
    ".git",
    ".audit",
    ".env",
    ".token",
    ".venv",
    ".veyro",
    "__pycache__",
}

REQUIRED_SOURCE_FILES = {
    "README.md",
    "pyproject.toml",
    "LICENSE",
    "GATES.md",
    "MANIFEST.in",
    "release-status.json",
    ".env.example",
    "src/veyro/__init__.py",
    "src/veyro/cli.py",
    "src/veyro/integrations/checker.mjs",
    "src/veyro/integrations/opencode.mjs",
    "src/veyro/integrations/prime-agent.mjs",
    "config/model-profiles.json",
    "config/laya-profiles.json",
    "docs/assets/veyro-logo.svg",
    "docs/assets/veyro-logo.png",
    "docs/assets/veyro-logo-animated.gif",
    "docs/assets/veyro-supervision.gif",
    "docs/assets/veyro-supervision.png",
    "docs/assets/veyro-task-readout.png",
    "docs/release-scope.md",
    "docs/qwen-models.md",
    "tools/check_release.py",
    "tools/check_public_docs.py",
    "tools/generate_supervision_diagram.py",
    "examples/evaluator-case.json",
    "docs/assets/veyro-relay-logo.svg",
    "tools/generate_header_logo.py",
    "docs/assets/veyro-logo-animated-poster.png",
    "tools/generate_brand_assets.py",
    "tests/conftest.py",
    "docs/localjev.md",
    "docs/local-harness.md",
    "docs/sources/building-a-harness-with-jev.md",
    "docs/sources/building-a-harness-with-jev.json",
    "docs/evidence/local-final-regression.log",
    "docs/evidence/g4_unicode_probe.py",
    "docs/evidence/g4-prompt-experiment/README.md",
    "docs/evidence/g4-prompt-experiment/small-round1.json",
    "docs/evidence/g4-prompt-experiment/small-round2.json",
    "docs/evidence/g4-prompt-experiment/variants.json",
    "docs/evidence/g4-prompt-experiment/prompt_probe.py",
    "docs/evidence/g4-prompt-experiment/14b-corrected.json",
    "examples/assess_localjev.py",
    "examples/failed-verification.json",
    "config/baselines/localjev-qwen3-14b.json",
}


def inspect_files(files: dict[str, bytes], retired_name: str | None) -> None:
    private = PRIVATE_ARCHIVE_PARTS
    retired = retired_name.lower().encode() if retired_name else None
    for name, data in files.items():
        path = PurePosixPath(name)
        if (
            "\\" in name
            or path.as_posix() != name
            or path.is_absolute()
            or ".." in path.parts
            or private.intersection(path.parts)
        ):
            raise ValueError("Unsafe or private archive path: " + name)
        if path.suffix in {".pyc", ".pyo"}:
            raise ValueError("Bytecode cache in archive: " + name)
        if retired and (retired in name.lower().encode() or retired in data.lower()):
            raise ValueError("Retired name in archive: " + name)
        if b"orchestr" + b"at" in data.lower():
            raise ValueError("Retired controller term in archive: " + name)
        private_homes = (b"/" + b"Users" + b"/", b"/" + b"home" + b"/")
        if any(prefix in data for prefix in private_homes):
            raise ValueError("Machine-specific home path in archive: " + name)


def source_manifest_sha256(files: dict[str, bytes]) -> str:
    """Hash a canonical manifest of every source path, size, and content digest."""

    digest = hashlib.sha256()
    for name in sorted(files):
        row = [name, len(files[name]), hashlib.sha256(files[name]).hexdigest()]
        digest.update(json.dumps(row, separators=(",", ":"), ensure_ascii=True).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_wheel(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            member_name = member.filename.removesuffix("/")
            member_path = PurePosixPath(member_name)
            if (
                "\\" in member_name
                or member_path.is_absolute()
                or ".." in member_path.parts
                or PRIVATE_ARCHIVE_PARTS.intersection(member_path.parts)
            ):
                raise ValueError("Unsafe or private wheel path: " + member.filename)
            if member.is_dir():
                continue
            if member.filename in files:
                raise ValueError("Duplicate wheel path: " + member.filename)
            files[member.filename] = archive.read(member)
    return files


def _read_sdist(path: Path, retired_name: str | None) -> tuple[str, dict[str, bytes]]:
    source_files: dict[str, bytes] = {}
    prefixes: set[str] = set()
    with tarfile.open(path, "r:*") as archive:
        for member in archive.getmembers():
            member_path = PurePosixPath(member.name)
            if (
                "\\" in member.name
                or member_path.is_absolute()
                or ".." in member_path.parts
                or PRIVATE_ARCHIVE_PARTS.intersection(member_path.parts)
            ):
                raise ValueError("Unsafe or private source archive path: " + member.name)
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError("Nonregular source archive member: " + member.name)
            prefix, separator, relative = member.name.partition("/")
            if not separator or not relative or prefix in {".", ".."}:
                raise ValueError("Missing source archive root")
            if retired_name and retired_name.lower() in member.name.lower():
                raise ValueError("Retired name in source archive path: " + member.name)
            prefixes.add(prefix)
            if relative in source_files:
                raise ValueError("Duplicate source archive path: " + relative)
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Unreadable source archive member: " + member.name)
            source_files[relative] = stream.read()
    if len(prefixes) != 1:
        raise ValueError("Expected one source archive root")
    return prefixes.pop(), source_files


def _validate_runtime_payload(
    wheel_files: dict[str, bytes], source_files: dict[str, bytes], expected_root: str
) -> None:
    expected_payload: dict[str, bytes] = {}
    for name, data in source_files.items():
        path = PurePosixPath(name)
        if name.startswith("src/veyro/") and path.suffix == ".py":
            wheel_name = name.removeprefix("src/")
        elif path.parent == PurePosixPath("src/veyro/integrations") and path.suffix == ".mjs":
            wheel_name = name.removeprefix("src/")
        elif name in {"config/model-profiles.json", "config/laya-profiles.json"}:
            wheel_name = f"{expected_root}.data/data/share/veyro/{path.name}"
        else:
            continue
        expected_payload[wheel_name] = data

    dist_info_prefix = f"{expected_root}.dist-info/"
    loadable_suffixes = {
        ".cjs",
        ".dll",
        ".dylib",
        ".exe",
        ".js",
        ".json",
        ".mjs",
        ".node",
        ".pth",
        ".py",
        ".pyc",
        ".pyd",
        ".pyo",
        ".so",
        ".wasm",
    }
    unexpected = sorted(
        name
        for name in wheel_files
        if name not in expected_payload
        and not name.startswith(dist_info_prefix)
        and (
            PurePosixPath(name).suffix.lower() in loadable_suffixes
            or name.startswith(f"{expected_root}.data/scripts/")
            or name.startswith(f"{expected_root}.data/platlib/")
        )
    )
    if unexpected:
        raise ValueError(
            "Wheel contains unexpected executable or loadable payload: " + ", ".join(unexpected)
        )

    wheel_payload_names = {
        name
        for name in wheel_files
        if (
            (name.startswith("veyro/") and PurePosixPath(name).suffix == ".py")
            or (
                PurePosixPath(name).parent == PurePosixPath("veyro/integrations")
                and PurePosixPath(name).suffix == ".mjs"
            )
            or (
                PurePosixPath(name).parent
                == PurePosixPath(f"{expected_root}.data/data/share/veyro")
                and PurePosixPath(name).suffix == ".json"
            )
        )
    }
    if wheel_payload_names != expected_payload.keys():
        missing_from_wheel = sorted(expected_payload.keys() - wheel_payload_names)
        missing_from_sdist = sorted(wheel_payload_names - expected_payload.keys())
        raise ValueError(
            "Wheel and sdist runtime payload paths differ: "
            f"missing from wheel={missing_from_wheel}, missing from sdist={missing_from_sdist}"
        )
    for name in sorted(expected_payload):
        if wheel_files[name] != expected_payload[name]:
            raise ValueError("Wheel runtime payload differs from sdist source: " + name)


def _target_binds_name(target: ast.expr, name: str) -> bool:
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, (ast.List, ast.Tuple)):
        return any(_target_binds_name(item, name) for item in target.elts)
    return False


def _validate_entry_point_module(wheel_files: dict[str, bytes]) -> None:
    module_path = "veyro/cli.py"
    source = wheel_files.get(module_path)
    if source is None:
        raise ValueError("Wheel is missing console entry-point module: " + module_path)
    try:
        tree = ast.parse(source.decode("utf-8"), filename=module_path)
    except (SyntaxError, UnicodeDecodeError) as error:
        raise ValueError("Wheel console entry-point module is not valid Python") from error

    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if statement.name == "app":
                return
        elif isinstance(statement, ast.Assign):
            if any(_target_binds_name(target, "app") for target in statement.targets):
                return
        elif isinstance(statement, ast.AnnAssign):
            if statement.value is not None and _target_binds_name(statement.target, "app"):
                return
        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            if any(
                (alias.asname or alias.name.partition(".")[0]) == "app"
                for alias in statement.names
            ):
                return
    raise ValueError("Wheel console entry-point module does not define app")


def _validate_wheel_structure(wheel_files: dict[str, bytes], expected_root: str) -> None:
    dist_info = f"{expected_root}.dist-info"
    foreign_dist_info = sorted(
        name
        for name in wheel_files
        if PurePosixPath(name).parts[0].endswith(".dist-info")
        and PurePosixPath(name).parts[0] != dist_info
    )
    if foreign_dist_info:
        raise ValueError("Wheel contains a noncanonical metadata directory")

    wheel_path = f"{dist_info}/WHEEL"
    wheel_names = sorted(name for name in wheel_files if name.endswith(".dist-info/WHEEL"))
    if wheel_names != [wheel_path]:
        raise ValueError("Wheel must contain exactly one canonical .dist-info/WHEEL")
    wheel_metadata = BytesParser().parsebytes(wheel_files[wheel_path])
    required_headers = {
        "Wheel-Version": "1.0",
        "Root-Is-Purelib": "true",
        "Tag": "py3-none-any",
    }
    for header, expected in required_headers.items():
        if wheel_metadata.get_all(header, []) != [expected]:
            raise ValueError(f"Wheel {header} must be {expected}")

    record_path = f"{dist_info}/RECORD"
    record_names = sorted(name for name in wheel_files if name.endswith(".dist-info/RECORD"))
    if record_names != [record_path]:
        raise ValueError("Wheel must contain exactly one canonical .dist-info/RECORD")
    try:
        rows = csv.reader(
            io.StringIO(wheel_files[record_path].decode("utf-8"), newline=""), strict=True
        )
        records: dict[str, tuple[str, str]] = {}
        for row in rows:
            if len(row) != 3 or not row[0]:
                raise ValueError("Wheel RECORD must contain three fields per row")
            name, encoded_hash, size = row
            if name in records:
                raise ValueError("Wheel RECORD contains a duplicate path: " + name)
            records[name] = (encoded_hash, size)
    except (csv.Error, UnicodeDecodeError) as error:
        raise ValueError("Wheel RECORD is not valid UTF-8 CSV") from error

    if records.keys() != wheel_files.keys():
        missing = sorted(wheel_files.keys() - records.keys())
        extra = sorted(records.keys() - wheel_files.keys())
        raise ValueError(
            f"Wheel RECORD coverage differs from wheel members: missing={missing}, extra={extra}"
        )

    for name in sorted(wheel_files):
        encoded_hash, size = records[name]
        if name == record_path:
            if encoded_hash or size:
                raise ValueError("Wheel RECORD row must have empty hash and size fields")
            continue
        if re.fullmatch(r"sha256=[A-Za-z0-9_-]{43}", encoded_hash) is None:
            raise ValueError("Wheel RECORD contains an invalid URL-safe sha256 hash: " + name)
        expected_hash = base64.urlsafe_b64encode(hashlib.sha256(wheel_files[name]).digest())
        if encoded_hash != "sha256=" + expected_hash.rstrip(b"=").decode("ascii"):
            raise ValueError("Wheel RECORD hash differs from wheel member: " + name)
        if re.fullmatch(r"0|[1-9][0-9]*", size) is None:
            raise ValueError("Wheel RECORD contains an invalid size: " + name)
        if int(size) != len(wheel_files[name]):
            raise ValueError("Wheel RECORD size differs from wheel member: " + name)


def _validate_release_status(data: bytes) -> None:
    status = json.loads(data)
    if (
        status.get("schema_version") != 1
        or status.get("protocol") != "veyro-release-status-v1"
    ):
        raise ValueError("Unsupported release status schema")
    if status.get("production_qualified") is not False:
        raise ValueError("Release status must not claim production qualification")
    if status.get("authority") != "artifact_only":
        raise ValueError("Release status authority must be artifact_only")
    interfaces = status.get("interfaces", {})
    expected = {
        "supported": list(SUPPORTED_COMMANDS),
        "experimental": list(EXPERIMENTAL_COMMANDS),
        "unavailable": list(UNAVAILABLE_COMMANDS),
    }
    if interfaces != expected:
        raise ValueError("Release status command taxonomy differs from the public CLI contract")
    gates = status.get("gates", {})
    if (
        gates.get("G4", {}).get("status") != "open"
        or gates.get("G6", {}).get("status") != "open"
    ):
        raise ValueError("Release status must keep G4 and G6 open")


def _validate_materialized_docs(archive_root: str, source_files: dict[str, bytes]) -> None:
    with tempfile.TemporaryDirectory() as directory:
        materialized_root = Path(directory) / archive_root
        for name, data in source_files.items():
            destination = materialized_root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        validate_public_documents(materialized_root)


def check(wheel: Path, sdist: Path, retired_name: str | None = None) -> dict[str, object]:
    wheel_files = _read_wheel(wheel)
    archive_root, source_files = _read_sdist(sdist, retired_name)

    inspect_files(wheel_files, retired_name)
    inspect_files(source_files, retired_name)

    missing = sorted(REQUIRED_SOURCE_FILES - source_files.keys())
    if missing:
        raise ValueError(
            "Source distribution is missing verification inputs: " + ", ".join(missing)
        )
    project = tomllib.loads(source_files["pyproject.toml"].decode())["project"]
    if (
        project.get("name") != EXPECTED_DISTRIBUTION
        or project.get("scripts") != EXPECTED_SCRIPTS
    ):
        raise ValueError("Source project differs from the Veyro distribution contract")
    normalized_name = re.sub(r"[-_.]+", "_", project["name"]).lower()
    expected_root = f"{normalized_name}-{project['version']}"
    if archive_root != expected_root or sdist.name != f"{expected_root}.tar.gz":
        raise ValueError("Source archive name or root differs from project identity")
    if wheel.name != f"{expected_root}-py3-none-any.whl":
        raise ValueError("Wheel filename differs from project identity")
    _validate_wheel_structure(wheel_files, expected_root)

    expected_metadata = f"{expected_root}.dist-info/METADATA"
    metadata_names = [name for name in wheel_files if name.endswith(".dist-info/METADATA")]
    if metadata_names != [expected_metadata]:
        raise ValueError("Wheel metadata directory differs from project identity")
    metadata = BytesParser().parsebytes(wheel_files[expected_metadata])
    if (metadata["Name"], metadata["Version"]) != (
        project["name"],
        project["version"],
    ):
        raise ValueError("Wheel identity differs from pyproject")

    entry_path = metadata_names[0].removesuffix("METADATA") + "entry_points.txt"
    if entry_path not in wheel_files:
        raise ValueError("Wheel is missing console entry points")
    entries = configparser.ConfigParser()
    entries.read_file(io.StringIO(wheel_files[entry_path].decode()))
    if (
        "console_scripts" not in entries
        or dict(entries["console_scripts"]) != project["scripts"]
    ):
        raise ValueError("Wheel entry points differ from pyproject")

    _validate_release_status(source_files["release-status.json"])
    _validate_entry_point_module(wheel_files)
    _validate_runtime_payload(wheel_files, source_files, expected_root)
    _validate_materialized_docs(archive_root, source_files)

    return {
        "schema_version": 1,
        "protocol": "veyro-release-artifact-v1",
        "authority": "artifact_only",
        "production_qualified": False,
        "status": "passed",
        "distribution": project["name"],
        "version": project["version"],
        "source": {
            "archive_root": archive_root,
            "sdist_sha256": file_sha256(sdist),
            "manifest_sha256": source_manifest_sha256(source_files),
            "files": len(source_files),
        },
        "wheel": {
            "sha256": file_sha256(wheel),
            "files": len(wheel_files),
            "source_payload_matches_sdist": True,
        },
        "checks": {
            "archive_safety": "passed",
            "metadata": "passed",
            "entry_points": "passed",
            "wheel_structure": "passed",
            "required_files": "passed",
            "release_status": "passed",
            "source_payload": "passed",
            "unpacked_public_docs": "passed",
        },
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
