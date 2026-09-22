from __future__ import annotations

# ruff: noqa: E501 - embedded fake launcher source is intentionally literal.
import hashlib
import sys
from pathlib import Path

import pytest

from veyro import local_evaluation
from veyro.evaluators import (
    ChoiceQuestion,
    EvaluationCase,
    EvaluatorDefinition,
    NoulQuestion,
    Outcome,
    ScoreOutcome,
    ScoreQuestion,
)
from veyro.laya_evaluation import (
    LayaProfile,
    _laya_questions,
    _normalize_laya_response,
    _tree_sha256,
    evaluate_laya,
    laya_model_identity,
    load_laya_profiles,
)
from veyro.readout import READOUT_PROTOCOL

QUESTIONS = {
    "done": NoulQuestion(prompt="Complete?"),
    "kind": ChoiceQuestion(
        prompt="Kind?",
        outcomes=[Outcome(label="code", description="Source"), Outcome(label="text", description="Text")],
    ),
    "quality": ScoreQuestion(
        prompt="Quality?",
        outcomes=[
            ScoreOutcome(label="low", description="Incomplete", value=0),
            ScoreOutcome(label="high", description="Complete", value=10),
        ],
    ),
    "constant": ChoiceQuestion(prompt="Only?", outcomes=[Outcome(label="only", description="Only")]),
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fake_profile(tmp_path: Path) -> LayaProfile:
    root = tmp_path / "model"
    required = [
        "rl_agent_api.py",
        "rl_common.py",
        "typed-decisions/model.safetensors",
        "typed-decisions/rl_agent_config.json",
        "typed-decisions/encoder/config.json",
        "typed-decisions/tokenizer/tokenizer.json",
        "typed-decisions/tokenizer/tokenizer_config.json",
    ]
    for index, name in enumerate(required):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture-{index}")
    launcher = tmp_path / "laya-local"
    launcher.write_text(
        """#!/usr/bin/env python3
import json, os, sys
assert os.environ["HF_HUB_OFFLINE"] == "1"
assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
assert "HTTP_PROXY" not in os.environ and "HTTPS_PROXY" not in os.environ
payload = json.load(sys.stdin)
root = os.path.realpath(os.environ["LAYA_MODEL_ROOT"])
answers = {
  "done": {"type": "noul", "noul": 0.8, "rl_agent": {"act_probability": 1.0}},
  "kind": {"type": "choice", "choice": "code", "probabilities": {"code": 0.7, "text": 0.3}, "confidence": 0.4, "rl_agent": {"act_probability": 1.0}},
  "quality": {"type": "score", "score": 8.0, "probabilities": {"0": 0.2, "1": 0.8}, "legend": {"0": "Incomplete", "1": "Complete"}, "confidence": 0.5, "rl_agent": {"act_probability": 1.0}}
}
assert set(payload["questions"]) == set(answers)
print(json.dumps({"model": "rl-agent", "answers": answers, "usage": {"input_tokens": 42, "output_tokens": 0}, "checkpoint": "typed-decisions", "checkpoint_path": root + "/typed-decisions"}))
"""
    )
    launcher.chmod(0o755)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    (runtime_root / "runtime.txt").write_text("pinned runtime")
    return LayaProfile(
        id="laya",
        python=Path(sys.executable),
        python_sha256=digest(Path(sys.executable).resolve()),
        runtime_root=runtime_root,
        runtime_sha256=_tree_sha256(runtime_root),
        launcher=launcher,
        launcher_sha256=digest(launcher),
        model_root=root,
        checkpoint="typed-decisions",
        revision="a" * 40,
        device="cpu",
        files={name: digest(root / name) for name in required},
    )


def test_repo_profile_is_pinned_to_installed_typed_checkpoint():
    profile = load_laya_profiles()["laya"]
    assert profile.checkpoint == "typed-decisions"
    assert profile.files["typed-decisions/model.safetensors"] == (
        "4fa56de72383a9d3efa9cfa78955733c81b9fc8067a587ca4beb82c78107a24e"
    )
    assert laya_model_identity(profile).startswith(
        "convaiinnovations/laya/typed-decisions"
        "@revision:1c5edc17a7acd8701df6fc341c0d179f1c62c982@sha256:"
        "4fa56de72383a9d3efa9cfa78955733c81b9fc8067a587ca4beb82c78107a24e"
        "@manifest-sha256:"
    )
    assert len(laya_model_identity(profile).rsplit(":", 1)[1]) == 64


def test_model_identity_binds_complete_runtime_manifest(tmp_path):
    profile = fake_profile(tmp_path)
    identity = laya_model_identity(profile)
    changed_files = {**profile.files, "rl_common.py": "b" * 64}
    variants = [
        profile.model_copy(update={"python_sha256": "b" * 64}),
        profile.model_copy(update={"runtime_sha256": "b" * 64}),
        profile.model_copy(update={"runtime_layout_sha256": "b" * 64}),
        profile.model_copy(update={"launcher_sha256": "b" * 64}),
        profile.model_copy(update={"device": "mps"}),
        profile.model_copy(update={"files": changed_files}),
    ]
    assert all(laya_model_identity(variant) != identity for variant in variants)


def test_profile_rejects_cwd_relative_runtime_paths(tmp_path):
    profile = fake_profile(tmp_path).model_dump()
    profile["launcher"] = "relative/laya-local"
    with pytest.raises(ValueError, match="absolute or home-relative"):
        LayaProfile.model_validate(profile)


def test_question_mapping_preserves_typed_semantics_and_skips_constant():
    mapped = _laya_questions(QUESTIONS)
    assert mapped == {
        "done": {
            "type": "noul",
            "instructions": "Complete?",
            "criteria": {
                "false": "The criterion is not fully established.",
                "true": "The entire criterion is established.",
            },
        },
        "kind": {
            "type": "choice",
            "instructions": "Kind?",
            "criteria": {"code": "Source", "text": "Text"},
        },
        "quality": {
            "type": "score",
            "instructions": "Quality?",
            "criteria": ["Incomplete", "Complete"],
        },
    }


def test_response_mapping_returns_readout_protocol_in_declared_order(tmp_path):
    profile = fake_profile(tmp_path)
    raw = {
        "model": "rl-agent",
        "answers": {
            "done": {"type": "noul", "noul": 0.8, "rl_agent": {"act_probability": 1.0}},
            "kind": {
                "type": "choice",
                "choice": "code",
                "probabilities": {"text": 0.3, "code": 0.7},
                "confidence": 0.4,
                "rl_agent": {"act_probability": 1.0},
            },
            "quality": {
                "type": "score",
                "score": 8,
                "probabilities": {"1": 0.8, "0": 0.2},
                "legend": {"0": "Incomplete", "1": "Complete"},
                "confidence": 0.5,
                "rl_agent": {"act_probability": 1.0},
            },
        },
        "usage": {"input_tokens": 42, "output_tokens": 0},
        "checkpoint": "typed-decisions",
        "checkpoint_path": str(profile.model_root.resolve() / "typed-decisions"),
    }
    result = _normalize_laya_response(profile, QUESTIONS, raw, latency_ms=12.5)
    assert result["protocol"] == READOUT_PROTOCOL
    assert result["predictions"] == {
        "done": {"outcomes": ["no", "yes"], "probabilities": [0.2, 0.8]},
        "kind": {"outcomes": ["code", "text"], "probabilities": [0.7, 0.3]},
        "quality": {"outcomes": ["low", "high"], "probabilities": [0.2, 0.8]},
    }
    assert result["metrics"]["backend"] == "laya"


def test_offline_subprocess_adapter_verifies_and_executes(tmp_path, monkeypatch):
    profile = fake_profile(tmp_path)
    monkeypatch.setattr("veyro.laya_evaluation._offline_command", lambda command: command)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    result = evaluate_laya(profile, {"case": "state"}, QUESTIONS, timeout=10)
    assert result["predictions"]["done"]["probabilities"] == pytest.approx([0.2, 0.8])
    assert result["predictions"]["kind"]["outcomes"] == ["code", "text"]


def test_runtime_hash_drift_fails_before_inference(tmp_path, monkeypatch):
    profile = fake_profile(tmp_path)
    (profile.model_root / "rl_common.py").write_text("changed")
    monkeypatch.setattr(
        "veyro.laya_evaluation.subprocess.run",
        lambda *args, **kwargs: pytest.fail("drift must fail before subprocess launch"),
    )
    with pytest.raises(ValueError, match="SHA-256"):
        evaluate_laya(profile, {}, QUESTIONS, timeout=10)


def test_dependency_runtime_drift_fails_before_inference(tmp_path, monkeypatch):
    profile = fake_profile(tmp_path)
    (profile.runtime_root / "runtime.txt").write_text("changed")
    monkeypatch.setattr(
        "veyro.laya_evaluation.subprocess.run",
        lambda *args, **kwargs: pytest.fail("runtime drift must fail before launch"),
    )
    with pytest.raises(ValueError, match="runtime SHA-256"):
        evaluate_laya(profile, {}, QUESTIONS, timeout=10)


def test_dependency_runtime_content_is_checked_when_layout_matches(tmp_path, monkeypatch):
    profile = fake_profile(tmp_path).model_copy(update={"runtime_layout_sha256": "b" * 64})
    (profile.runtime_root / "runtime.txt").write_text("mutated bytes!")
    monkeypatch.setattr(
        "veyro.laya_evaluation._immutable_tree_layout_sha256", lambda _root: "b" * 64
    )
    monkeypatch.setattr(
        "veyro.laya_evaluation.subprocess.run",
        lambda *args, **kwargs: pytest.fail("runtime drift must fail before launch"),
    )
    with pytest.raises(ValueError, match="runtime SHA-256"):
        evaluate_laya(profile, {}, QUESTIONS, timeout=10)


def test_inherited_network_sandbox_is_not_nested(monkeypatch):
    monkeypatch.setattr("veyro.laya_evaluation._network_outbound_denied", lambda: True)
    monkeypatch.setattr(
        "veyro.laya_evaluation.shutil.which",
        lambda *args: pytest.fail("inherited sandbox must not launch sandbox-exec"),
    )
    command = ["python", "laya"]
    assert __import__("veyro.laya_evaluation", fromlist=["_offline_command"])._offline_command(
        command
    ) == command


def test_environment_marker_cannot_spoof_network_sandbox(monkeypatch):
    monkeypatch.setenv("VEYRO_NETWORK_SANDBOXED", "1")
    monkeypatch.setattr("veyro.laya_evaluation._network_outbound_denied", lambda: False)
    monkeypatch.setattr("veyro.laya_evaluation.shutil.which", lambda name: "/sandbox-exec")
    assert __import__("veyro.laya_evaluation", fromlist=["_offline_command"])._offline_command(
        ["python", "laya"]
    )[:3] == ["/sandbox-exec", "-p", "(version 1) (allow default) (deny network-outbound)"]


def test_unsupported_platform_fails_closed(monkeypatch):
    monkeypatch.setattr("veyro.laya_evaluation.sys.platform", "linux")
    with pytest.raises(ValueError, match="supported OS network sandbox"):
        __import__("veyro.laya_evaluation", fromlist=["_offline_command"])._offline_command(
            ["python", "laya"]
        )


def test_run_evaluator_dispatches_laya_without_qwen_service(tmp_path, monkeypatch):
    profile = fake_profile(tmp_path)
    definition = EvaluatorDefinition.model_validate(
        {
            "name": "laya-test",
            "version": "1",
            "prompt": "{{artifact}}",
            "variables": {"artifact": {"source": "output"}},
            "questions": {
                "done": {"type": "noul", "prompt": "Complete?"},
                "kind": {
                    "type": "choice",
                    "prompt": "Kind?",
                    "outcomes": [
                        {"label": "code", "description": "Source"},
                        {"label": "text", "description": "Text"},
                    ],
                },
            },
        }
    )
    case = EvaluationCase(id="live", input=None, output="artifact")
    monkeypatch.setattr(local_evaluation, "load_evaluation_profiles", lambda: {"laya": profile})
    monkeypatch.setattr(
        local_evaluation,
        "evaluate_laya",
        lambda *args, **kwargs: {
            "protocol": READOUT_PROTOCOL,
            "predictions": {
                "done": {"outcomes": ["no", "yes"], "probabilities": [0.2, 0.8]},
                "kind": {"outcomes": ["code", "text"], "probabilities": [0.7, 0.3]},
            },
            "metrics": {"backend": "laya"},
        },
    )
    result = local_evaluation.run_evaluator(definition, case, "laya")
    assert result["model"] == laya_model_identity(profile)
    assert result["results"]["done"]["value"] == pytest.approx(0.8)
    assert result["results"]["kind"]["source"] == "uncalibrated_model_distribution"
    assert result["metrics"] == {"backend": "laya"}
