from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "check_release.py"
spec = importlib.util.spec_from_file_location("release_check", TOOL)
assert spec is not None and spec.loader is not None
release_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_check)


@pytest.mark.parametrize(
    "name,data",
    [
        ("../escape.py", b""),
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
