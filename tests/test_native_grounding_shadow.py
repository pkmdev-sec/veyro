from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from veyro import native_judge
from veyro.grounding import EvidenceRelationship, EvidenceType
from veyro.native_judge import JudgeConfig, NativeGroundingShadowConfig


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def check(*, exit_code: int = 0) -> dict:
    return {
        "command": ["uv", "run", "pytest", "-q"],
        "exit_code": exit_code,
        "timed_out": False,
        "output": "1 passed\n" if exit_code == 0 else "1 failed\n",
        "output_truncated": False,
        "latency_ms": 10.0,
        "completed_at": datetime.now(UTC).isoformat(),
    }


def config(tmp_path) -> JudgeConfig:
    profile = tmp_path / "profile.json"
    profile.write_text("{}")
    return JudgeConfig(
        rubric_version="shadow-test",
        criteria={"done": "The implementation is complete."},
        evidence_files=["artifact.txt"],
        provider={"kind": "local", "profile": "small", "allow_uncalibrated": True},
        grounding_shadow={
            "profile": profile,
            "profile_sha256": sha(profile.read_bytes()),
            "required_evidence_types": {"done": ["implementation", "executed_check"]},
            "evidence_bindings": {
                "artifact.txt": {
                    "type": "implementation",
                    "relationship": "supports",
                    "clause_ids": ["done"],
                }
            },
        },
    )


def test_native_shadow_configuration_requires_absolute_bound_profile(tmp_path):
    with pytest.raises(ValidationError, match="must be absolute"):
        NativeGroundingShadowConfig(
            profile="relative-profile.json",
            profile_sha256="a" * 64,
            required_evidence_types={
                "done": frozenset({EvidenceType.IMPLEMENTATION, EvidenceType.EXECUTED_CHECK})
            },
        )

    path = tmp_path / "profile.json"
    path.write_text("{}")
    assert native_judge._bound_shadow_json(path, sha(path.read_bytes())) == b"{}"
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        native_judge._bound_shadow_json(path, "0" * 64)


def test_native_shadow_contract_requires_every_clause_code_and_checks(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text("{}")
    with pytest.raises(ValidationError, match="implementation and executed-check"):
        NativeGroundingShadowConfig(
            profile=profile,
            profile_sha256=sha(profile.read_bytes()),
            required_evidence_types={"done": frozenset({EvidenceType.IMPLEMENTATION})},
        )

    payload = config(tmp_path).model_dump(mode="python")
    payload["criteria"]["reviewed"] = "The change was reviewed."
    with pytest.raises(ValidationError, match="every criterion exactly"):
        JudgeConfig.model_validate(payload)


def test_native_shadow_builds_case_from_current_evidence_and_check_receipts(tmp_path):
    selected = config(tmp_path)
    evidence = {"artifact.txt": "def completed():\n    return True\n"}
    current_check = check()
    case = native_judge._native_grounding_case(
        selected,
        "Implement the behavior",
        tmp_path,
        evidence,
        [current_check],
    )
    assert case.criterion == "Implement the behavior"
    assert case.clauses[0].required_evidence_types == frozenset(
        {EvidenceType.IMPLEMENTATION, EvidenceType.EXECUTED_CHECK}
    )
    source, executed = case.evidence
    assert source.type is EvidenceType.IMPLEMENTATION
    assert source.relationship is EvidenceRelationship.SUPPORTS
    assert source.content == evidence["artifact.txt"]
    assert executed.check is not None
    assert executed.check.protected is False
    assert executed.check.passed is True
    assert executed.check.contract_sha256 == case.contract_sha256
    assert executed.check.candidate_sha256 == case.candidate_sha256

    changed = native_judge._native_grounding_case(
        selected,
        "Implement the behavior",
        tmp_path,
        {"artifact.txt": evidence["artifact.txt"] + "# changed\n"},
        [current_check],
    )
    assert changed.candidate_sha256 != case.candidate_sha256
    assert changed.contract_sha256 == case.contract_sha256


def test_unbound_or_empty_native_evidence_cannot_become_implementation(tmp_path):
    selected = config(tmp_path)
    unbound_payload = selected.model_dump(mode="python")
    unbound_payload["grounding_shadow"]["evidence_bindings"] = {}
    unbound = JudgeConfig.model_validate(unbound_payload)
    unbound_case = native_judge._native_grounding_case(
        unbound, "Task", tmp_path, {"artifact.txt": "claims completion"}, [check()]
    )
    assert unbound_case.evidence[0].type is EvidenceType.UNTRUSTED_ARTIFACT
    assert unbound_case.evidence[0].relationship is EvidenceRelationship.CONTEXT

    empty_case = native_judge._native_grounding_case(
        selected, "Task", tmp_path, {"artifact.txt": ""}, [check()]
    )
    assert empty_case.evidence[0].type is EvidenceType.UNTRUSTED_ARTIFACT
    assert empty_case.evidence[0].content == "[empty evidence file]"


@pytest.mark.asyncio
async def test_shadow_attachment_cannot_change_authoritative_judge_status(tmp_path, monkeypatch):
    selected = config(tmp_path)
    monkeypatch.setattr(
        native_judge,
        "_evaluate_configured_grounding_shadow",
        lambda *_args: {
            "protocol": "selene-grounding-shadow-v1",
            "authority": "shadow_only",
            "reported_result": "no",
            "would_authorize_completion": False,
        },
    )
    authoritative = {"status": "passed", "scores": {"done": 1.0}}
    result = await native_judge._attach_grounding_shadow(
        selected,
        authoritative,
        "Task",
        tmp_path,
        {"artifact.txt": "implemented"},
        [check()],
    )
    assert result["status"] == "passed"
    assert result["scores"] == {"done": 1.0}
    assert result["grounding_shadow"]["authority"] == "shadow_only"
    assert result["grounding_shadow"]["reported_result"] == "no"


@pytest.mark.asyncio
async def test_shadow_error_is_recorded_without_blocking_main_judge(tmp_path, monkeypatch):
    selected = config(tmp_path)

    def fail(*_args):
        raise RuntimeError("model failed")

    monkeypatch.setattr(native_judge, "_evaluate_configured_grounding_shadow", fail)
    result = await native_judge._attach_grounding_shadow(
        selected,
        {"status": "failed", "scores": {"done": 0.1}},
        "Task",
        tmp_path,
        {"artifact.txt": "implemented"},
        [check()],
    )
    assert result["status"] == "failed"
    assert result["scores"] == {"done": 0.1}
    assert result["grounding_shadow"] == {
        "protocol": "selene-grounding-shadow-v1",
        "authority": "shadow_only",
        "status": "error",
        "error": "grounding_shadow_error",
    }


@pytest.mark.asyncio
async def test_native_judge_attaches_shadow_only_after_main_local_result(tmp_path, monkeypatch):
    selected = config(tmp_path)
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("implemented")
    monkeypatch.setattr(
        native_judge,
        "evaluate_local_judge",
        lambda *_args: {"status": "passed", "scores": {"done": 1.0}},
    )
    monkeypatch.setattr(
        native_judge,
        "_evaluate_configured_grounding_shadow",
        lambda *_args: {"authority": "shadow_only", "reported_result": "yes"},
    )
    result = await native_judge.judge_checkpoint(
        {
            "judge": selected.model_dump(mode="json"),
            "repository": str(tmp_path),
            "task": "Implement the behavior",
            "checks": [check()],
        }
    )
    assert result["status"] == "passed"
    assert result["grounding_shadow"] == {
        "authority": "shadow_only",
        "reported_result": "yes",
    }


@pytest.mark.asyncio
async def test_evidence_mutation_during_shadow_invalidates_entire_judgment(tmp_path, monkeypatch):
    selected = config(tmp_path)
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("implemented")
    monkeypatch.setattr(
        native_judge,
        "evaluate_local_judge",
        lambda *_args: {"status": "passed", "scores": {"done": 1.0}},
    )

    def mutate(*_args):
        artifact.write_text("changed during shadow")
        return {"authority": "shadow_only", "reported_result": "yes"}

    monkeypatch.setattr(native_judge, "_evaluate_configured_grounding_shadow", mutate)
    result = await native_judge.judge_checkpoint(
        {
            "judge": selected.model_dump(mode="json"),
            "repository": str(tmp_path),
            "task": "Implement the behavior",
            "checks": [check()],
        }
    )
    assert result == {"status": "error", "error": "evidence_changed_during_judgment"}


def test_native_shadow_configuration_denies_retired_holdout_paths(tmp_path):
    retired = tmp_path / ".audit" / "selene-holdout-v1"
    retired.mkdir(parents=True)
    profile = retired / "profile.json"
    profile.write_text("{}")
    with pytest.raises(ValidationError, match="retired Selene holdout"):
        NativeGroundingShadowConfig(
            profile=profile,
            profile_sha256=sha(profile.read_bytes()),
            required_evidence_types={
                "done": frozenset({EvidenceType.IMPLEMENTATION, EvidenceType.EXECUTED_CHECK})
            },
        )
