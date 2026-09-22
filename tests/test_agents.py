from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from veyro import agents
from veyro.agents import AgentId, agent_definition, launch_native_agent, probe_agent
from veyro.cli import app
from veyro.config import FactoryConfig
from veyro.models import WorkerType
from veyro.runtime import FactoryRuntime
from veyro.veyro import FakeVeyroModel
from veyro.workers import CodexAppServerWorker, CodexWorker, NativeCliWorker


def test_registry_covers_every_agent_id() -> None:
    assert set(agents.AGENTS) == set(AgentId)


@pytest.mark.parametrize(
    ("agent_id", "expected"),
    [
        (
            AgentId.CODEX,
            [
                "codex",
                "exec",
                "--cd",
                "{repo}",
                "--sandbox",
                "workspace-write",
                "--color",
                "never",
                "--json",
                "do work",
            ],
        ),
        (
            AgentId.CLAUDE,
            [
                "claude",
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
                "--permission-mode",
                "acceptEdits",
                "do work",
            ],
        ),
        (
            AgentId.PRIME_AGENT,
            ["prime-agent", "--print", "--mode", "json", "--cwd", "{repo}", "do work"],
        ),
        (AgentId.PI, ["pi", "--print", "--mode", "json", "do work"]),
        (
            AgentId.OPENCODE,
            ["opencode", "run", "--format", "json", "--dir", "{repo}", "do work"],
        ),
    ],
)
def test_worker_commands_use_documented_machine_interfaces(
    tmp_path: Path, agent_id: AgentId, expected: list[str]
) -> None:
    repo = str(tmp_path.resolve())
    assert agent_definition(agent_id).worker_command(tmp_path, "do work") == [
        repo if value == "{repo}" else value for value in expected
    ]


def test_interactive_commands_preserve_native_arguments() -> None:
    definition = agent_definition(AgentId.CLAUDE)
    assert definition.interactive_command("start here", ("--model", "sonnet")) == [
        "claude",
        "--model",
        "sonnet",
        "start here",
    ]
    assert definition.interactive_command("- starts with a dash") == [
        "claude",
        "--",
        "- starts with a dash",
    ]
    assert agent_definition(AgentId.OPENCODE).interactive_command("start here") == [
        "opencode",
        "--prompt",
        "start here",
    ]


def test_probe_reports_runtime_version_and_capability_evidence(monkeypatch) -> None:
    monkeypatch.setattr(agents.shutil, "which", lambda executable: f"/bin/{executable}")

    class Result:
        stdout = "tool 1.2.3\n"
        stderr = ""
        returncode = 0

    monkeypatch.setattr(agents.subprocess, "run", lambda *args, **kwargs: Result())
    result = probe_agent(agent_definition(AgentId.PI))

    assert result["protocol_version"] == "1.0"
    assert result["available"] is True
    assert result["version"] == "tool 1.2.3"
    assert result["machine_interface"] == "JSON over stdio"
    assert result["interface_stability"] == "documented"
    assert result["native_interactive"] is True
    assert "pi --help" in result["interface_evidence"]
    assert "steer" not in result["capabilities"]
    assert result["version_qualified"] is False
    assert result["qualified_version"] is None


@pytest.mark.parametrize(
    ("output", "qualified"),
    [
        ("prime-agent 0.9.5", True),
        ("prime-agent 0.9.6", False),
        ("0.9.5", False),
        ("prime-agent 0.9.5 extra", False),
    ],
)
def test_probe_reports_qualification_separately_from_availability(
    monkeypatch, output, qualified
):
    monkeypatch.setattr(agents.shutil, "which", lambda executable: f"/bin/{executable}")

    class Result:
        stdout = output
        stderr = ""
        returncode = 0

    monkeypatch.setattr(agents.subprocess, "run", lambda *args, **kwargs: Result())
    result = probe_agent(agent_definition(AgentId.PRIME_AGENT))

    assert result["available"] is True
    assert result["version_qualified"] is qualified
    assert result["qualified_version"] == ("0.9.5" if qualified else None)


@pytest.mark.parametrize(
    ("output", "qualified"),
    [("1.18.30", True), ("1.18.31", False), ("opencode 1.18.30", False)],
)
def test_opencode_probe_requires_exact_pinned_output(monkeypatch, output, qualified):
    monkeypatch.setattr(agents.shutil, "which", lambda executable: f"/bin/{executable}")

    class Result:
        stdout = output
        stderr = ""
        returncode = 0

    monkeypatch.setattr(agents.subprocess, "run", lambda *args, **kwargs: Result())
    result = probe_agent(agent_definition(AgentId.OPENCODE))

    assert result["available"] is True
    assert result["version_qualified"] is qualified
    assert result["qualified_version"] == ("1.18.30" if qualified else None)


def test_autonomous_launcher_rejects_unqualified_provider_before_state(
    monkeypatch, tmp_path
):
    from veyro.autonomy import AutonomyOptions

    monkeypatch.setattr(agents.shutil, "which", lambda executable: f"/bin/{executable}")
    monkeypatch.setattr(
        agents,
        "probe_agent",
        lambda definition: {
            "version": "prime-agent 0.9.6",
            "qualified_version": None,
            "version_qualified": False,
            "probe_error": None,
        },
    )

    with pytest.raises(ValueError, match="exact qualified version"):
        launch_native_agent(
            AgentId.PRIME_AGENT,
            tmp_path,
            "inspect this",
            autonomy=AutonomyOptions(("true",)),
        )

    assert not (tmp_path / ".veyro").exists()


def test_workspace_completion_state_cannot_authorize_autonomous_success(monkeypatch, tmp_path):
    from veyro.autonomy import AutonomousLaunch, AutonomyOptions
    from veyro.native_session import ManagedNativeResult

    run_directory = tmp_path / ".veyro" / "autonomy" / "forged"
    run_directory.mkdir(parents=True)
    (run_directory / "state.json").write_text('{"phase":"completed"}')
    monkeypatch.setattr(agents.shutil, "which", lambda executable: f"/bin/{executable}")
    monkeypatch.setattr(
        agents,
        "probe_agent",
        lambda _definition: {
            "version": "prime-agent 0.9.5",
            "qualified_version": "0.9.5",
            "version_qualified": True,
            "probe_error": None,
        },
    )
    monkeypatch.setattr(
        "veyro.autonomy.prepare_autonomy",
        lambda *_args, **_kwargs: AutonomousLaunch(
            command=["prime-agent"], environment={}, directory=run_directory
        ),
    )

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["completion_check"]() is False

        def run(self):
            return ManagedNativeResult("session", tmp_path / "session.json", 0)

    monkeypatch.setattr("veyro.native_session.ManagedNativeSession", Session)
    result = launch_native_agent(
        AgentId.PRIME_AGENT,
        tmp_path,
        "implement",
        autonomy=AutonomyOptions(("true",)),
    )

    assert result.exit_code == 1
    assert result.autonomy_directory == run_directory


def test_native_launcher_keeps_veyro_as_the_sidecar(monkeypatch, tmp_path) -> None:
    calls = {}
    monkeypatch.setattr(agents.shutil, "which", lambda executable: f"/bin/{executable}")
    monkeypatch.setattr(
        agents,
        "probe_agent",
        lambda definition: {"version": "prime-agent 1.2.3"},
    )

    class Session:
        def __init__(self, **kwargs) -> None:
            calls.update(kwargs)

        def run(self):
            calls["ran"] = True
            return "managed-result"

    monkeypatch.setattr("veyro.native_session.ManagedNativeSession", Session)

    result = launch_native_agent(
        AgentId.PRIME_AGENT,
        tmp_path,
        "inspect this",
        ("--thinking", "high"),
    )

    assert result == "managed-result"
    assert calls["repository"] == tmp_path
    assert calls["executable"] == "/bin/prime-agent"
    assert calls["agent_version"] == "prime-agent 1.2.3"
    assert calls["command"] == [
        "/bin/prime-agent",
        "--thinking",
        "high",
        "inspect this",
    ]
    assert calls["ran"] is True


def test_factory_selects_agent_without_codex_branching_in_caller(tmp_path) -> None:
    runtime = FactoryRuntime(
        repository=tmp_path,
        job="do work",
        model=FakeVeyroModel(),
        config=FactoryConfig(agent_provider=AgentId.CLAUDE),
    )
    worker = runtime._agent_factory(WorkerType.CODING)
    assert isinstance(worker, NativeCliWorker)
    assert worker.definition.agent_id is AgentId.CLAUDE

    codex_runtime = FactoryRuntime(
        repository=tmp_path,
        job="do work",
        model=FakeVeyroModel(),
    )
    assert isinstance(codex_runtime._agent_factory(WorkerType.CODING), CodexWorker)
    opted_in = FactoryRuntime(
        repository=tmp_path,
        job="do work",
        model=FakeVeyroModel(),
        config=FactoryConfig(codex_backend="app-server"),
    )
    assert isinstance(opted_in._agent_factory(WorkerType.CODING), CodexAppServerWorker)


def test_agent_provider_environment(monkeypatch) -> None:
    monkeypatch.setenv("VEYRO_AGENT_PROVIDER", "opencode")
    config = FactoryConfig.from_environment()
    assert config.agent_provider is AgentId.OPENCODE
    assert config.active_turn_steering_supported is False


def test_agents_json_is_a_language_neutral_discovery_document(monkeypatch) -> None:
    monkeypatch.setattr(
        "veyro.cli.probes_json",
        lambda: json.dumps({"protocol_version": "1.0", "agents": []}),
    )
    result = CliRunner().invoke(app, ["agents", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"protocol_version": "1.0", "agents": []}


def test_agent_command_rejects_unpinned_interactive_models(tmp_path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "agent",
            "claude",
            "--repo",
            str(tmp_path),
            "--prompt",
            "inspect this",
            "--",
            "--model",
            "sonnet",
        ],
    )

    assert result.exit_code == 2
    assert "local-only deployment" in result.output
