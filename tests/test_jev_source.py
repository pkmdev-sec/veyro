"""Verify the stored Jev article source and the claims Veyro makes about it.

No network access: these tests read the durable copy committed under docs/sources/.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from check_jev_source import SOURCE, check, corpus  # noqa: E402


@pytest.fixture
def document():
    return json.loads(SOURCE.read_text())


def write(tmp_path, document):
    path = tmp_path / "source.json"
    path.write_text(json.dumps(document))
    return path


def test_stored_source_matches_our_claims():
    result = check()
    assert result["status"] == "passed"
    assert result["title"] == "Building a Harness with Jev"
    assert result["author"] == "sydneyrunkle"
    assert result["blocks"] == 51
    assert result["code_blocks"] == 5


def test_article_names_only_the_three_documented_components(document):
    text = corpus(document["article"])
    for component in ("TypeSafeClassifier", "ModelRouterMiddleware", "AutoModeMiddleware"):
        assert component in text


def test_article_is_not_a_supervisor_specification(document):
    """The absence of this vocabulary is why the article cannot justify our supervisor."""
    text = corpus(document["article"]).lower()
    for term in ("supervisor", "checkpoint", "rubric", "stop condition", "judge"):
        assert term not in text


def test_article_withholds_the_score_confidence_formula(document):
    """Score confidence stays unset because the source never defines it."""
    from veyro.evaluators import ScoreOutcome, ScoreQuestion, normalize_result

    text = corpus(document["article"])
    assert "a confidence value" in text
    assert "modal" not in text.lower()

    question = ScoreQuestion(
        prompt="Rate the urgency.",
        outcomes=[
            ScoreOutcome(label="low", description="Not urgent.", value=0.0),
            ScoreOutcome(label="high", description="Urgent.", value=1.0),
        ],
    )
    result = normalize_result(question, {"low": 0.25, "high": 0.75})
    assert result.confidence is None
    assert result.confidence_semantics == "not_available"


def test_missing_required_phrase_fails(tmp_path, document):
    mutated = copy.deepcopy(document)
    for block in mutated["article"]["content"]["blocks"]:
        if block["text"].startswith("Noul: Answer"):
            block["text"] = "removed"
    with pytest.raises(ValueError, match="missing required phrases"):
        check(write(tmp_path, mutated))


def test_injected_supervisor_vocabulary_fails(tmp_path, document):
    mutated = copy.deepcopy(document)
    mutated["article"]["content"]["blocks"][0]["text"] += " a supervisor checkpoint"
    with pytest.raises(ValueError, match="supervisor vocabulary"):
        check(write(tmp_path, mutated))


def test_retitled_source_fails(tmp_path, document):
    mutated = copy.deepcopy(document)
    mutated["article"]["title"] = "Something Else"
    with pytest.raises(ValueError, match="Unexpected article title"):
        check(write(tmp_path, mutated))
