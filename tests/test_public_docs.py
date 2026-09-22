"""Keep the public documentation portable, linked, and tied to runtime identities."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from veyro.supervision.checkpoints import (
    AUTHORITATIVE_MODEL_CHECKPOINT,
    AUTHORITATIVE_PROVIDER_ID,
    CHECKPOINT_QUESTIONS,
)

ROOT = Path(__file__).resolve().parents[1]

TOOL = ROOT / "tools" / "check_public_docs.py"
spec = importlib.util.spec_from_file_location("public_docs_check", TOOL)
assert spec is not None and spec.loader is not None
public_docs_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(public_docs_check)
validate_public_documents = public_docs_check.validate_public_documents
DOCUMENTS = [
    ROOT / "README.md",
    *sorted((ROOT / "docs").rglob("*.md")),
    *sorted((ROOT / "examples").glob("*.md")),
]


def test_public_document_links_and_anchors_resolve():
    validate_public_documents(ROOT)


@pytest.mark.parametrize(
    "target",
    ["missing.md", ".audit/private-report.md", "../.audit/private-report.md"],
)
def test_public_document_validator_rejects_nonportable_links(tmp_path: Path, target: str):
    (tmp_path / "README.md").write_text(f"[private evidence]({target})\n")
    audit = tmp_path / ".audit"
    audit.mkdir()
    (audit / "private-report.md").write_text("private\n")

    with pytest.raises(ValueError):
        validate_public_documents(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "[private][evidence]\n\n[evidence]: .audit/private-report.md\n",
        "<a href='.audit/private-report.md'>private</a>\n",
        "<img src=missing-image.png>\n",
        "[private](file:///etc/passwd)\n",
        "<a href=javascript:alert(1)>unsafe</a>\n",
    ],
)
def test_public_document_validator_covers_reference_and_html_links(
    tmp_path: Path, body: str
):
    (tmp_path / "README.md").write_text(body)

    with pytest.raises(ValueError):
        validate_public_documents(tmp_path)


def test_public_taxonomy_matches_machine_readable_release_status():
    status = json.loads((ROOT / "release-status.json").read_text())
    readme = (ROOT / "README.md").read_text()
    scope = (ROOT / "docs/release-scope.md").read_text()

    assert status["authority"] == "artifact_only"
    assert status["production_qualified"] is False
    assert status["gates"]["G4"]["status"] == "open"
    assert status["gates"]["G6"]["status"] == "open"
    for category, commands in status["interfaces"].items():
        assert category.capitalize() in readme
        assert category.capitalize() in scope
        for command in commands:
            assert f"`veyro {command}`" in readme
            assert f"`{command}`" in scope


def test_localjev_showcase_matches_runtime_and_baseline():
    baseline = json.loads((ROOT / "config/baselines/localjev-qwen3-14b.json").read_text())
    readme = (ROOT / "README.md").read_text()
    guide = (ROOT / "docs/localjev.md").read_text()
    assert "localjev" in readme.split("##", 1)[0]
    assert "Qwen3-14B" in readme.split("##", 1)[0]
    text = guide
    assert AUTHORITATIVE_PROVIDER_ID in text
    assert baseline["upstream"]["model"] in text
    assert baseline["upstream"]["quantization"] in text
    assert baseline["provider"]["endpoint"] in text
    assert baseline["provider"]["request_model"] in text
    assert AUTHORITATIVE_MODEL_CHECKPOINT in guide
    assert baseline["upstream"]["digest"] in AUTHORITATIVE_MODEL_CHECKPOINT
    explanation = (ROOT / "docs/why-jev.md").read_text()
    assert all(f"`{question}`" in explanation for question in CHECKPOINT_QUESTIONS)


def test_readme_visuals_are_accessible_and_ordered():
    readme = (ROOT / "README.md").read_text()
    image_tags = re.findall(r'<img\b[^>]*>', readme)
    images = [re.search(r'src="([^"]+)"', tag).group(1) for tag in image_tags]
    assert images == [
        "docs/assets/veyro-relay-logo.svg",
        "https://github.com/pkmdev-sec/veyro/actions/workflows/ci.yml/badge.svg",
        "https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white",
        "https://img.shields.io/badge/License-MIT-93dec4",
        "docs/assets/veyro-supervision.gif",
        "docs/assets/veyro-task-readout.png",
    ]
    assert all(re.search(r'alt="[^"]+"', tag) for tag in image_tags)
    assert readme.index("docs/assets/veyro-relay-logo.svg") < readme.index(
        "docs/assets/veyro-supervision.gif"
    ) < readme.index("docs/assets/veyro-task-readout.png")
    assert "```mermaid" not in readme
    for document in DOCUMENTS:
        text = document.read_text()
        links = re.findall(r'<a\b[^>]*href="([^"]+)"', text)
        links += re.findall(r"(?<!!)\[[^\]]*\]\(([^)]+)\)", text)
        for target in links:
            path = Path(urlsplit(target).path)
            assert not (
                path.name.startswith("veyro-")
                and "logo" in path.stem
                and path.suffix in {".png", ".svg"}
            ), (
                document.name,
                target,
            )


def test_readme_evaluator_example_renders_without_inference():
    from veyro.evaluators import EvaluationCase, EvaluatorDefinition, build_evaluation_request

    definition = EvaluatorDefinition.load(ROOT / "examples/evaluator-definition.json")
    case = EvaluationCase.model_validate_json((ROOT / "examples/evaluator-case.json").read_text())
    request = build_evaluation_request(definition, case)
    assert request["state"]["case"] == "Task: What is 2 + 3?\nAnswer: 5\nReference: 5"
    assert set(request["questions"]) == {"correct", "route", "quality"}


def test_retired_controller_term_is_absent_from_repository_text():
    retired = "orchestr" + "at"
    roots = [ROOT / name for name in ("src", "tests", "tools", "docs")]
    files = [
        path
        for directory in roots
        for path in directory.rglob("*")
        if path.suffix in {".py", ".md", ".mjs", ".toml"}
    ]
    files += [ROOT / name for name in ("README.md", "GATES.md", "PLAN.md", "pyproject.toml")]
    offenders = [
        str(path.relative_to(ROOT))
        for path in files
        if path.is_file() and retired in path.read_text().lower()
    ]
    assert offenders == []


def test_dual_model_harness_roles_are_documented_from_runtime_profiles():
    profiles = json.loads((ROOT / "config/model-profiles.json").read_text())
    laya = json.loads((ROOT / "config/laya-profiles.json").read_text())["laya"]
    readme = (ROOT / "README.md").read_text()
    assert profiles["coder30"]["ollama_model"] in readme
    assert laya["checkpoint"] in readme

    detailed_guides = [
        (ROOT / "docs/qwen-models.md").read_text(),
        (ROOT / "docs/local-harness.md").read_text(),
    ]
    for text in detailed_guides:
        assert profiles["coder30"]["ollama_model"] in text
        assert laya["checkpoint"] in text
        assert "--coding-profile coder30" in text
        assert "--evaluation-profile laya" in text
    role_guide = detailed_guides[0]
    assert "Executable checks remain authoritative" in role_guide
    assert "G4 remains open" in role_guide
    assert "G6 remains open" in role_guide


def test_live_model_docs_do_not_claim_the_implemented_profiles_are_unmerged():
    for name in ("README.md", "docs/qwen-models.md", "docs/theory.md"):
        text = (ROOT / name).read_text().lower()
        assert "not shipped" not in text
        assert "unmerged" not in text
