from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import json
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "check_release.py"
spec = importlib.util.spec_from_file_location("release_check", TOOL)
assert spec is not None and spec.loader is not None
release_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_check)

PROJECT = b"""\
[project]
name = "veyro-factory"
version = "0.4.0"
[project.scripts]
veyro = "veyro.cli:app"
"""

RELEASE_STATUS = {
    "schema_version": 1,
    "protocol": "veyro-release-status-v1",
    "authority": "artifact_only",
    "production_qualified": False,
    "interfaces": {
        "supported": list(release_check.SUPPORTED_COMMANDS),
        "experimental": list(release_check.EXPERIMENTAL_COMMANDS),
        "unavailable": list(release_check.UNAVAILABLE_COMMANDS),
    },
    "gates": {"G4": {"status": "open"}, "G6": {"status": "open"}},
}
DIST_ROOT = "veyro_factory-0.4.0"
DIST_INFO = f"{DIST_ROOT}.dist-info"
WHEEL_PATH = f"{DIST_INFO}/WHEEL"
RECORD_PATH = f"{DIST_INFO}/RECORD"
DATA_ROOT = f"{DIST_ROOT}.data/data/share/veyro"
CLI_SOURCE = b"raise RuntimeError('artifact code must not execute')\napp = object()\n"
MJS_SOURCE = b"export const checker = true;\n"
OPENCODE_MJS_SOURCE = b"export const opencode = true;\n"
PRIME_AGENT_MJS_SOURCE = b"export const primeAgent = true;\n"
MODEL_PROFILES = b'{"small": {"id": "small"}}\n'
LAYA_PROFILES = b'{"laya": {"id": "laya"}}\n'


def _record_bytes(
    files: dict[str, bytes], edit_rows: Callable[[list[list[str]]], None] | None
) -> bytes:
    rows: list[list[str]] = []
    for name, data in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        rows.append([name, "sha256=" + digest.decode("ascii"), str(len(data))])
    rows.append([RECORD_PATH, "", ""])
    if edit_rows is not None:
        edit_rows(rows)
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue().encode()


def _artifacts(
    root: Path,
    *,
    readme: str = "# Veyro\n",
    wheel_source: bytes = b'__version__ = "0.4.0"\n',
    sdist_source: bytes = b'__version__ = "0.4.0"\n',
    omitted: str | None = None,
    archive_root: str = DIST_ROOT,
    extra_wheel: tuple[str, bytes] | None = None,
    project: bytes = PROJECT,
    wheel_changes: dict[str, bytes | None] | None = None,
    record_edit: Callable[[list[list[str]]], None] | None = None,
    include_record: bool = True,
) -> tuple[Path, Path]:
    source_files = {name: b"" for name in release_check.REQUIRED_SOURCE_FILES}
    source_files.update(
        {
            "README.md": readme.encode(),
            "pyproject.toml": project,
            "release-status.json": json.dumps(RELEASE_STATUS, sort_keys=True).encode(),
            "src/veyro/__init__.py": sdist_source,
            "src/veyro/cli.py": CLI_SOURCE,
            "src/veyro/integrations/checker.mjs": MJS_SOURCE,
            "src/veyro/integrations/opencode.mjs": OPENCODE_MJS_SOURCE,
            "src/veyro/integrations/prime-agent.mjs": PRIME_AGENT_MJS_SOURCE,
            "config/model-profiles.json": MODEL_PROFILES,
            "config/laya-profiles.json": LAYA_PROFILES,
            "config/baselines/evidence.json": b"{}\n",
        }
    )
    if omitted:
        source_files.pop(omitted)

    sdist = root / f"{DIST_ROOT}.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name, data in sorted(source_files.items()):
            member = tarfile.TarInfo(f"{archive_root}/{name}")
            member.size = len(data)
            member.mtime = 0
            archive.addfile(member, io.BytesIO(data))

    wheel_files = {
        "veyro/__init__.py": wheel_source,
        "veyro/cli.py": CLI_SOURCE,
        "veyro/integrations/checker.mjs": MJS_SOURCE,
        "veyro/integrations/opencode.mjs": OPENCODE_MJS_SOURCE,
        "veyro/integrations/prime-agent.mjs": PRIME_AGENT_MJS_SOURCE,
        f"{DATA_ROOT}/model-profiles.json": MODEL_PROFILES,
        f"{DATA_ROOT}/laya-profiles.json": LAYA_PROFILES,
        f"{DIST_INFO}/METADATA": (
            b"Metadata-Version: 2.1\nName: veyro-factory\nVersion: 0.4.0\n"
        ),
        f"{DIST_INFO}/entry_points.txt": b"[console_scripts]\nveyro = veyro.cli:app\n",
        WHEEL_PATH: (
            b"Wheel-Version: 1.0\n"
            b"Generator: release-test\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    if extra_wheel is not None:
        wheel_files[extra_wheel[0]] = extra_wheel[1]
    for name, data in (wheel_changes or {}).items():
        if data is None:
            wheel_files.pop(name, None)
        else:
            wheel_files[name] = data
    if include_record:
        wheel_files[RECORD_PATH] = _record_bytes(wheel_files, record_edit)

    wheel = root / f"{DIST_ROOT}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in sorted(wheel_files.items()):
            archive.writestr(name, data)
    return wheel, sdist


@pytest.fixture
def release_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.mark.parametrize(
    "name,data",
    [
        ("../escape.py", b""),
        ("..\\escape.py", b""),
        ("veyro/./module.py", b""),
        (".audit/private.md", b""),
        (".env", b""),
        (".veyro/events.jsonl", b""),
        ("veyro/__pycache__/module.pyc", b""),
        ("retired/module.py", b""),
        ("veyro/module.py", b"from retired import module"),
    ],
)
def test_release_guard_rejects_private_or_retired_content(name, data):
    with pytest.raises(ValueError):
        release_check.inspect_files({name: data}, "retired")


def test_release_guard_accepts_source_and_example_configuration():
    release_check.inspect_files({"veyro/__init__.py": b"", ".env.example": b""}, "retired")


def test_release_guard_rejects_retired_controller_term():
    retired = b"orchestr" + b"ation"
    with pytest.raises(ValueError, match="Retired controller term"):
        release_check.inspect_files({"README.md": retired}, None)


def test_release_guard_rejects_machine_specific_home_paths():
    home = b"/" + b"Users" + b"/example/project"
    with pytest.raises(ValueError, match="Machine-specific home path"):
        release_check.inspect_files({"evidence.json": home}, None)


def test_source_manifest_is_order_independent_and_content_bound():
    first = {"b.txt": b"two", "a.txt": b"one"}
    second = {"a.txt": b"one", "b.txt": b"two"}
    changed = {"a.txt": b"one", "b.txt": b"three"}
    renamed = {"a.txt": b"one", "c.txt": b"two"}

    assert release_check.source_manifest_sha256(first) == release_check.source_manifest_sha256(
        second
    )
    assert release_check.source_manifest_sha256(first) != release_check.source_manifest_sha256(
        changed
    )
    assert release_check.source_manifest_sha256(first) != release_check.source_manifest_sha256(
        renamed
    )


def test_matching_artifacts_produce_deterministic_artifact_only_receipt(release_root: Path):
    wheel, sdist = _artifacts(release_root)

    first = release_check.check(wheel, sdist)
    second = release_check.check(wheel, sdist)

    assert first == second
    assert first["schema_version"] == 1
    assert first["protocol"] == "veyro-release-artifact-v1"
    assert first["authority"] == "artifact_only"
    assert first["production_qualified"] is False
    assert first["wheel"]["source_payload_matches_sdist"] is True
    assert first["checks"]["unpacked_public_docs"] == "passed"
    assert first["checks"]["wheel_structure"] == "passed"


def test_release_rejects_wheel_code_that_differs_from_sdist(release_root: Path):
    wheel, sdist = _artifacts(release_root, wheel_source=b'__version__ = "changed"\n')

    with pytest.raises(ValueError, match="payload differs"):
        release_check.check(wheel, sdist)


@pytest.mark.parametrize(
    ("wheel_changes", "message"),
    [
        ({"veyro/cli.py": None}, "missing console entry-point module"),
        ({"veyro/cli.py": b"command = object()\n"}, "does not define app"),
    ],
)
def test_release_rejects_missing_console_entry_point_source(
    release_root: Path, wheel_changes: dict[str, bytes | None], message: str
):
    wheel, sdist = _artifacts(release_root, wheel_changes=wheel_changes)

    with pytest.raises(ValueError, match=message):
        release_check.check(wheel, sdist)


@pytest.mark.parametrize(
    ("path", "changed"),
    [
        ("veyro/integrations/checker.mjs", b"export const checker = false;\n"),
        (f"{DATA_ROOT}/model-profiles.json", b'{"changed": true}\n'),
    ],
)
def test_release_rejects_runtime_data_that_differs_from_sdist(
    release_root: Path, path: str, changed: bytes
):
    wheel, sdist = _artifacts(release_root, wheel_changes={path: changed})

    with pytest.raises(ValueError, match="runtime payload differs"):
        release_check.check(wheel, sdist)


@pytest.mark.parametrize(
    ("source_path", "wheel_path"),
    [
        ("src/veyro/integrations/checker.mjs", "veyro/integrations/checker.mjs"),
        ("config/model-profiles.json", f"{DATA_ROOT}/model-profiles.json"),
    ],
)
def test_release_rejects_required_runtime_file_omitted_from_both_artifacts(
    release_root: Path, source_path: str, wheel_path: str
):
    wheel, sdist = _artifacts(
        release_root,
        omitted=source_path,
        wheel_changes={wheel_path: None},
    )

    with pytest.raises(ValueError, match="missing verification inputs"):
        release_check.check(wheel, sdist)


def test_release_rejects_missing_wheel_metadata(release_root: Path):
    wheel, sdist = _artifacts(release_root, wheel_changes={WHEEL_PATH: None})

    with pytest.raises(ValueError, match="canonical .dist-info/WHEEL"):
        release_check.check(wheel, sdist)


@pytest.mark.parametrize(
    ("wheel_metadata", "message"),
    [
        (
            b"Wheel-Version: 1.1\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            "Wheel-Version must be 1.0",
        ),
        (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-any\n",
            "Root-Is-Purelib must be true",
        ),
        (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp311-cp311-macosx_11_0_arm64\n",
            "Tag must be py3-none-any",
        ),
    ],
)
def test_release_rejects_invalid_wheel_metadata(
    release_root: Path, wheel_metadata: bytes, message: str
):
    wheel, sdist = _artifacts(release_root, wheel_changes={WHEEL_PATH: wheel_metadata})

    with pytest.raises(ValueError, match=message):
        release_check.check(wheel, sdist)


def test_release_rejects_missing_record(release_root: Path):
    wheel, sdist = _artifacts(release_root, include_record=False)

    with pytest.raises(ValueError, match="canonical .dist-info/RECORD"):
        release_check.check(wheel, sdist)


@pytest.mark.parametrize(
    ("problem", "message"),
    [
        ("missing", "RECORD coverage differs"),
        ("extra", "RECORD coverage differs"),
        ("duplicate", "RECORD contains a duplicate path"),
    ],
)
def test_release_rejects_invalid_record_coverage(
    release_root: Path, problem: str, message: str
):
    def edit_rows(rows: list[list[str]]) -> None:
        if problem == "missing":
            rows.pop(0)
        elif problem == "extra":
            rows.append(["not-in-wheel.txt", "sha256=" + "A" * 43, "0"])
        else:
            rows.append(rows[0].copy())

    wheel, sdist = _artifacts(release_root, record_edit=edit_rows)

    with pytest.raises(ValueError, match=message):
        release_check.check(wheel, sdist)


@pytest.mark.parametrize(
    ("record_hash", "message"),
    [
        ("sha256=not-url-safe!", "invalid URL-safe sha256 hash"),
        ("sha256=" + "A" * 43, "RECORD hash differs"),
    ],
)
def test_release_rejects_invalid_record_hash(
    release_root: Path, record_hash: str, message: str
):
    def edit_rows(rows: list[list[str]]) -> None:
        next(row for row in rows if row[0] == "veyro/cli.py")[1] = record_hash

    wheel, sdist = _artifacts(release_root, record_edit=edit_rows)

    with pytest.raises(ValueError, match=message):
        release_check.check(wheel, sdist)


@pytest.mark.parametrize(
    ("record_size", "message"),
    [("not-a-size", "invalid size"), ("999", "RECORD size differs")],
)
def test_release_rejects_invalid_record_size(
    release_root: Path, record_size: str, message: str
):
    def edit_rows(rows: list[list[str]]) -> None:
        next(row for row in rows if row[0] == "veyro/cli.py")[2] = record_size

    wheel, sdist = _artifacts(release_root, record_edit=edit_rows)

    with pytest.raises(ValueError, match=message):
        release_check.check(wheel, sdist)


def test_release_rejects_nonempty_record_hash_and_size(release_root: Path):
    def edit_rows(rows: list[list[str]]) -> None:
        record = next(row for row in rows if row[0] == RECORD_PATH)
        record[1:] = ["sha256=" + "A" * 43, "0"]

    wheel, sdist = _artifacts(release_root, record_edit=edit_rows)

    with pytest.raises(ValueError, match="empty hash and size"):
        release_check.check(wheel, sdist)


@pytest.mark.parametrize(
    "extra_name",
    [
        "unexpected.py",
        "veyro/extra.so",
        "veyro/extra.pth",
        "unexpected.mjs",
        f"{DIST_ROOT}.data/scripts/veyro",
    ],
)
def test_release_rejects_unexpected_executable_or_loadable_payload(
    release_root: Path, extra_name: str
):
    wheel, sdist = _artifacts(
        release_root,
        extra_wheel=(extra_name, b"wheel-only payload\n"),
    )

    with pytest.raises(ValueError, match="unexpected executable or loadable"):
        release_check.check(wheel, sdist)


def test_release_rejects_noncanonical_source_archive_identity(release_root: Path):
    wheel, sdist = _artifacts(release_root, archive_root="unrelated-0.4.0")

    with pytest.raises(ValueError, match="archive name or root"):
        release_check.check(wheel, sdist)


def test_release_rejects_renamed_source_archive(release_root: Path):
    wheel, sdist = _artifacts(release_root)
    renamed = sdist.rename(release_root / "renamed.tar.gz")

    with pytest.raises(ValueError, match="archive name or root"):
        release_check.check(wheel, renamed)


def test_release_rejects_missing_gate_file(release_root: Path):
    wheel, sdist = _artifacts(release_root, omitted="GATES.md")

    with pytest.raises(ValueError, match="GATES.md"):
        release_check.check(wheel, sdist)


def test_release_rejects_a_different_self_consistent_distribution(release_root: Path):
    other = PROJECT.replace(b"veyro-factory", b"other-project").replace(
        b"veyro = \"veyro.cli:app\"", b"other = \"other.cli:app\""
    )
    wheel, sdist = _artifacts(release_root, project=other)

    with pytest.raises(ValueError, match="Veyro distribution contract"):
        release_check.check(wheel, sdist)


def test_release_rejects_noncanonical_wheel_filename(release_root: Path):
    wheel, sdist = _artifacts(release_root)
    renamed = wheel.rename(release_root / "veyro_factory-0.4.0-invalid.whl")

    with pytest.raises(ValueError, match="Wheel filename"):
        release_check.check(renamed, sdist)


@pytest.mark.parametrize("target", ["missing.md", ".audit/private.md"])
def test_release_rejects_nonportable_sdist_documentation(release_root: Path, target: str):
    wheel, sdist = _artifacts(release_root, readme=f"# Veyro\n\n[private]({target})\n")

    with pytest.raises(ValueError):
        release_check.check(wheel, sdist)
