from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.table import Table

from veyro.agents import AgentId, launch_native_agent, probe_agents, probes_json
from veyro.local_cli import evaluator_app, local_app
from veyro.model_import import (
    GLIFORMER_LARGE_V1_WEIGHTS,
    ArtifactImportError,
    import_pinned_artifact,
)
from veyro.selene_cli import selene_app

app = typer.Typer(
    name="veyro",
    help=(
        "Observe native coding sessions, apply policy-gated controls, and run explicit "
        "local experiments."
    ),
    no_args_is_help=True,
)
app.add_typer(local_app, name="local")
app.add_typer(evaluator_app, name="evaluator")
app.add_typer(selene_app, name="selene")
console = Console()


@app.command("supervise")
def supervise_existing_session(
    agent: Annotated[Literal["prime-agent", "opencode"], typer.Option("--agent")],
    session: Annotated[str, typer.Option("--session")],
    repo: Annotated[Path, typer.Option("--repo", exists=True, file_okay=False, resolve_path=True)],
    proposal: Annotated[Path, typer.Option("--proposal", help="Private JSON control proposal")],
    ledger_dir: Annotated[
        Path, typer.Option("--ledger-dir", help="Required persistent private decision ledger")
    ],
    policy: Annotated[
        Path | None, typer.Option("--policy", help="Private operator rollout policy")
    ] = None,
    socket: Annotated[Path | None, typer.Option("--socket")] = None,
    server: Annotated[str | None, typer.Option("--server")] = None,
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds", min=1, max=300)] = 180,
) -> None:
    """Evaluate one proposal; default observe-only. Read approval JSON from stdin."""
    import json
    import math

    from veyro.models.rollout import RolloutPolicy
    from veyro.supervision.supervisor import (
        ControlProposal,
        read_operator_file,
        supervise_proposal,
    )

    def emit(record: dict[str, object]) -> None:
        typer.echo(json.dumps(record), nl=True)

    async def run(selected_policy, selected_proposal):
        async with asyncio.timeout(timeout_seconds):
            await supervise_proposal(
                provider=AgentId(agent),
                repository=repo,
                selector=session,
                proposal=selected_proposal,
                policy=selected_policy,
                emit=emit,
                socket=socket,
                server=server,
                ledger_directory=ledger_dir,
            )

    try:
        if not math.isfinite(timeout_seconds):
            raise ValueError("timeout must be finite")
        selected_policy = (
            RolloutPolicy.model_validate_json(read_operator_file(policy))
            if policy
            else RolloutPolicy()
        )
        selected_proposal = ControlProposal.model_validate_json(read_operator_file(proposal))
        asyncio.run(run(selected_policy, selected_proposal))
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except Exception:
        emit(
            {
                "type": "error",
                "detail": "Supervision failed safely; uncertain delivery must not be retried.",
            }
        )
        raise typer.Exit(2) from None


@app.command("sessions")
def discover_existing_sessions(
    agent: Annotated[AgentId, typer.Option("--agent", help="Provider to inspect")],
    repo: Annotated[Path, typer.Option("--repo", exists=True, file_okay=False, resolve_path=True)],
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Existing private Prime socket")
    ] = None,
    server: Annotated[
        str | None, typer.Option("--server", help="Authenticated loopback OpenCode URL")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
) -> None:
    """Discover existing session metadata. Never start or resume a provider."""
    import json

    from veyro.supervision.attachment import AttachmentError, discover_sessions

    try:
        report = asyncio.run(
            discover_sessions(agent, repo, socket=socket, server=server, limit=limit)
        )
    except (OSError, ValueError, RuntimeError, TypeError) as error:
        detail = (
            str(error) if isinstance(error, AttachmentError) else "Session discovery failed safely."
        )
        typer.echo(json.dumps({"type": "error", "detail": detail}))
        raise typer.Exit(2) from None
    typer.echo(report.model_dump_json())


@app.command("attach")
def attach_existing_session(
    agent: Annotated[AgentId, typer.Option("--agent", help="Provider to observe")],
    session: Annotated[str, typer.Option("--session", help="Exact selector from veyro sessions")],
    repo: Annotated[Path, typer.Option("--repo", exists=True, file_okay=False, resolve_path=True)],
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Existing private Prime socket")
    ] = None,
    server: Annotated[
        str | None, typer.Option("--server", help="Authenticated loopback OpenCode URL")
    ] = None,
    watch_seconds: Annotated[float, typer.Option("--watch-seconds", min=0, max=300)] = 0,
    max_events: Annotated[int, typer.Option("--max-events", min=1, max=1000)] = 100,
    after_sequence: Annotated[int, typer.Option("--after-sequence", min=0)] = 0,
) -> None:
    """Read-only attach. Emit a capability report and bounded metadata NDJSON."""
    import json

    from veyro.models import SupervisionEventType
    from veyro.supervision.attachment import AttachmentError, attach_existing

    async def observe() -> None:
        attachment = await attach_existing(
            agent,
            repo,
            session,
            socket=socket,
            server=server,
            after_sequence=after_sequence,
            follow_journal=watch_seconds > 0,
        )
        count = 0
        last_sequence = after_sequence
        reason = "snapshot"
        try:
            typer.echo(
                json.dumps(
                    {"type": "attachment", "report": attachment.report.model_dump(mode="json")}
                )
            )
            journal = attachment.report.candidate.mode == "local_hook_journal"
            if watch_seconds > 0 or journal:
                try:
                    async with asyncio.timeout(watch_seconds or 5):
                        async for event in attachment.events():
                            typer.echo(
                                json.dumps(
                                    {"type": "event", "event": event.model_dump(mode="json")}
                                )
                            )
                            count += 1
                            last_sequence = event.sequence
                            if event.event_type is SupervisionEventType.NORMALIZATION_FAILED:
                                raise AttachmentError(
                                    "Observation source failed; do not assume complete history."
                                )
                            if count >= max_events:
                                reason = "event_limit"
                                break
                except TimeoutError:
                    reason = "deadline"
            typer.echo(
                json.dumps(
                    {
                        "type": "attachment_end",
                        "reason": reason,
                        "events": count,
                        "last_sequence": last_sequence,
                        "incomplete_tail": attachment.incomplete_tail,
                    }
                )
            )
        finally:
            await attachment.close()

    try:
        asyncio.run(observe())
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (OSError, ValueError, RuntimeError, TypeError) as error:
        detail = (
            str(error)
            if isinstance(error, AttachmentError)
            else "Session attachment failed safely."
        )
        typer.echo(json.dumps({"type": "error", "detail": detail}))
        raise typer.Exit(2) from None


@app.command("agents")
def list_agents(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the versioned discovery document")
    ] = False,
) -> None:
    """List native agents and Veyro's supported machine interfaces."""

    if json_output:
        typer.echo(probes_json())
        return
    table = Table(
        "Agent",
        "Available",
        "Observed version",
        "Autonomy qualified",
        "Qualified version",
        "Required autonomy versions",
        "Machine interface",
        "Stability",
        "Capabilities",
    )
    for probe in probe_agents():
        table.add_row(
            str(probe["agent_id"]),
            "yes" if probe["available"] else "no",
            str(probe["version"] or "-"),
            "yes" if probe["version_qualified"] else "no",
            str(probe["qualified_version"] or "-"),
            ", ".join(probe["required_autonomy_versions"]) or "-",
            str(probe["machine_interface"]),
            str(probe["interface_stability"]),
            ", ".join(probe["capabilities"]),
        )
    console.print(table)


@app.command(
    "agent",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run_experimental_agent(
    ctx: typer.Context,
    provider: Annotated[AgentId, typer.Argument(help="Native agent for the pinned local task")],
    repo: Annotated[
        Path,
        typer.Option("--repo", exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    prompt: Annotated[
        str | None, typer.Option("--prompt", help="Optional initial native prompt")
    ] = None,
    autonomous: Annotated[bool, typer.Option("--autonomous")] = False,
    coding_profile: Annotated[
        str | None,
        typer.Option("--coding-profile", help="Pinned local model used for coding"),
    ] = None,
    evaluation_profile: Annotated[
        str | None,
        typer.Option("--evaluation-profile", help="Pinned local model used for typed evaluation"),
    ] = None,
    allow_uncalibrated_judge: Annotated[
        bool,
        typer.Option(
            "--allow-uncalibrated-evaluator",
            help="Allow an uncalibrated advisory evaluator experiment",
        ),
    ] = False,
    check: Annotated[
        list[str] | None, typer.Option("--check", help="Worker-visible repair check; repeatable")
    ] = None,
    max_continuations: Annotated[int, typer.Option("--max-continuations", min=0)] = 6,
    deny_read_path: Annotated[
        list[Path] | None,
        typer.Option(
            "--deny-read-path",
            exists=True,
            resolve_path=True,
            help="Repeatable host path the local coding process must not read",
        ),
    ] = None,
    check_timeout: Annotated[float, typer.Option("--check-timeout", min=0.01)] = 120,
    model_turn_timeout: Annotated[
        float,
        typer.Option("--model-turn-timeout", min=0.01, help="Local model response timeout"),
    ] = 120,
    timeout: Annotated[float, typer.Option("--timeout", min=0.01)] = 1800,
    judge: Annotated[
        Path | None,
        typer.Option(
            "--evaluator",
            exists=True,
            dir_okay=False,
            resolve_path=True,
            help="Typed advisory rubric/provider JSON",
        ),
    ] = None,
) -> None:
    """Experimental: run a pinned local autonomous task; requires a coding profile."""

    def announce(record) -> None:
        console.print(f"Veyro sidecar: [bold]{record.session_id}[/bold]")
        console.print(f"Evidence: {repo / '.veyro' / 'native-sessions' / record.session_id}")

    from veyro.autonomy import AutonomyOptions, CodingModel, HarnessModels

    try:
        local_models = None
        if not autonomous:
            raise ValueError(
                "local-only deployment does not launch unpinned interactive models; "
                "use sessions/attach or --autonomous with --coding-profile"
            )
        if coding_profile is None:
            raise ValueError("local-only autonomy requires --coding-profile")
        if evaluation_profile is not None and coding_profile is None:
            raise ValueError("--evaluation-profile requires --coding-profile")
        if coding_profile is not None:
            local_models = (
                HarnessModels(coding_profile, evaluation_profile)
                if evaluation_profile is not None
                else CodingModel(coding_profile)
            )
        if (check or judge or local_models or allow_uncalibrated_judge) and not autonomous:
            raise ValueError("harness options require --autonomous")
        if evaluation_profile is not None and judge is None:
            raise ValueError("--evaluation-profile requires --evaluator")
        if judge is not None and coding_profile is not None and evaluation_profile is None:
            raise ValueError("local evaluation requires --evaluation-profile")
        options = (
            AutonomyOptions(
                checks=tuple(check or ()),
                max_continuations=max_continuations,
                check_timeout=check_timeout,
                model_turn_timeout=model_turn_timeout,
                timeout=timeout,
                judge_file=judge,
                models=local_models,
                allow_uncalibrated_judge=allow_uncalibrated_judge,
                deny_read_paths=tuple(deny_read_path or ()),
            )
            if autonomous
            else None
        )
        result = launch_native_agent(
            provider,
            repo,
            prompt,
            tuple(ctx.args),
            on_started=announce,
            autonomy=options,
        )
    except ValueError as error:
        console.print(str(error))
        raise typer.Exit(code=2) from error
    except FileNotFoundError as error:
        console.print(str(error))
        raise typer.Exit(code=127) from error
    except OSError as error:
        console.print(f"Native agent launch failed: {error}")
        raise typer.Exit(code=126) from error
    if result.autonomy_directory:
        console.print(f"Autonomy evidence: {result.autonomy_directory}")
    if result.exit_code != 0:
        code = result.exit_code if result.exit_code > 0 else 128 - result.exit_code
        raise typer.Exit(code=code)


@app.command("import-jeff-weights")
def import_jeff_weights(
    source: Annotated[
        str,
        typer.Option(
            "--source",
            help="Local file or approved HTTPS artifact URL",
        ),
    ],
    model_dir: Annotated[
        Path,
        typer.Option("--model-dir", help="jeff GLiFormer model directory"),
    ] = Path("~/.local/share/jeff/models/gliformer-large-v1"),
    ca_bundle: Annotated[
        Path | None,
        typer.Option("--ca-bundle", help="Trusted CA bundle for an HTTPS source"),
    ] = None,
) -> None:
    """Experimental: install the pinned local jeff shadow weight file."""

    destination = model_dir.expanduser() / GLIFORMER_LARGE_V1_WEIGHTS.filename
    try:
        result = import_pinned_artifact(
            source,
            destination,
            GLIFORMER_LARGE_V1_WEIGHTS,
            ca_bundle=ca_bundle,
        )
    except ArtifactImportError as error:
        console.print(f"Weight import refused: {error}")
        raise typer.Exit(code=2) from error

    action = "Installed" if result.installed else "Already verified"
    console.print(f"{action}: {result.destination}")
    console.print(f"Bytes: {result.bytes_written}")
    console.print(f"SHA-256: {result.sha256}")


if __name__ == "__main__":
    app()
