#!/usr/bin/env python3
"""Validate that public documentation is portable within a release tree."""

from __future__ import annotations

import argparse
import re
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

REMOVED_PUBLIC_FILES = (
    "task.md",
    "docs/product-charter.md",
    "docs/trustworthy-supervision-plan.md",
)


def public_documents(root: Path) -> list[Path]:
    """Return public Markdown documents in stable path order."""

    documents = [root / "README.md"]
    documents.extend((root / "docs").rglob("*.md"))
    documents.extend((root / "examples").glob("*.md"))
    return sorted(
        (path for path in documents if path.is_file()), key=lambda path: path.as_posix()
    )


class _HtmlTargetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.targets: list[str] = []

    def handle_starttag(
        self, _tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.targets.extend(
            value for name, value in attrs if name in {"href", "src"} and value is not None
        )


def _targets(text: str) -> list[str]:
    targets = [
        angle or plain
        for angle, plain in re.findall(r"\]\(\s*(?:<([^>]+)>|([^\s)]+))", text)
    ]
    targets.extend(
        angle or plain
        for angle, plain in re.findall(
            r"^\s{0,3}\[[^]]+\]:\s*(?:<([^>]+)>|(\S+))", text, re.MULTILINE
        )
    )
    html = _HtmlTargetParser()
    html.feed(text)
    targets.extend(html.targets)
    return targets


def _heading_slugs(text: str) -> set[str]:
    return {
        re.sub(r"[^a-z0-9 _-]", "", heading.lower()).replace(" ", "-")
        for heading in re.findall(r"^#+ (.+)$", text, re.MULTILINE)
    }


def _contains_audit_path(path: str) -> bool:
    return ".audit" in PurePosixPath(unquote(path).replace("\\", "/")).parts


def validate_public_documents(root: Path) -> None:
    """Reject nonportable links, private audit links, and machine-local text."""

    root = root.resolve()
    documents = public_documents(root)
    if root / "README.md" not in documents:
        raise ValueError("Public documentation is missing README.md")

    for removed in REMOVED_PUBLIC_FILES:
        if (root / removed).exists():
            raise ValueError(f"Removed planning artifact is public: {removed}")

    for document in documents:
        text = document.read_text(encoding="utf-8")
        relative_document = document.relative_to(root).as_posix()
        if re.search(r"\b(?:SUP|GOV|ARCH|JEV)-[0-9]{3}\b", text):
            raise ValueError(f"Planning identifier in public document: {relative_document}")
        if re.search(r"/(?:Users|home)/[A-Za-z0-9_.-]+/|/var/folders/", text):
            raise ValueError(f"Machine-specific path in public document: {relative_document}")
        for removed in REMOVED_PUBLIC_FILES:
            if Path(removed).name in text:
                raise ValueError(
                    f"Removed planning artifact referenced by {relative_document}: {removed}"
                )

        for target in _targets(text):
            url = urlsplit(target)
            if _contains_audit_path(url.path):
                raise ValueError(f"Private .audit link in {relative_document}: {target}")
            if url.netloc:
                if url.scheme not in {"", "http", "https"}:
                    raise ValueError(
                        f"Unsupported URL scheme in {relative_document}: {target}"
                    )
                continue
            if url.scheme:
                if url.scheme not in {"http", "https", "mailto"}:
                    raise ValueError(
                        f"Unsupported URL scheme in {relative_document}: {target}"
                    )
                continue

            linked = (document.parent / unquote(url.path)).resolve() if url.path else document
            if not linked.is_relative_to(root):
                raise ValueError(f"Link escapes release tree in {relative_document}: {target}")
            if not linked.exists():
                raise ValueError(f"Missing link target in {relative_document}: {target}")
            if url.fragment:
                if not linked.is_file():
                    raise ValueError(
                        f"Anchor target is not a file in {relative_document}: {target}"
                    )
                fragment = unquote(url.fragment)
                if fragment not in _heading_slugs(linked.read_text(encoding="utf-8")):
                    raise ValueError(f"Missing anchor in {relative_document}: {target}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root", nargs="?", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    validate_public_documents(args.root)


if __name__ == "__main__":
    main()
