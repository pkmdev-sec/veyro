from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from veyro.supervision.checkpoints import CHECKPOINT_QUESTIONS
from veyro.veyro.base import VeyroModelError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "examples" / "assess_localjev.py"
spec = importlib.util.spec_from_file_location("assessment_example", SCRIPT)
assert spec is not None and spec.loader is not None
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


def test_example_executable_previews_real_checkpoint_without_scores():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        cwd=ROOT,
    )
    value = json.loads(result.stdout)
    assert value["mode"] == "preview"
    assert value["checkpoint_kind"] == "failed_verification"
    assert value["synthetic_input"] is True
    assert value["controls_enabled"] is False
    assert value["per_response_weight_attestation"] is False
    assert set(value["questions"]) == set(CHECKPOINT_QUESTIONS)
    assert "assessment" not in value


@pytest.mark.asyncio
async def test_preview_never_constructs_a_model_client(monkeypatch):
    def forbidden():
        raise AssertionError("Preview attempted to construct an assessor")

    monkeypatch.setattr(example, "authoritative_assessments", forbidden)
    assert (await example.run_example(live=False))["mode"] == "preview"


@pytest.mark.asyncio
async def test_live_failure_closes_client_without_fallback(monkeypatch):
    class FailedAssessor:
        closed = False

        async def assess_if_needed(self, event, state):
            assert event.sequence == state.last_sequence
            assert state.checks[-1].passed is False
            raise VeyroModelError("unavailable")

        async def close(self):
            self.closed = True

    service = FailedAssessor()
    monkeypatch.setattr(example, "authoritative_assessments", lambda: service)
    with pytest.raises(VeyroModelError):
        await example.run_example(live=True)
    assert service.closed
