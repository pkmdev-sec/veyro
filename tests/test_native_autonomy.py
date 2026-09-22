from __future__ import annotations

import json
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from typer.testing import CliRunner

from veyro import autonomy as autonomy_module
from veyro.agents import AgentId
from veyro.autonomy import (
    AutonomyOptions,
    VerifiedCodingModel,
    build_autonomy_plan,
    prepare_autonomy,
)
from veyro.autonomy_check import block, checkpoint, run_check
from veyro.cli import app
from veyro.local_models import LocalModels, VerifiedModel


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

def command(source: str) -> str:
    return shlex.join([sys.executable, "-c", source])


def launch(tmp_path, checks=None, **kwargs):
    prepared = prepare_autonomy(
        AgentId.PRIME_AGENT,
        tmp_path,
        "Implement the fixture",
        (),
        AutonomyOptions(tuple(checks or [command("pass")]), **kwargs),
    )
    config = prepared.directory / "config.json"
    checkpoint(config, "root", "start", claim=True)
    return prepared, config


def test_build_repair_complete_with_real_commands(tmp_path):
    prepared, config = launch(
        tmp_path, [command("from pathlib import Path; assert Path('done').exists()")]
    )
    failure = checkpoint(config, "root", "1")
    assert failure["reason"] == "checks_failed"
    assert "materially different change" in failure["message"]
    assert failure["checks"][0]["exit_code"] == 1
    (tmp_path / "done").touch()
    assert checkpoint(config, "root", "2")["reason"] == "operator_review_required"
    assert checkpoint(config, "root", "3")["action"] == "ignore"
    events = [
        json.loads(line) for line in (prepared.directory / "events.jsonl").read_text().splitlines()
    ]
    assert [e["reason"] for e in events] == ["checks_failed", "operator_review_required"]


def test_all_checks_must_pass_and_last_budget_turn_can_complete(tmp_path):
    prepared, config = launch(
        tmp_path, [command("pass"), command("raise SystemExit(2)")], max_continuations=1
    )
    assert checkpoint(config, "root", "1")["reason"] == "checks_failed"
    assert checkpoint(config, "root", "2")["reason"] == "continuation_limit"
    prepared, config = launch(tmp_path, max_continuations=1)
    assert checkpoint(config, "root", "1")["reason"] == "operator_review_required"


def test_zero_continuations_blocks_after_first_failed_check(tmp_path):
    prepared, config = launch(
        tmp_path,
        [command("raise SystemExit(2)")],
        max_continuations=0,
    )

    decision = checkpoint(config, "root", "1")

    assert decision["action"] == "blocked"
    assert decision["reason"] == "continuation_limit"
    assert decision["continuations"] == 0
    state = json.loads((prepared.directory / "state.json").read_text())
    assert state["phase"] == "blocked"


def test_duplicate_and_foreign_sessions_do_not_dispatch(tmp_path):
    prepared, config = launch(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: checkpoint(config, "root", "1"), range(2)))
    assert sorted(r["action"] for r in results) == ["blocked", "ignore"]
    assert checkpoint(config, "child", "2")["reason"] == "foreign_session"
    checkpoint(config, "child", "start", claim=True)
    assert json.loads((prepared.directory / "state.json").read_text())["session"] == "root"


def test_adapter_error_finalizes_state_and_preserves_terminal_state(tmp_path):
    prepared, config = launch(tmp_path)

    decision = block(config, "native_session_error")

    assert decision["action"] == "blocked"
    assert decision["reason"] == "native_session_error"
    state = json.loads((prepared.directory / "state.json").read_text())
    assert state["phase"] == "blocked"
    assert block(config, "deadline") == decision
    event = json.loads((prepared.directory / "events.jsonl").read_text())
    assert event["turn"] == "adapter-error"


def test_uncertain_checkpoint_and_deadline_block(tmp_path):
    prepared, config = launch(tmp_path)
    state = {"phase": "verifying", "continuations": 0, "session": "root"}
    (prepared.directory / "state.json").write_text(json.dumps(state))
    assert checkpoint(config, "root", "1")["reason"] == "uncertain_checkpoint"
    prepared, config = launch(tmp_path)
    data = json.loads(config.read_text())
    data["deadline"] = 0
    config.write_text(json.dumps(data))
    assert checkpoint(config, "root", "1")["reason"] == "deadline"


def test_timeout_and_bounded_output(tmp_path):
    result = run_check(command("import time; time.sleep(5)"), str(tmp_path), 0.05)
    assert result["timed_out"]
    assert result["latency_ms"] < 2000
    result = run_check(command("print('x' * 100000)"), str(tmp_path), 5)
    assert result["exit_code"] == 0
    assert len(result["output"]) == 8000


def test_standalone_protocol_and_corrupt_state_fail_closed(tmp_path):
    prepared, config = launch(tmp_path)
    checker = Path(__file__).parents[1] / "src/veyro/autonomy_check.py"
    result = subprocess.run(
        [sys.executable, str(checker), str(config), "root", "1"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["reason"] == "operator_review_required"
    (prepared.directory / "state.json").write_text("corrupt")
    result = subprocess.run(
        [sys.executable, str(checker), str(config), "root", "2"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["action"] == "blocked"


@pytest.mark.parametrize("provider", [AgentId.PRIME_AGENT, AgentId.OPENCODE])
def test_launch_is_scoped_and_preserves_native_args(tmp_path, monkeypatch, provider):
    original = {
        "plugin": ["existing"],
        "agent": {"build": {"permission": {"bash": {"rm *": "deny"}}}},
    }
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps(original))
    prepared = prepare_autonomy(
        provider, tmp_path, "Implement", ("--model", "test-model"), AutonomyOptions(("true",))
    )
    assert "test-model" in prepared.command
    assert prepared.directory.stat().st_mode & 0o777 == 0o700
    assert not (tmp_path / "opencode.json").exists()
    if provider is AgentId.OPENCODE:
        config = json.loads(prepared.environment["OPENCODE_CONFIG_CONTENT"])
        assert config["plugin"][0] == "existing"
        assert config["agent"]["build"]["permission"]["bash"] == {"rm *": "deny"}
        assert config["agent"]["build"]["permission"]["question"] == "deny"
        assert "--auto" in prepared.command
        assert "run" in prepared.command
        assert "--format" in prepared.command
        assert "json" in prepared.command
    else:
        assert "--extension" in prepared.command
        assert "--autonomous" not in prepared.command


def test_invalid_opencode_configuration_creates_no_run_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", "not-json")

    with pytest.raises(json.JSONDecodeError):
        prepare_autonomy(
            AgentId.OPENCODE,
            tmp_path,
            "Implement",
            (),
            AutonomyOptions(("true",)),
        )

    assert not (tmp_path / ".veyro").exists()


@pytest.mark.parametrize(
    "override",
    [
        [],
        {"plugin": {}},
        {"agent": []},
        {"agent": {"build": []}},
        {"agent": {"build": {"permission": []}}},
    ],
)
def test_invalid_opencode_shape_creates_no_run_state(tmp_path, monkeypatch, override):
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps(override))

    with pytest.raises(ValueError):
        prepare_autonomy(
            AgentId.OPENCODE,
            tmp_path,
            "Implement",
            (),
            AutonomyOptions(("true",)),
        )

    assert not (tmp_path / ".veyro").exists()


def test_artifact_write_failure_never_publishes_run_directory(tmp_path, monkeypatch):
    plan = build_autonomy_plan(
        AgentId.OPENCODE,
        tmp_path,
        "Implement",
        (),
        AutonomyOptions(("true",)),
    )
    real_open = autonomy_module.os.open

    def fail_state(path, flags, mode=0o777, **kwargs):
        if Path(path).name == "state.json":
            raise OSError("injected write failure")
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(autonomy_module.os, "open", fail_state)
    with pytest.raises(OSError, match="injected write failure"):
        plan.commit()

    assert not plan.directory.exists()
    assert not (tmp_path / ".veyro").exists()


def test_opencode_checkpoint_waits_through_partial_state_write(tmp_path, monkeypatch):
    from veyro import opencode_autonomy as runner

    previous = {"decision": {"action": "continue", "continuations": 1}}
    blocked = {"phase": "blocked", "decision": {"action": "blocked"}}
    reads = iter([{}, {}, {"phase": "verifying"}, previous, blocked])
    monkeypatch.setattr(runner, "_read_json", lambda path: next(reads))
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    assert runner._await_checkpoint(
        tmp_path / "state.json", runner._decision_marker(previous)
    ) == blocked


def test_opencode_runner_ends_at_operator_review_boundary(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"phase": "building"}))
    calls = tmp_path / "calls.jsonl"
    fake = tmp_path / "fake_agent.py"
    fake.write_text(
        """import json, subprocess, sys
from pathlib import Path
state = Path(sys.argv[1])
calls = Path(sys.argv[2])
with calls.open('a') as stream:
    stream.write(json.dumps(sys.argv[3:]) + '\\n')
entries = calls.read_text().splitlines()
if len(entries) <= 2:
    value = {'phase': 'building', 'session': 'same-session',
        'decision': {'action': 'continue', 'message': f'repair {len(entries)}',
            'continuations': len(entries)}}
else:
    value = {'phase': 'blocked', 'session': 'same-session',
        'decision': {'action': 'blocked', 'reason': 'operator_review_required'}}
subprocess.Popen([sys.executable, '-c',
    'import json, sys, time; from pathlib import Path; p = Path(sys.argv[1]); '
    'time.sleep(.05); intermediate = json.loads(p.read_text()); '
    'intermediate["phase"] = "verifying"; p.write_text(json.dumps(intermediate)); '
    'time.sleep(.1); p.write_text(sys.argv[2])', str(state), json.dumps(value)],
    start_new_session=True)
raise SystemExit(7)
"""
    )
    runner = Path(__file__).parents[1] / "src/veyro/opencode_autonomy.py"
    result = subprocess.run(
        [
            sys.executable,
            str(runner),
            str(state),
            sys.executable,
            str(fake),
            str(state),
            str(calls),
            "initial task",
        ],
        check=False,
        timeout=5,
    )
    assert result.returncode == 7
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert invocations == [
        ["initial task"],
        ["--session", "same-session", "repair 1"],
        ["--session", "same-session", "repair 2"],
    ]


def test_opencode_runner_does_not_replay_an_unchanged_repair_decision(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"phase": "building"}))
    calls = tmp_path / "calls"
    fake = tmp_path / "stalled_agent.py"
    fake.write_text(
        """import json, sys
from pathlib import Path
state = Path(sys.argv[1])
calls = Path(sys.argv[2])
calls.write_text(calls.read_text() + 'x' if calls.exists() else 'x')
state.write_text(json.dumps({'phase': 'building', 'session': 'same-session',
    'decision': {'action': 'continue', 'message': 'repair', 'continuations': 1}}))
"""
    )
    runner = Path(__file__).parents[1] / "src/veyro/opencode_autonomy.py"
    result = subprocess.run(
        [
            sys.executable,
            str(runner),
            str(state),
            sys.executable,
            str(fake),
            str(state),
            str(calls),
            "initial task",
        ],
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert calls.read_text() == "xx"


def test_local_opencode_launch_bounds_native_context(tmp_path):
    from veyro.autonomy import CodingModel

    prepared = prepare_autonomy(
        AgentId.OPENCODE,
        tmp_path,
        "Implement",
        (),
        AutonomyOptions(("true",), models=CodingModel("14b"), model_turn_timeout=45),
    )
    config = json.loads(prepared.environment["OPENCODE_CONFIG_CONTENT"])
    build = config["agent"]["build"]
    model = config["provider"]["veyro-local"]["models"]["qwen3:14b"]
    provider_options = config["provider"]["veyro-local"]["options"]

    assert json.loads((prepared.directory / "state.json").read_text())["phase"] == "building"
    assert "plan_path" not in json.loads((prepared.directory / "config.json").read_text())
    assert build["steps"] == 8
    assert build["tools"] == {
        "skill": False,
        "task": False,
        "todowrite": False,
        "webfetch": False,
        "websearch": False,
    }
    assert model["limit"] == {"context": 16384, "output": 1024}
    assert provider_options["headerTimeout"] == 45000
    assert provider_options["chunkTimeout"] == 45000
    assert provider_options["timeout"] == 45000
    assert "maxMessageBytes" in (prepared.directory / "adapter.mjs").read_text()


@pytest.mark.parametrize("checks", [(), ("",), (" ",)])
def test_checks_required(checks):
    with pytest.raises(ValueError):
        AutonomyOptions(checks)


def test_cli_requires_local_profile_and_explicit_autonomy(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "veyro.agents.probe_agent",
        lambda _definition: {
            "qualified_version": "1.18.30",
            "version_qualified": True,
            "probe_error": None,
            "version": "1.18.30",
        },
    )
    cases = [
        [],
        ["--autonomous"],
        ["--check", "true"],
        ["--autonomous", "--check", "true"],
    ]
    for args in cases:
        result = CliRunner().invoke(app, ["agent", "opencode", "--repo", str(tmp_path), *args])
        assert result.exit_code == 2

    result = CliRunner().invoke(
        app,
        [
            "agent",
            "opencode",
            "--repo",
            str(tmp_path),
            "--autonomous",
            "--check",
            "true",
            "--coding-profile",
            "small",
            "--prompt",
            "Implement",
            "--",
            "--model",
            "remote/model",
        ],
    )
    assert result.exit_code == 2
    assert "native model overrides" in result.output


def test_older_replayed_turn_is_ignored(tmp_path):
    prepared, config = launch(tmp_path, [command("raise SystemExit(1)")])
    checkpoint(config, "root", "one")
    checkpoint(config, "root", "two")
    assert checkpoint(config, "root", "one")["reason"] == "duplicate_turn"


def test_checker_cancellation_reaps_detached_verifier(tmp_path):
    import os
    import select
    import signal

    ready = tmp_path / "ready"
    os.mkfifo(ready)
    reader = os.open(ready, os.O_RDONLY | os.O_NONBLOCK)
    child = None
    checker = Path(__file__).parents[1] / "src/veyro/autonomy_check.py"
    script = (
        "import os, time; from pathlib import Path; "
        "Path('pid').write_text(str(os.getpid())); "
        "Path('ready').write_text('ready'); time.sleep(30)"
    )
    prepared, config = launch(tmp_path, [command(script)])
    try:
        child = subprocess.Popen(
            [sys.executable, str(checker), str(config), "root", "build"], stdout=subprocess.PIPE
        )
        assert select.select([reader], [], [], 5)[0], "verifier did not start"
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=5) == 128 + signal.SIGTERM
        verifier_pid = int((tmp_path / "pid").read_text())
        # A killed orphan can briefly be a zombie before the OS reaps it.
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(verifier_pid)], capture_output=True, text=True
        )
        assert not status.stdout.strip() or status.stdout.strip().startswith("Z")
    finally:
        os.close(reader)
        if child and child.poll() is None:
            child.kill()
            child.wait()


def test_local_model_recovery_unloads_runner_and_records_result(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from veyro.agents import _recover_local_model
    from veyro.autonomy import AutonomousLaunch

    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="stopped", stderr="")

    monkeypatch.setattr("veyro.agents.subprocess.run", run)
    identity = VerifiedCodingModel(
        profile_id="14b",
        requested_model="qwen3:14b",
        manifest_sha256="a" * 64,
        blob_sha256="b" * 64,
    )
    launch = AutonomousLaunch([], {}, tmp_path, identity)
    _recover_local_model(launch)

    assert calls[0][0] == ["ollama", "stop", "qwen3:14b"]
    assert calls[0][1]["timeout"] == 30
    record = json.loads((tmp_path / "model-recovery.json").read_text())
    assert record == {
        "coding_model": {
            "profile": "14b",
            "requested_model": "qwen3:14b",
            "manifest_sha256": "a" * 64,
            "blob_sha256": "b" * 64,
        },
        "exit_code": 0,
        "output": "stopped",
    }


def test_native_deadline_escalates_and_reports_timeout(tmp_path):
    import time

    from veyro.agents import agent_definition
    from veyro.native_session import ManagedNativeSession

    session = ManagedNativeSession(
        definition=agent_definition(AgentId.PRIME_AGENT),
        repository=tmp_path,
        command=[
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ],
        executable=sys.executable,
        agent_version=None,
        prompt="fixture",
        timeout_seconds=0.3,
        termination_grace_seconds=0.1,
    )
    started = time.monotonic()
    assert session.run().exit_code == 124
    assert time.monotonic() - started < 2


def test_successful_autonomy_is_not_killed_by_old_deadline(tmp_path):
    from veyro.agents import agent_definition
    from veyro.native_session import ManagedNativeSession

    session = ManagedNativeSession(
        definition=agent_definition(AgentId.PRIME_AGENT),
        repository=tmp_path,
        command=[sys.executable, "-c", "import time; time.sleep(.2)"],
        executable=sys.executable,
        agent_version=None,
        prompt="fixture",
        timeout_seconds=0.05,
        completion_check=lambda: True,
    )
    assert session.run().exit_code == 0


@pytest.mark.parametrize(
    "scenario",
    [
        "prime-success",
        "prime-aborted",
        "prime-blocked",
        "prime-switch",
        "opencode-success",
        "opencode-cancel",
        "opencode-busy",
        "opencode-local-bounds",
    ],
)
def test_native_adapter_callbacks_with_real_checker(tmp_path, scenario):
    import shutil

    if shutil.which("node") is None:
        pytest.skip("Node is needed for native JavaScript adapter contracts")
    prepared, config = launch(
        tmp_path, [command("from pathlib import Path; assert Path('done').exists()")]
    )
    root = Path(__file__).parents[1]
    process = subprocess.run(
        [
            "node",
            str(root / "tests/native_autonomy_adapters.mjs"),
            scenario,
            str(config),
            sys.executable,
            str(root / "src/veyro/autonomy_check.py"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 0, process.stdout + process.stderr


@pytest.mark.parametrize("native_arg", ["--resume", "--session=old", "--autonomous", "--pure"])
def test_conflicting_native_modes_are_rejected(tmp_path, native_arg):
    with pytest.raises(ValueError, match="new session"):
        prepare_autonomy(
            AgentId.PRIME_AGENT, tmp_path, "Task", (native_arg,), AutonomyOptions(("true",))
        )


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1])
@pytest.mark.parametrize("field", ["timeout", "check_timeout", "model_turn_timeout"])
def test_invalid_time_budgets_are_rejected(timeout, field):
    with pytest.raises(ValueError):
        AutonomyOptions(("true",), **{field: timeout})


def test_native_assets_are_declared_for_wheel_and_source_distribution():
    import tomllib

    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert "integrations/*.mjs" in project["tool"]["setuptools"]["package-data"]["veyro"]
    assert "recursive-include tests *.py *.mjs" in (root / "MANIFEST.in").read_text()
