"""Prove the G4 canary gates were each correct about their own question.

These tests exist to stop a future change from "fixing" G4 by weakening the
verifier or leaking held-out expectations into the judge rubric. No network or
model access: everything here is recorded evidence plus pure functions.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
CANARY = ROOT / "docs" / "local-prime-14b-canary.json"
RUBRIC = ROOT / "examples" / "local-native-canary-judge.json"

# The exact implementation the 14B model produced in the 7/8 run, per GATES.md.
DEFECTIVE = (
    "def slug(text):\n"
    '    """Return a slug."""\n'
    '    return re.sub(r"[\\W_]+", "-", text).strip("-").lower()\n'
)


def defective_slug(text: str) -> str:
    return re.sub(r"[\W_]+", "-", text).strip("-").lower()


def test_canary_run_reached_completed_with_seven_of_eight():
    result = json.loads(CANARY.read_text())["result"]
    assert result["phase"] == "completed"
    assert (result["assertions_passed"], result["assertions_total"]) == (7, 8)
    assert result["verifier_unchanged"] is True


def test_judge_was_only_asked_two_structural_questions():
    """The judge never received the expected slug outputs, so it cannot vouch for them."""
    rubric = json.loads(RUBRIC.read_text())
    assert set(rubric["criteria"]) == {"docstring_present", "implementation_present"}
    text = json.dumps(rubric)
    for leaked in ("CAF", "caf", "hello-world", "a-b-c"):
        assert leaked not in text


def test_both_judge_answers_were_true_for_the_defective_code():
    """Root cause check: the judge scored high because both claims really hold."""
    scores = json.loads(CANARY.read_text())["result"]["judge_result"]["scores"]
    assert min(scores.values()) > 0.9

    assert '"""' in DEFECTIVE, "docstring_present is genuinely true"
    assert "re.sub" in DEFECTIVE, "implementation_present is genuinely true"
    assert "return text\n" != DEFECTIVE, "not an identity stub"


def test_defective_code_passes_every_asserted_case_and_fails_only_a_held_out_one():
    """The executable gate was also correct: all three asserted cases really pass."""
    from benchmark_native_autonomy import CASES

    for text, expected in CASES[:3]:
        assert defective_slug(text) == expected

    failures = [t for t, e in CASES[3:] if defective_slug(t) != e]
    assert failures == ["CAF\u00c9"]


def test_verifier_still_holds_back_the_unicode_case():
    """Guards the measurement: asserting CASES[6] would destroy the overfit probe."""
    source = (ROOT / "tools" / "benchmark_native_autonomy.py").read_text()
    assert "CASES[:3]" in source, "verifier must assert only the first three cases"
