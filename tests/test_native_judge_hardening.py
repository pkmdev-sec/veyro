from __future__ import annotations

import json
import sys

import pytest

from veyro.autonomy_check import run_check, run_judge
from veyro.native_judge import JudgeConfig, read_evidence


@pytest.mark.parametrize("shadow_location", ["cwd", "pythonpath"])
def test_repository_cannot_shadow_the_judge(tmp_path, monkeypatch, shadow_location):
    shadow = tmp_path if shadow_location == "cwd" else tmp_path / "injected"
    package = shadow / "veyro"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    (package / "native_judge.py").write_text('print(\'{"status":"passed","scores":{"done":1}}\')')
    monkeypatch.setenv("PYTHONPATH", str(shadow))
    monkeypatch.delenv("VEYRO_TEST_NO_JUDGE_KEY", raising=False)
    (tmp_path / "artifact.txt").write_text("example")
    config = JudgeConfig(
        rubric_version="test",
        criteria={"done": "The artifact is complete."},
        evidence_files=["artifact.txt"],
        provider={"api_key_env": "VEYRO_TEST_NO_JUDGE_KEY"},
    )
    result = run_judge(
        {"judge": config.model_dump(mode="json"), "repository": str(tmp_path), "task": "Example"},
        [{"exit_code": 0, "timed_out": False}],
        10,
    )
    assert result["status"] == "error"
    assert result["error"] == "missing_judge_credentials"


@pytest.mark.parametrize("link", [False, True])
def test_known_credential_file_is_never_evidence(tmp_path, link):
    (tmp_path / ".env").write_text("TYPESAFE_API_KEY=invented-test-secret")
    name = "artifact.txt" if link else ".env"
    if link:
        (tmp_path / name).symlink_to(tmp_path / ".env")
    config = JudgeConfig(
        rubric_version="test", criteria={"done": "Complete"}, evidence_files=[name]
    )
    with pytest.raises(ValueError):
        read_evidence(tmp_path, config)


def test_internal_symlink_does_not_expand_the_evidence_allowlist(tmp_path):
    (tmp_path / "private.txt").write_text("not approved as evidence")
    (tmp_path / "artifact.txt").symlink_to(tmp_path / "private.txt")
    config = JudgeConfig(
        rubric_version="test", criteria={"done": "Complete"}, evidence_files=["artifact.txt"]
    )
    with pytest.raises(ValueError):
        read_evidence(tmp_path, config)


def test_machine_output_is_complete_and_has_a_separate_limit(tmp_path):
    payload = {"status": "passed", "scores": {"done": 1}, "provenance": "x" * 12000}
    result = run_check(
        [sys.executable, "-c", f"print({json.dumps(payload)!r})"],
        str(tmp_path),
        5,
        output_limit=256000,
        complete_output=True,
    )
    assert json.loads(result["output"]) == payload
    assert not result["output_truncated"]
    result = run_check(
        [sys.executable, "-c", "print('x' * 300000)"],
        str(tmp_path),
        5,
        output_limit=256000,
        complete_output=True,
    )
    assert result["output_truncated"]


def test_truncated_judge_response_is_an_explicit_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "veyro.autonomy_check.run_check",
        lambda *_args, **_kwargs: {
            "output_truncated": True,
            "latency_ms": 1,
            "output": '{"status":"passed","scores":{"done":1}}',
        },
    )
    result = run_judge({"repository": str(tmp_path), "task": "task", "judge": {}}, [], 1)
    assert result == {"status": "error", "error": "judge_response_too_large", "latency_ms": 1}


@pytest.mark.parametrize("corrupt_judge", [{}, None, False])
def test_present_but_invalid_judge_is_not_silently_disabled(tmp_path, corrupt_judge):
    from veyro.agents import AgentId
    from veyro.autonomy import AutonomyOptions, prepare_autonomy
    from veyro.autonomy_check import checkpoint

    launch = prepare_autonomy(AgentId.PRIME_AGENT, tmp_path, "Task", (), AutonomyOptions(("true",)))
    config_path = launch.directory / "config.json"
    config = json.loads(config_path.read_text())
    config.update(judge=corrupt_judge, task="Task")
    config_path.write_text(json.dumps(config))
    (launch.directory / "plan.md").write_text("Plan")
    checkpoint(config_path, "root", "start", claim=True)
    checkpoint(config_path, "root", "plan")
    result = checkpoint(config_path, "root", "build")
    assert result["action"] == "blocked"
    assert result["reason"] == "judge_error"
