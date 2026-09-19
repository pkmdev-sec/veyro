from __future__ import annotations

import asyncio
import os
from datetime import UTC
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from foreman.agents import AgentId, launch_native_agent, probe_agents, probes_json
from foreman.config import FactoryConfig
from foreman.foreman import (
    FakeForemanModel,
    ForemanModel,
    JevForemanModel,
    ShadowingForemanModel,
)
from foreman.model_import import (
    GLIFORMER_LARGE_V1_WEIGHTS,
    ArtifactImportError,
    import_pinned_artifact,
)
from foreman.models import EventType, FactoryStatus, WorkerType
from foreman.persistence import PersistenceError, RunStore
from foreman.runtime import FactoryRuntime
from foreman.terminal import TerminalRenderer, duration_label, elapsed_label
from foreman.workers import FakeWorker

app = typer.Typer(
    name="foreman",
    help="Supervise native coding agents with a fast semantic decision loop.",
    no_args_is_help=True,
)
console = Console()


@app.command("supervise")
def supervise_existing_session(
    agent: Annotated[AgentId, typer.Option("--agent")],
    session: Annotated[str, typer.Option("--session")],
    repo: Annotated[Path, typer.Option("--repo", exists=True, file_okay=False, resolve_path=True)],
    proposal: Annotated[Path, typer.Option("--proposal", help="Private JSON control proposal")],
    policy: Annotated[
        Path | None, typer.Option("--policy", help="Private operator rollout policy")
    ] = None,
    ledger_dir: Annotated[
        Path | None, typer.Option("--ledger-dir", help="Persistent private no-retry ledger")
    ] = None,
    socket: Annotated[Path | None, typer.Option("--socket")] = None,
    server: Annotated[str | None, typer.Option("--server")] = None,
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds", min=1, max=300)] = 180,
) -> None:
    """Evaluate one proposal; default observe-only. Read approval JSON from stdin."""
    import json
    import math

    from foreman.models.rollout import RolloutPolicy
    from foreman.supervision.supervisor import (
        ControlProposal,
        read_operator_file,
        supervise_proposal,
    )

    def emit(record: dict[str, object]) -> None:
        typer.echo(json.dumps(record), nl=True)

    async def run(selected_policy, selected_proposal):
        async with asyncio.timeout(timeout_seconds):
            await supervise_proposal(
                provider=agent,
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

    from foreman.supervision.attachment import AttachmentError, discover_sessions

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
    session: Annotated[str, typer.Option("--session", help="Exact selector from foreman sessions")],
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

    from foreman.models import SupervisionEventType
    from foreman.supervision.attachment import AttachmentError, attach_existing

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
    """List native agents and Foreman's supported machine interfaces."""

    if json_output:
        typer.echo(probes_json())
        return
    table = Table("Agent", "Available", "Version", "Machine interface", "Stability", "Capabilities")
    for probe in probe_agents():
        table.add_row(
            str(probe["agent_id"]),
            "yes" if probe["available"] else "no",
            str(probe["version"] or "-"),
            str(probe["machine_interface"]),
            str(probe["interface_stability"]),
            ", ".join(probe["capabilities"]),
        )
    console.print(table)


@app.command(
    "agent",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def open_agent(
    ctx: typer.Context,
    provider: Annotated[AgentId, typer.Argument(help="Native agent to open")],
    repo: Annotated[
        Path,
        typer.Option("--repo", exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    prompt: Annotated[
        str | None, typer.Option("--prompt", help="Optional initial native prompt")
    ] = None,
) -> None:
    """Run a provider's native interactive harness with a Foreman sidecar."""

    def announce(record) -> None:
        console.print(f"Foreman sidecar: [bold]{record.session_id}[/bold]")
        console.print(f"Evidence: {repo / '.foreman' / 'native-sessions' / record.session_id}")

    try:
        result = launch_native_agent(
            provider,
            repo,
            prompt,
            tuple(ctx.args),
            on_started=announce,
        )
    except FileNotFoundError as error:
        console.print(str(error))
        raise typer.Exit(code=127) from error
    except OSError as error:
        console.print(f"Native agent launch failed: {error}")
        raise typer.Exit(code=126) from error
    if result.exit_code != 0:
        code = result.exit_code if result.exit_code > 0 else 128 - result.exit_code
        raise typer.Exit(code=code)


def _run_async(runtime: FactoryRuntime) -> FactoryStatus:
    try:
        state = asyncio.run(runtime.run())
    except KeyboardInterrupt:
        console.print("\nFactory interrupted.")
        raise typer.Exit(code=130) from None
    return state.status


@app.command()
def run(
    repo: Annotated[
        Path,
        typer.Option("--repo", exists=True, file_okay=False, resolve_path=True, help="Repository"),
    ],
    job: Annotated[str, typer.Option("--job", help="Any free-form software job")],
    agent: Annotated[
        AgentId | None,
        typer.Option("--agent", help="Native coding agent (default: configuration or Codex)"),
    ] = None,
) -> None:
    """Launch a native coding agent supervised by TypeSafe AI Jev."""

    load_dotenv(repo / ".env", override=False)
    load_dotenv(override=False)
    config = FactoryConfig.from_environment()
    if agent is not None:
        config = config.model_copy(update={"agent_provider": agent})
    provider = config.jev_provider
    api_key = os.getenv(provider.api_key_env)
    if not api_key:
        console.print(
            f"Missing {provider.api_key_env} for semantic provider {provider.provider_id!r}."
        )
        raise typer.Exit(code=2)
    authoritative_model = JevForemanModel(
        provider_id=provider.provider_id,
        base_url=str(provider.base_url),
        api_key=api_key,
        model=provider.request_model,
        checkpoint=provider.checkpoint,
        role="authoritative",
        timeout_seconds=provider.timeout_seconds,
        max_state_characters=provider.max_state_characters,
        state_format=provider.state_format,
    )
    model: ForemanModel = authoritative_model
    if shadow := config.jev_shadow_provider:
        shadow_api_key = os.getenv(shadow.api_key_env)
        if not shadow_api_key:
            console.print(
                f"Missing {shadow.api_key_env} for shadow provider {shadow.provider_id!r}."
            )
            raise typer.Exit(code=2)
        model = ShadowingForemanModel(
            authoritative_model,
            {
                shadow.provider_id: JevForemanModel(
                    provider_id=shadow.provider_id,
                    base_url=str(shadow.base_url),
                    api_key=shadow_api_key,
                    model=shadow.request_model,
                    checkpoint=shadow.checkpoint,
                    role="shadow",
                    timeout_seconds=shadow.timeout_seconds,
                    max_state_characters=shadow.max_state_characters,
                    state_format=shadow.state_format,
                )
            },
        )
    runtime = FactoryRuntime(
        repository=repo,
        job=job,
        model=model,
        config=config,
        event_sink=TerminalRenderer(console),
    )
    status = _run_async(runtime)
    console.print(f"Run ID: [bold]{runtime.state.run_id}[/bold]")
    if status is not FactoryStatus.FINISHED:
        raise typer.Exit(code=1)


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
    """Verify and atomically install the pinned GLiFormer weight file."""

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


@app.command()
def demo(
    repo: Annotated[
        Path,
        typer.Option("--repo", exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    job: Annotated[str, typer.Option("--job")] = (
        "Add rate limiting to the API and make sure it is properly tested."
    ),
) -> None:
    """Run a deterministic local simulation with no credentials or external services."""

    config = FactoryConfig(
        assessment_min_interval_seconds=0.04,
        periodic_assessment_seconds=0.25,
        worker_timeout_seconds=5.0,
        overall_timeout_seconds=10.0,
        max_workers=2,
        max_retries=1,
        max_iterations=8,
    )

    def simulated_worker(worker_type: WorkerType) -> FakeWorker:
        if worker_type is WorkerType.VERIFIER:
            return FakeWorker(
                output_lines=["Independently verifying requirements and tests"],
                delay_seconds=0.02,
            )
        return FakeWorker(
            output_lines=["Implementing the requested change", "Running relevant tests"],
            delay_seconds=0.07,
        )

    runtime = FactoryRuntime(
        repository=repo,
        job=job,
        model=FakeForemanModel(),
        config=config,
        worker_factory=simulated_worker,
        event_sink=TerminalRenderer(console),
    )
    status = _run_async(runtime)
    console.print(f"Demo run ID: [bold]{runtime.state.run_id}[/bold]")
    if status is not FactoryStatus.FINISHED:
        raise typer.Exit(code=1)


@app.command("inspect")
def inspect_run(
    run_id: Annotated[str, typer.Argument(help="Run identifier")],
    repo: Annotated[
        Path,
        typer.Option("--repo", exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    """Reconstruct a persisted factory timeline."""

    store = RunStore(repo)
    try:
        state = store.load_state(run_id)
        events = store.load_events(run_id)
    except PersistenceError as error:
        console.print(str(error))
        raise typer.Exit(code=2) from error

    finished = state.finished_at or state.updated_at
    duration = max(0.0, (finished - state.started_at).total_seconds())
    console.print(f"[bold]FOREMAN RUN: {state.run_id}[/bold]")
    console.print("Job:")
    console.print(state.job)
    console.print(f"Duration: {duration_label(duration)}")
    console.print(f"Workers: {len(state.workers)}")
    console.print(f"Assessments: {len(state.assessment_history)}")
    console.print(f"Result: {state.status.value}")

    for event in events:
        offset = (event.timestamp - state.started_at).total_seconds()
        prefix = elapsed_label(offset)
        payload = event.payload
        if event.event_type is EventType.FACTORY_STARTED:
            console.print(f"{prefix}  Factory started")
        elif event.event_type in {EventType.WORKER_STARTED, EventType.VERIFICATION_STARTED}:
            label = (
                "Verification worker"
                if event.event_type is EventType.VERIFICATION_STARTED
                else "Worker"
            )
            console.print(f"{prefix}  {label} {payload.get('worker_id')} started")
        elif event.event_type in {
            EventType.WORKER_COMPLETED,
            EventType.WORKER_FAILED,
            EventType.WORKER_STOPPED,
            EventType.VERIFICATION_COMPLETED,
        }:
            console.print(
                f"{prefix}  {payload.get('worker_id')} {str(payload.get('status', '')).upper()}"
            )
        elif event.event_type is EventType.FOREMAN_ASSESSED:
            assessment = payload["assessment"]
            console.print(f"{prefix}  Foreman assessment")
            provenance = assessment.get("provenance")
            if provenance:
                console.print(
                    f"       provider         {provenance['provider_id']} "
                    f"({provenance['role']}, {provenance['checkpoint']})"
                )
            for label, key in [
                ("implementation", "implementation_complete"),
                ("requirements", "requirements_satisfied"),
                ("tests", "tests_sufficient"),
                ("verification", "needs_verification"),
                ("progress", "meaningful_progress"),
                ("stuck", "worker_stuck"),
            ]:
                console.print(f"       {label:<16} {float(assessment[key]):.2f}")
        elif event.event_type is EventType.FOREMAN_ASSESSMENT_FAILED:
            console.print(
                f"{prefix}  Shadow {payload.get('provider_id')} failed: {payload.get('message')}"
            )
        elif event.event_type is EventType.FOREMAN_INTERVENED:
            console.print(f"       {payload.get('action')}: {payload.get('reason', '')}")
        elif event.event_type in {EventType.WORKER_STEERED, EventType.WORKER_STEER_FAILED}:
            label = "steered" if event.event_type is EventType.WORKER_STEERED else "steer failed"
            console.print(f"{prefix}  {payload.get('worker_id')} {label}")
        elif event.event_type in {
            EventType.FACTORY_FINISHED,
            EventType.FACTORY_ESCALATED,
            EventType.FACTORY_FAILED,
        }:
            console.print(f"{prefix}  {event.event_type.value}")


@app.command()
def runs(
    repo: Annotated[
        Path,
        typer.Option("--repo", exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    """List recent runs stored under the repository's .foreman directory."""

    table = Table("Run", "Status", "Updated", "Workers", "Job")
    for state in RunStore(repo).list_states():
        updated = state.updated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
        job = state.job.replace("\n", " ")
        table.add_row(state.run_id, state.status.value, updated, str(len(state.workers)), job[:70])
    console.print(table)


if __name__ == "__main__":
    app()
