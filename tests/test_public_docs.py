"""Keep the public documentation portable, linked, and tied to runtime identities."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from veyro.supervision.checkpoints import (
    AUTHORITATIVE_MODEL_CHECKPOINT,
    AUTHORITATIVE_PROVIDER_ID,
    CHECKPOINT_QUESTIONS,
)

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = [
    ROOT / "README.md",
    *sorted((ROOT / "docs").glob("*.md")),
    *sorted((ROOT / "examples").glob("*.md")),
]


def test_public_document_links_and_anchors_resolve():
    for document in DOCUMENTS:
        text = document.read_text()
        targets = re.findall(r"\]\(([^)]+)\)", text)
        targets += re.findall(r'(?:href|src)="([^"]+)"', text)
        for target in targets:
            url = urlsplit(target)
            if url.scheme or url.netloc:
                continue
            linked = document.parent / unquote(url.path) if url.path else document
            assert linked.exists(), (document.name, target)
            if url.fragment:
                headings = re.findall(r"^#+ (.+)$", linked.read_text(), re.MULTILINE)
                slugs = {
                    re.sub(r"[^a-z0-9 _-]", "", heading.lower()).replace(" ", "-")
                    for heading in headings
                }
                assert unquote(url.fragment) in slugs, (document.name, target)


def test_public_docs_exclude_planning_and_personal_machine_artifacts():
    removed = ["task.md", "docs/product-charter.md", "docs/trustworthy-supervision-plan.md"]
    assert not any((ROOT / name).exists() for name in removed)
    for document in DOCUMENTS:
        text = document.read_text()
        assert not re.search(r"\b(?:SUP|GOV|ARCH|JEV)-[0-9]{3}\b", text), document
        assert not re.search(r"/(?:Users|home)/[A-Za-z0-9_.-]+/|/var/folders/", text), document
        for name in removed:
            assert Path(name).name not in text, document


def test_localjev_showcase_matches_runtime_and_baseline():
    baseline = json.loads((ROOT / "config/baselines/localjev-qwen3-14b.json").read_text())
    readme = (ROOT / "README.md").read_text()
    guide = (ROOT / "docs/localjev.md").read_text()
    assert "localjev" in readme.split("##", 1)[0]
    assert "Qwen3-14B" in readme.split("##", 1)[0]
    for text in (readme, guide):
        assert AUTHORITATIVE_PROVIDER_ID in text
        assert baseline["upstream"]["model"] in text
        assert baseline["upstream"]["quantization"] in text
        assert baseline["provider"]["endpoint"] in text
        assert baseline["provider"]["request_model"] in text
    assert AUTHORITATIVE_MODEL_CHECKPOINT in guide
    assert baseline["upstream"]["digest"] in AUTHORITATIVE_MODEL_CHECKPOINT
    explanation = (ROOT / "docs/why-jev.md").read_text()
    assert all(f"`{question}`" in explanation for question in CHECKPOINT_QUESTIONS)


def test_readme_separates_new_logo_introduction_and_animated_diagram():
    readme = (ROOT / "README.md").read_text()
    assert 'src="docs/assets/veyro-supervision.gif"' in readme
    images = re.findall(r'<img\b[^>]*src="([^"]+)"', readme)
    assert images == ["docs/assets/veyro-relay-logo.svg", "docs/assets/veyro-supervision.gif"]
    logo = readme.index('src="docs/assets/veyro-relay-logo.svg"')
    intro_start = readme.index("Veyro observes Prime Agent")
    intro_end = readme.index("structured events, not terminal scraping.")
    diagram = readme.index('src="docs/assets/veyro-supervision.gif"')
    assert logo < intro_start < intro_end < diagram < readme.index("## Qwen3-14B")
    assert "</p>" in readme[logo:intro_start]
    assert '<p align="center">' in readme[intro_end:diagram]
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


def test_model_comparison_distinguishes_released_and_unmerged_variants():
    baseline = json.loads((ROOT / "config/baselines/localjev-qwen3-14b.json").read_text())
    for name in ("README.md", "docs/qwen-models.md"):
        text = (ROOT / name).read_text()
        assert "Qwen3 4B Instruct" in text
        assert "qwen3:4b-instruct-2507-q4_K_M" in text
        assert "Qwen3 14B" in text
        assert baseline["upstream"]["model"] in text
        assert "unmerged" in text.lower()
        assert "not shipped" in text.lower()
        assert "memory" in text
