#!/usr/bin/env python3
"""Check the stored Jev article source against the claims Veyro makes about it.

The stored copy at docs/sources/building-a-harness-with-jev.json is the durable
evidence for Gate G0. This tool proves the claims in that gate still match the
stored text, so a reviewer never has to trust prose alone.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "sources" / "building-a-harness-with-jev.json"

# Phrases the article must contain. Each one backs a design decision Veyro states.
REQUIRED = [
    "Choice: Pick from a set of options.",
    "Score: Rate an input against ordered levels",
    "Noul: Answer a yes-or-no question.",
    "you can ask multiple questions about the same state in one request",
    "evaluate every question in a request in parallel",
    "reinforcement learning for calibrated decisions (RLCD)",
    "TypeSafeClassifier",
    "ModelRouterMiddleware",
    "AutoModeMiddleware",
    "jev-latest",
]

# Terms the article must NOT contain. Their absence is why Veyro must not cite
# this article as the source of its supervisor, checkpoint, or stopping design.
FORBIDDEN = ["supervisor", "supervise", "checkpoint", "rubric", "stop condition"]


def corpus(article: dict) -> str:
    content = article["content"]
    texts = [block["text"] for block in content["blocks"]]
    code = [
        entity["value"]["data"]["markdown"]
        for entity in content["entityMap"]
        if entity["value"]["type"] == "MARKDOWN"
    ]
    return "\n".join(texts + code)


def check(path: Path = SOURCE) -> dict[str, object]:
    document = json.loads(path.read_text())
    article = document["article"]
    text = corpus(article)
    lowered = text.lower()

    missing = [phrase for phrase in REQUIRED if phrase not in text]
    if missing:
        raise ValueError("Stored article is missing required phrases: " + "; ".join(missing))

    present = [term for term in FORBIDDEN if term in lowered]
    if present:
        raise ValueError(
            "Stored article unexpectedly contains supervisor vocabulary: " + "; ".join(present)
        )

    if article["title"] != "Building a Harness with Jev":
        raise ValueError("Unexpected article title: " + article["title"])
    if article["author"]["screen_name"] != "sydneyrunkle":
        raise ValueError("Unexpected article author")

    return {
        "status": "passed",
        "title": article["title"],
        "author": article["author"]["screen_name"],
        "article_id": article["id"],
        "blocks": len(article["content"]["blocks"]),
        "code_blocks": sum(
            1 for entity in article["content"]["entityMap"] if entity["value"]["type"] == "MARKDOWN"
        ),
        "required_phrases": len(REQUIRED),
        "forbidden_terms_absent": len(FORBIDDEN),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    args = parser.parse_args()
    print(json.dumps(check(args.source), sort_keys=True))


if __name__ == "__main__":
    main()
