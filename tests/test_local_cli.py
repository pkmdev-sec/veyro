from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from veyro import cli, local_server
from veyro.agents import AgentId
from veyro.autonomy import AutonomyOptions, CodingModel, HarnessModels, prepare_autonomy
from veyro.evaluators import DistributionCalibrator, EvaluatorDefinition, load_corrections
from veyro.local_evaluation import calibration_schema, model_identity
from veyro.local_models import LocalModels, VerifiedModel, load_profiles
from veyro.readout import READOUT_PROTOCOL

runner = CliRunner()


@pytest.fixture(autouse=True)
def verified_resident_model(monkeypatch, tmp_path):
    def require_resident(_self, profile):
        return VerifiedModel(
            profile=profile,
            blob_path=tmp_path / f"{profile.id}.gguf",
            manifest_sha256=profile.manifest_sha256,
            blob_sha256=profile.blob_sha256,
        )

    monkeypatch.setattr(LocalModels, "require_resident", require_resident)


@pytest.fixture
def artifacts(tmp_path):
    values = {
        "definition": {
            "name": "review",
            "version": "1",
            "prompt": "{{artifact}}",
            "variables": {"artifact": {"source": "output"}},
            "questions": {"done": {"type": "noul", "prompt": "Complete?"}},
        },
        "case": {"id": "held", "input": "Task", "output": "Live artifact"},
        "record": {
            "case": {"id": "example", "input": "Task", "output": "Prior artifact"},
            "labels": {"done": "yes"},
        },
        "training": [{"id": "train", "probabilities": {"no": 0.2, "yes": 0.8}, "label": "yes"}],
        "holdout": ["held"],
    }
    paths = {}
    for name, value in values.items():
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(value))
    paths["store"] = tmp_path / "corrections.jsonl"
    paths["calibration"] = tmp_path / "calibration.json"
    return paths


def invoke(*args):
    return runner.invoke(cli.app, [str(arg) for arg in args])


def write_rubric(tmp_path):
    path = tmp_path / "rubric.json"
    path.write_text(
        json.dumps(
            {
                "rubric_version": "v1",
                "criteria": {"done": "Implementation exists"},
                "evidence_files": ["artifact.txt"],
            }
        )
    )
    return path


def test_preview_correction_calibration_and_evaluation_round_trip(artifacts, monkeypatch):
    p = artifacts
    preview = invoke("evaluator", "preview", p["definition"], p["case"])
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["state"] == {"case": "Live artifact", "few_shot_examples": []}
    for _ in range(2):
        corrected = invoke("evaluator", "correct", p["definition"], p["record"], p["store"])
        assert corrected.exit_code == 0, corrected.output
        assert json.loads(corrected.output)["records"] == 1
    definition = EvaluatorDefinition.load(p["definition"])
    assert len(load_corrections(p["store"], definition)) == 1
    preview = invoke(
        "evaluator", "preview", p["definition"], p["case"], "--corrections", p["store"]
    )
    state = json.loads(preview.output)["state"]
    assert state["few_shot_examples"][0]["corrected_labels"] == {"done": "yes"}
    calibrated = invoke(
        "evaluator",
        "calibrate",
        p["definition"],
        "done",
        p["training"],
        p["holdout"],
        p["calibration"],
    )
    assert calibrated.exit_code == 0, calibrated.output
    artifact = DistributionCalibrator.load(p["calibration"])
    assert artifact.model == model_identity(load_profiles()["small"])
    assert artifact.schema_sha256 == calibration_schema(definition, "done")
    assert artifact.training_ids == ["train"] and artifact.holdout_ids == ["held"]
    monkeypatch.setattr(
        local_server,
        "evaluate_local",
        lambda *a, **k: {
            "protocol": READOUT_PROTOCOL,
            "metrics": {},
            "predictions": {"done": {"outcomes": ["no", "yes"], "probabilities": [0.2, 0.8]}},
        },
    )
    evaluated = invoke(
        "evaluator", "run", p["definition"], p["case"], "--calibration", f"done={p['calibration']}"
    )
    assert evaluated.exit_code == 0, evaluated.output
    assert json.loads(evaluated.output)["calibrated"] is True
    held = p["holdout"].with_name("held-predictions.json")
    held.write_text(
        json.dumps([{"id": "held", "probabilities": {"no": 0.4, "yes": 0.6}, "label": "yes"}])
    )
    validated = invoke("evaluator", "validate-calibration", p["calibration"], held)
    assert validated.exit_code == 0, validated.output
    assert json.loads(validated.output)["count"] == 1


def test_calibration_rejects_training_holdout_overlap(artifacts):
    p = artifacts
    p["holdout"].write_text('["train"]')
    result = invoke(
        "evaluator",
        "calibrate",
        p["definition"],
        "done",
        p["training"],
        p["holdout"],
        p["calibration"],
    )
    assert result.exit_code == 2
    assert "holdout leakage" in result.output
    assert not p["calibration"].exists()


@pytest.mark.parametrize("bad", ["not-json", "{}", "null", "1"])
def test_invalid_training_json_is_a_safe_cli_error(artifacts, bad):
    p = artifacts
    p["training"].write_text(bad)
    result = invoke(
        "evaluator",
        "calibrate",
        p["definition"],
        "done",
        p["training"],
        p["holdout"],
        p["calibration"],
    )
    assert result.exit_code == 2, repr(result.exception)
    assert result.output.strip()
    assert "Traceback" not in result.output
    assert not p["calibration"].exists()


@pytest.mark.parametrize("entry", ["done", "=file", "done="])
def test_calibration_option_errors_do_not_call_service(artifacts, monkeypatch, entry):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid CLI inputs must fail before service access")

    monkeypatch.setattr(local_server, "evaluate_local", forbidden)
    result = invoke(
        "evaluator", "run", artifacts["definition"], artifacts["case"], "--calibration", entry
    )
    assert result.exit_code == 2
    assert "QUESTION=FILE" in result.output


def test_bad_correction_preserves_existing_store(artifacts):
    p = artifacts
    assert invoke("evaluator", "correct", p["definition"], p["record"], p["store"]).exit_code == 0
    before = p["store"].read_bytes()
    record = json.loads(p["record"].read_text())
    record["labels"]["done"] = "invalid"
    p["record"].write_text(json.dumps(record))
    result = invoke("evaluator", "correct", p["definition"], p["record"], p["store"])
    assert result.exit_code == 2
    assert p["store"].read_bytes() == before


def test_dual_model_cli_options_are_scoped_to_launch(tmp_path, monkeypatch):
    native = "opencode"
    seen = []

    def launch(provider, repo, prompt, args, **kwargs):
        seen.append((provider, repo, prompt, args, kwargs["autonomy"]))
        return SimpleNamespace(exit_code=0, autonomy_directory=None)

    monkeypatch.setattr(cli, "launch_native_agent", launch)
    environment = dict(os.environ)
    rubric = write_rubric(tmp_path)
    result = invoke(
        "agent",
        native,
        "--repo",
        tmp_path,
        "--prompt",
        "Implement",
        "--autonomous",
        "--check",
        "true",
        "--coding-profile",
        "small",
        "--evaluation-profile",
        "14b",
        "--evaluator",
        rubric,
        "--allow-uncalibrated-evaluator",
    )
    assert result.exit_code == 0, repr(result.exception)
    assert seen[0][4].models == HarnessModels("small", "14b")
    assert seen[0][4].allow_uncalibrated_judge is True
    assert dict(os.environ) == environment
    assert not (tmp_path / "opencode.json").exists()


def test_local_flags_without_autonomy_fail_before_launch(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid launch must not start a native agent")

    monkeypatch.setattr(cli, "launch_native_agent", forbidden)
    result = invoke(
        "agent",
        "opencode",
        "--repo",
        tmp_path,
        "--coding-profile",
        "coder30",
        "--evaluation-profile",
        "14b",
    )
    assert result.exit_code == 2
    assert "does not launch unpinned interactive models" in result.output


def test_evaluation_profile_requires_coding_profile(tmp_path):
    result = invoke(
        "agent",
        "opencode",
        "--repo",
        tmp_path,
        "--prompt",
        "Implement",
        "--autonomous",
        "--check",
        "true",
        "--evaluation-profile",
        "14b",
    )
    assert result.exit_code == 2
    assert "requires --coding-profile" in result.output


def test_coding_profile_can_run_without_typed_evaluation(tmp_path, monkeypatch):
    seen = []

    def launch(*args, **kwargs):
        seen.append(kwargs["autonomy"])
        return SimpleNamespace(exit_code=0, autonomy_directory=None)

    monkeypatch.setattr(cli, "launch_native_agent", launch)
    result = invoke(
        "agent",
        "opencode",
        "--repo",
        tmp_path,
        "--prompt",
        "Implement",
        "--autonomous",
        "--check",
        "true",
        "--coding-profile",
        "coder30",
    )
    assert result.exit_code == 0, repr(result.exception)
    assert seen[0].models == CodingModel("coder30")


def test_dual_model_profiles_preserve_native_denials_and_global_configuration(
    tmp_path, monkeypatch
):
    original = {
        "permission": {"bash": {"rm *": "deny"}},
        "agent": {
            "build": {"permission": {"bash": {"git push*": "deny"}, "external_directory": "deny"}}
        },
    }
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps(original))
    launch = prepare_autonomy(
        AgentId.OPENCODE,
        tmp_path,
        "Task",
        (),
        AutonomyOptions(
            ("true",),
            judge_file=write_rubric(tmp_path),
            models=HarnessModels("small", "14b"),
            allow_uncalibrated_judge=True,
        ),
    )
    actual = json.loads(launch.environment["OPENCODE_CONFIG_CONTENT"])
    assert actual["permission"] == original["permission"]
    assert actual["agent"]["build"]["permission"]["bash"] == {"git push*": "deny"}
    assert actual["agent"]["build"]["permission"]["external_directory"] == "deny"
    assert json.loads(os.environ["OPENCODE_CONFIG_CONTENT"]) == original


@pytest.mark.parametrize("provider", [None, [], "invalid"])
def test_malformed_local_judge_file_is_a_safe_launch_error(tmp_path, monkeypatch, provider):
    path = tmp_path / "judge.json"
    path.write_text(
        json.dumps(
            {
                "rubric_version": "1",
                "criteria": {"done": "Done?"},
                "evidence_files": ["artifact.txt"],
                "provider": provider,
            }
        )
    )

    def prepare(provider, repo, prompt, args, **kwargs):
        prepare_autonomy(provider, repo, prompt, args, kwargs["autonomy"])
        pytest.fail("invalid provider must not prepare a launch")

    monkeypatch.setattr(cli, "launch_native_agent", prepare)
    result = invoke(
        "agent",
        "opencode",
        "--repo",
        tmp_path,
        "--prompt",
        "Task",
        "--autonomous",
        "--check",
        "true",
        "--coding-profile",
        "coder30",
        "--evaluation-profile",
        "14b",
        "--allow-uncalibrated-evaluator",
        "--evaluator",
        path,
    )
    assert result.exit_code == 2, repr(result.exception)
    assert result.output.strip()
    assert not (tmp_path / ".veyro").exists()


@pytest.mark.parametrize(
    "response",
    [
        {"protocol": READOUT_PROTOCOL, "metrics": {}},
        {"protocol": READOUT_PROTOCOL, "predictions": None, "metrics": {}},
        {"protocol": READOUT_PROTOCOL, "predictions": {"done": {}}, "metrics": {}},
        {
            "protocol": READOUT_PROTOCOL,
            "predictions": {"done": {"outcomes": ["no", "yes"], "probabilities": None}},
            "metrics": {},
        },
    ],
)
def test_malformed_service_results_are_safe_cli_errors(artifacts, monkeypatch, response):
    monkeypatch.setattr(local_server, "evaluate_local", lambda *args, **kwargs: response)
    result = invoke("evaluator", "run", artifacts["definition"], artifacts["case"])
    assert result.exit_code == 2, repr(result.exception)
    assert result.output.strip()
    assert "Traceback" not in result.output


def test_calibrate_rejects_outcomes_from_another_question(artifacts):
    p = artifacts
    p["training"].write_text(
        json.dumps([{"id": "train", "probabilities": {"cat": 0.2, "dog": 0.8}, "label": "dog"}])
    )
    result = invoke(
        "evaluator",
        "calibrate",
        p["definition"],
        "done",
        p["training"],
        p["holdout"],
        p["calibration"],
    )
    assert result.exit_code == 2, result.output
    assert not p["calibration"].exists()


@pytest.mark.parametrize("id", ["train", "unreserved"])
def test_holdout_validation_rejects_training_or_unreserved_ids(artifacts, id):
    p = artifacts
    fitted = invoke(
        "evaluator",
        "calibrate",
        p["definition"],
        "done",
        p["training"],
        p["holdout"],
        p["calibration"],
    )
    assert fitted.exit_code == 0, fitted.output
    p["holdout"].write_text(
        json.dumps([{"id": id, "probabilities": {"no": 0.2, "yes": 0.8}, "label": "yes"}])
    )
    result = invoke("evaluator", "validate-calibration", p["calibration"], p["holdout"])
    assert result.exit_code == 2
    assert "holdout" in result.output


@pytest.mark.parametrize("opt_in", [False, True])
def test_launch_snapshots_local_judge_only_with_explicit_raw_score_opt_in(tmp_path, opt_in):
    path = tmp_path / "judge.json"
    original = {
        "rubric_version": "1",
        "criteria": {"done": "Done?"},
        "evidence_files": ["artifact.txt"],
    }
    path.write_text(json.dumps(original))
    options = AutonomyOptions(
        ("true",),
        judge_file=path,
        models=HarnessModels("coder30", "14b"),
        allow_uncalibrated_judge=opt_in,
    )
    if not opt_in:
        with pytest.raises(ValueError, match="invalid semantic evaluator"):
            prepare_autonomy(AgentId.OPENCODE, tmp_path, "Task", (), options)
        assert not (tmp_path / ".veyro").exists()
    else:
        launch = prepare_autonomy(AgentId.OPENCODE, tmp_path, "Task", (), options)
        config = json.loads((launch.directory / "config.json").read_text())
        assert config["judge"]["provider"]["kind"] == "local"
        assert config["judge"]["provider"]["profile"] == "14b"
        assert config["model_roles"]["coding"]["profile"] == "coder30"
        assert config["model_roles"]["evaluation"]["profile"] == "14b"
        assert config["judge"]["provider"]["allow_uncalibrated"] is True
    assert json.loads(path.read_text()) == original


def test_fit_and_validate_decision_head_cli(artifacts, tmp_path):
    p = artifacts
    training = tmp_path / "head-training.json"
    training.write_text(
        json.dumps(
            [
                {"id": "train-yes-1", "features": {"done": 0.9}, "label": "yes"},
                {"id": "train-yes-2", "features": {"done": 0.8}, "label": "yes"},
                {"id": "train-no-1", "features": {"done": 0.2}, "label": "no"},
                {"id": "train-no-2", "features": {"done": 0.1}, "label": "no"},
            ]
        )
    )
    reserved = tmp_path / "head-holdout-ids.json"
    reserved.write_text('["held-yes", "held-no"]')
    output = tmp_path / "decision-head.json"
    fitted = invoke(
        "evaluator",
        "fit-decision-head",
        p["definition"],
        training,
        reserved,
        output,
        "--feature",
        "done",
        "--regularization",
        "0.1",
    )
    assert fitted.exit_code == 0, fitted.output

    from veyro.evaluators import BinaryDecisionHead
    from veyro.local_evaluation import decision_head_schema

    head = BinaryDecisionHead.load(output)
    definition = EvaluatorDefinition.load(p["definition"])
    assert head.schema_sha256 == decision_head_schema(definition, ["done"])
    assert head.training_ids == ["train-yes-1", "train-yes-2", "train-no-1", "train-no-2"]
    holdout = tmp_path / "head-holdout.json"
    holdout.write_text(
        json.dumps(
            [
                {"id": "held-yes", "features": {"done": 0.85}, "label": "yes"},
                {"id": "held-no", "features": {"done": 0.15}, "label": "no"},
            ]
        )
    )
    validated = invoke("evaluator", "validate-decision-head", output, holdout)
    assert validated.exit_code == 0, validated.output
    assert json.loads(validated.output)["accuracy"] == 1
