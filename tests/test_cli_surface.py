from typer.main import get_command
from typer.testing import CliRunner

from veyro.cli import app

runner = CliRunner()


def test_root_help_advertises_supported_and_experimental_surfaces() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    commands = get_command(app).commands
    assert set(commands) == {
        "agents",
        "sessions",
        "attach",
        "supervise",
        "agent",
        "local",
        "evaluator",
        "selene",
        "import-jeff-weights",
    }
    assert {"run", "demo", "inspect", "runs"}.isdisjoint(commands)
    assert "Experimental" in result.output


def test_every_experimental_surface_labels_its_help() -> None:
    for command in ("agent", "local", "evaluator", "selene", "import-jeff-weights"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, command
        assert "experimental" in result.output.lower(), command

    agent_help = runner.invoke(app, ["agent", "--help"]).output
    assert "pinned local autonomous task" in agent_help
    assert "coding profile" in agent_help
