from __future__ import annotations

import json
import shlex
import sys

import pytest
from pydantic import ValidationError

from veyro import autonomy_check, local_evaluation, local_server, native_judge
from veyro.agents import AgentId
from veyro.autonomy import AutonomyOptions, HarnessModels, prepare_autonomy
from veyro.evaluators import CalibrationExample, DistributionCalibrator
from veyro.local_models import LocalModels, VerifiedModel, load_profiles
from veyro.native_judge import JudgeConfig, LocalJudgeProvider
from veyro.readout import READOUT_PROTOCOL


@pytest.fixture(autouse=True)
def no_remote_or_model_calls(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("local judge tests must not use remote credentials or load models")

    def require_resident(_self, profile):
        return VerifiedModel(
            profile=profile,
            blob_path=tmp_path / f"{profile.id}.gguf",
            manifest_sha256=profile.manifest_sha256,
            blob_sha256=profile.blob_sha256,
        )

    monkeypatch.setattr(LocalModels, "require_resident", require_resident)
    monkeypatch.setattr(native_judge, "JevVeyroModel", forbidden)
    monkeypatch.setattr(native_judge, "dotenv_values", forbidden)
    monkeypatch.setattr(local_server, "evaluate_local", forbidden)


@pytest.fixture
def rubric():
    return {
        "rubric_version": "local-test-v1",
        "criteria": {"done": "Artifact is complete."},
        "evidence_files": ["artifact.txt"],
        "provider": {"kind": "local", "profile": "small", "allow_uncalibrated": True},
    }


@pytest.mark.parametrize(
    "provider",
    [
        {"kind": "local"},
        {"kind": "local", "profile": "../small"},
        {"kind": "local", "profile": "small", "timeout_seconds": float("inf")},
        {"kind": "local", "profile": "small", "timeout_seconds": 0},
        {"kind": "local", "profile": "small", "base_url": "https://example.test"},
        {"kind": "local", "profile": "small", "unknown": True},
        {"kind": "locla", "profile": "small"},
    ],
)
def test_malformed_local_provider_cannot_fall_back_to_jev(rubric, provider):
    with pytest.raises(ValidationError):
        JudgeConfig.model_validate({**rubric, "provider": provider})


def test_uncalibrated_local_judge_requires_explicit_opt_in(rubric):
    rubric["provider"].pop("allow_uncalibrated")
    with pytest.raises(ValidationError, match="explicit"):
        JudgeConfig.model_validate(rubric)
    rubric["provider"]["allow_uncalibrated"] = True
    assert isinstance(JudgeConfig.model_validate(rubric).provider, LocalJudgeProvider)


def test_partial_calibration_does_not_bypass_opt_in(rubric):
    rubric["criteria"]["safe"] = "Preserves all denials."
    rubric["provider"] = {"kind": "local", "profile": "small", "calibrations": {"done": "a.json"}}
    with pytest.raises(ValidationError, match="every criterion"):
        JudgeConfig.model_validate(rubric)


@pytest.mark.parametrize("mutate", [False, True])
async def test_local_checkpoint_binds_evidence_checks_and_model_without_credentials(
    tmp_path, monkeypatch, rubric, mutate
):
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("Original evidence")
    (tmp_path / ".env").write_text("TYPESAFE_API_KEY=must-not-read")
    seen = []

    def evaluate(profile, state, questions, **kwargs):
        seen.append((profile, state, questions))
        if mutate:
            artifact.write_text("Changed evidence")
        return {
            "protocol": READOUT_PROTOCOL,
            "metrics": {"generated_tokens": 0},
            "predictions": {"done": {"outcomes": ["no", "yes"], "probabilities": [0.01, 0.99]}},
        }

    monkeypatch.setattr(local_server, "evaluate_local", evaluate)
    result = await native_judge.judge_checkpoint(
        {
            "judge": rubric,
            "repository": str(tmp_path),
            "task": "Implement",
            "checks": [
                {
                    "exit_code": 0,
                    "timed_out": False,
                    "output": "PRIVATE_CHECK_OUTPUT",
                    "command": "PRIVATE_COMMAND",
                }
            ],
        }
    )
    assert len(seen) == 1
    assert seen[0][0] == "small"
    state = seen[0][1]
    assert "Original evidence" in state["case"]
    assert '"index": 0' in state["case"]
    assert '"exit_code": 0' in state["case"]
    assert "PRIVATE" not in json.dumps(state)
    if mutate:
        assert result == {"status": "error", "error": "evidence_changed_during_judgment"}
        return
    assert result["status"] == "passed"
    assert result["calibrated"] is False
    assert result["scores"] == {"done": 0.99}
    assert result["task_sha256"] == native_judge.digest("Implement")
    assert result["evidence_sha256"] == native_judge.digest({"artifact.txt": "Original evidence"})
    provenance = result["provenance"]["done"]
    assert provenance["provider"] == "local-readout"
    assert provenance["source"] == "uncalibrated_label_logits"
    assert provenance["model"] == local_evaluation.model_identity(load_profiles()["small"])
    assert provenance["calibration_sha256"] is None


def test_calibrated_judge_records_exact_artifact_provenance(tmp_path, monkeypatch, rubric):
    path = tmp_path / "calibration.json"
    rubric["provider"] = {"kind": "local", "profile": "small", "calibrations": {"done": str(path)}}
    config = JudgeConfig.model_validate(rubric)
    artifact = DistributionCalibrator.fit(
        [CalibrationExample(id="training", probabilities={"no": 0.1, "yes": 0.9}, label="yes")],
        holdout_ids=["held"],
        model=local_evaluation.model_identity(load_profiles()["small"]),
        profile="small",
        schema_sha256=local_evaluation.calibration_schema(
            native_judge.native_definition(config), "done"
        ),
    )
    artifact.save(path)
    monkeypatch.setattr(
        local_server,
        "evaluate_local",
        lambda *args, **kwargs: {
            "protocol": READOUT_PROTOCOL,
            "metrics": {},
            "predictions": {"done": {"outcomes": ["no", "yes"], "probabilities": [0.1, 0.9]}},
        },
    )
    result = native_judge.evaluate_local_judge(config, "Task", {"artifact.txt": "Done"}, [])
    assert result["calibrated"] is True
    assert result["provenance"]["done"]["calibration_sha256"] == native_judge.digest(
        artifact.model_dump(mode="json")
    )
    assert result["provenance"]["done"]["calibration_schema_sha256"] == artifact.schema_sha256


def test_failed_checks_remain_authoritative_with_local_judge(tmp_path, monkeypatch, rubric):
    path = tmp_path / "rubric.json"
    path.write_text(json.dumps(rubric))
    launch = prepare_autonomy(
        AgentId.PRIME_AGENT,
        tmp_path,
        "Task",
        (),
        AutonomyOptions(
            checks=(shlex.join([sys.executable, "-c", "raise SystemExit(3)"]),), judge_file=path
        ),
    )
    config = launch.directory / "config.json"
    autonomy_check.checkpoint(config, "root", "start", claim=True)

    def forbidden(*args, **kwargs):
        pytest.fail("failed executable checks cannot be overruled by a local judge")

    monkeypatch.setattr(autonomy_check, "run_judge", forbidden)
    result = autonomy_check.checkpoint(config, "root", "build")
    assert result["reason"] == "checks_failed"
    assert result["action"] == "continue"


def test_dual_model_harness_repairs_from_typed_evaluation(tmp_path, monkeypatch, rubric):
    rubric_path = tmp_path / "rubric.json"
    rubric_path.write_text(json.dumps(rubric))
    (tmp_path / "artifact.txt").write_text("implementation")
    launch = prepare_autonomy(
        AgentId.OPENCODE,
        tmp_path,
        "Task",
        (),
        AutonomyOptions(
            checks=(shlex.join([sys.executable, "-c", "pass"]),),
            judge_file=rubric_path,
            models=HarnessModels("coder30", "laya"),
            allow_uncalibrated_judge=True,
        ),
    )
    config_path = launch.directory / "config.json"
    config = json.loads(config_path.read_text())
    assert config["model_roles"]["coding"]["profile"] == "coder30"
    assert config["model_roles"]["evaluation"]["profile"] == "laya"
    assert config["judge"]["provider"]["profile"] == "laya"
    assert "qwen3-coder:30b" in (launch.directory / "adapter.mjs").read_text()

    autonomy_check.checkpoint(config_path, "root", "start", claim=True)
    decisions = iter(
        [
            {"status": "failed", "scores": {"done": 0.2}},
            {"status": "passed", "scores": {"done": 0.9}},
        ]
    )
    calls = []

    def typed_evaluation(snapshot, checks, timeout):
        calls.append((snapshot["judge"]["provider"]["profile"], checks[0]["exit_code"], timeout))
        return next(decisions)

    monkeypatch.setattr(autonomy_check, "run_judge", typed_evaluation)
    first = autonomy_check.checkpoint(config_path, "root", "build")
    assert (first["action"], first["reason"]) == ("continue", "judge_failed")
    assert '"scores": {"done": 0.2}' in first["message"]
    second = autonomy_check.checkpoint(config_path, "root", "repair")
    assert (second["action"], second["reason"]) == ("blocked", "operator_review_required")
    assert [profile for profile, _, _ in calls] == ["laya", "laya"]
    assert all(exit_code == 0 and timeout > 0 for _, exit_code, timeout in calls)


@pytest.mark.parametrize(
    "profile_update",
    [
        {"revision": "b" * 40},
        {"runtime_sha256": "b" * 64},
        {"launcher_sha256": "b" * 64},
    ],
)
def test_launch_pinned_laya_identity_rejects_profile_drift(
    tmp_path, monkeypatch, rubric, profile_update
):
    rubric_path = tmp_path / "rubric.json"
    rubric_path.write_text(json.dumps(rubric))
    launch = prepare_autonomy(
        AgentId.OPENCODE,
        tmp_path,
        "Task",
        (),
        AutonomyOptions(
            checks=("true",),
            judge_file=rubric_path,
            models=HarnessModels("coder30", "laya"),
            allow_uncalibrated_judge=True,
        ),
    )
    snapshot = json.loads((launch.directory / "config.json").read_text())
    config = JudgeConfig.model_validate(snapshot["judge"])
    original = local_evaluation.load_evaluation_profiles()["laya"]
    changed = original.model_copy(update=profile_update)
    monkeypatch.setattr(local_evaluation, "load_evaluation_profiles", lambda: {"laya": changed})
    monkeypatch.setattr(
        local_evaluation,
        "run_evaluator",
        lambda *args, **kwargs: pytest.fail("identity drift must fail before inference"),
    )
    with pytest.raises(ValueError, match="identity changed after launch"):
        native_judge.evaluate_local_judge(config, "Task", {"artifact.txt": "Done"}, [])
