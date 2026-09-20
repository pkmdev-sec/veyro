from __future__ import annotations

import json
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from typer.testing import CliRunner

from veyro.agents import AgentId
from veyro.autonomy import AutonomyOptions, prepare_autonomy
from veyro.autonomy_check import checkpoint, run_check
from veyro.cli import app


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


def write_plan(prepared):
    (prepared.directory / "plan.md").write_text("Implement the fixture, then run its tests.")


def test_plan_build_repair_complete_with_real_commands(tmp_path):
    prepared, config = launch(
        tmp_path, [command("from pathlib import Path; assert Path('done').exists()")]
    )
    assert checkpoint(config, "root", "1")["reason"] == "plan_missing"
    write_plan(prepared)
    assert checkpoint(config, "root", "2")["reason"] == "plan_ready"
    failure = checkpoint(config, "root", "3")
    assert failure["reason"] == "checks_failed"
    assert failure["checks"][0]["exit_code"] == 1
    (tmp_path / "done").touch()
    assert checkpoint(config, "root", "4")["action"] == "complete"
    assert checkpoint(config, "root", "5")["action"] == "ignore"
    events = [
        json.loads(line) for line in (prepared.directory / "events.jsonl").read_text().splitlines()
    ]
    assert [e["reason"] for e in events] == [
        "plan_missing",
        "plan_ready",
        "checks_failed",
        "checks_passed",
    ]


def test_all_checks_must_pass_and_last_budget_turn_can_complete(tmp_path):
    prepared, config = launch(
        tmp_path, [command("pass"), command("raise SystemExit(2)")], max_continuations=1
    )
    write_plan(prepared)
    checkpoint(config, "root", "1")
    assert checkpoint(config, "root", "2")["reason"] == "continuation_limit"
    prepared, config = launch(tmp_path, max_continuations=1)
    write_plan(prepared)
    checkpoint(config, "root", "1")
    assert checkpoint(config, "root", "2")["action"] == "complete"


def test_duplicate_and_foreign_sessions_do_not_dispatch(tmp_path):
    prepared, config = launch(tmp_path)
    write_plan(prepared)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: checkpoint(config, "root", "1"), range(2)))
    assert sorted(r["action"] for r in results) == ["continue", "ignore"]
    assert checkpoint(config, "child", "2")["reason"] == "foreign_session"
    checkpoint(config, "child", "start", claim=True)
    assert json.loads((prepared.directory / "state.json").read_text())["session"] == "root"


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
    assert json.loads(result.stdout)["reason"] == "plan_missing"
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
    else:
        assert "--extension" in prepared.command
        assert "--autonomous" not in prepared.command


@pytest.mark.parametrize("checks", [(), ("",), (" ",)])
def test_checks_required(checks):
    with pytest.raises(ValueError):
        AutonomyOptions(checks)


def test_cli_requires_explicit_autonomy_and_checks(tmp_path):
    for args in [["--autonomous"], ["--check", "true"]]:
        result = CliRunner().invoke(app, ["agent", "prime-agent", "--repo", str(tmp_path), *args])
        assert result.exit_code == 2


def test_older_replayed_turn_is_ignored(tmp_path):
    prepared, config = launch(tmp_path, [command("raise SystemExit(1)")])
    write_plan(prepared)
    checkpoint(config, "root", "plan")
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
    write_plan(prepared)
    checkpoint(config, "root", "plan")
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
    ],
)
def test_native_adapter_callbacks_with_real_checker(tmp_path, scenario):
    import shutil

    if shutil.which("node") is None:
        pytest.skip("Node is needed for native JavaScript adapter contracts")
    prepared, config = launch(
        tmp_path, [command("from pathlib import Path; assert Path('done').exists()")]
    )
    write_plan(prepared)
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
def test_invalid_time_budgets_are_rejected(timeout):
    with pytest.raises(ValueError):
        AutonomyOptions(("true",), timeout=timeout)


def test_native_assets_are_declared_for_wheel_and_source_distribution():
    import tomllib

    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert "integrations/*.mjs" in project["tool"]["setuptools"]["package-data"]["veyro"]
    assert "recursive-include tests *.py *.mjs" in (root / "MANIFEST.in").read_text()
