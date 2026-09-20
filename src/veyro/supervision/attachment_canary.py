from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from veyro.agents import AgentId
from veyro.models.attachment import AttachmentReport, SessionDiscovery


async def run_canary(
    *,
    provider: AgentId,
    repository: Path,
    selector: str,
    socket: Path | None = None,
    server: str | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    """Exercise the executable against an existing session without sending work."""
    common = ["--agent", provider.value, "--repo", str(repository)]
    if socket is not None:
        common.extend(["--socket", str(socket)])
    if server is not None:
        common.extend(["--server", server])

    async def command(operation: str, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "veyro.cli",
            operation,
            *common,
            *args,
            env=environment if environment is not None else os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=20)
            if process.returncode != 0 or len(output) > 1024 * 1024:
                raise RuntimeError("read-only executable canary failed")
            return output.decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    before = SessionDiscovery.model_validate_json(await command("sessions", "--limit", "1000"))
    candidate = next((s for s in before.sessions if s.selector == selector), None)
    if candidate is None:
        raise RuntimeError("selected existing session was not discovered")
    records = [
        json.loads(line)
        for line in (
            await command(
                "attach",
                "--session",
                selector,
                "--watch-seconds",
                "0.1",
                "--max-events",
                "1",
            )
        ).splitlines()
    ]
    if not records or records[0].get("type") != "attachment":
        raise RuntimeError("attachment report is missing")
    report = AttachmentReport.model_validate(records[0]["report"])
    if (
        report.candidate != candidate
        or report.controls_enabled
        or report.content_included
        or records[-1].get("type") != "attachment_end"
    ):
        raise RuntimeError("read-only attachment contract was not satisfied")
    supervised = False
    if provider in {AgentId.PRIME_AGENT, AgentId.OPENCODE}:
        with tempfile.TemporaryDirectory(prefix="veyro-observe-proposal-") as directory:
            proposal = Path(directory) / "proposal.json"
            await asyncio.to_thread(
                proposal.write_text,
                json.dumps(
                    {
                        "command_id": "observe-only-canary",
                        "intent": {
                            "action": "queue_follow_up",
                            "message": "Do not deliver this proposal.",
                        },
                    }
                ),
            )
            await asyncio.to_thread(proposal.chmod, 0o600)
            decisions = [
                json.loads(line)
                for line in (
                    await command(
                        "supervise",
                        "--session",
                        selector,
                        "--proposal",
                        str(proposal),
                    )
                ).splitlines()
            ]
            if (
                decisions[-1].get("type") != "decision"
                or decisions[-1]["authorization"]["reason"] != "observe_only"
                or decisions[-1].get("delivery") is not None
            ):
                raise RuntimeError("observe-only supervision canary violated its rollout policy")
            supervised = True
    after = SessionDiscovery.model_validate_json(await command("sessions", "--limit", "1000"))
    if candidate not in after.sessions:
        raise RuntimeError("original session is no longer discoverable after detach")
    return {
        "provider": provider.value,
        "version": candidate.provider_version,
        "status": "passed",
        "executable": True,
        "existing_session_preserved": True,
        "observation": report.observation,
        "history": report.history.mode,
        "native_history_complete": False,
        "controls_enabled": False,
        "prompt_sent": False,
        "session_created_by_attachment": False,
        "supervise_observe_only": supervised,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only existing-session executable canary")
    parser.add_argument("--agent", type=AgentId, required=True, choices=list(AgentId))
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--server")
    args = parser.parse_args()
    try:
        report = asyncio.run(
            run_canary(
                provider=args.agent,
                repository=args.repo,
                selector=args.session,
                socket=args.socket,
                server=args.server,
            )
        )
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({"status": "failed", "detail": "Existing-session canary failed safely."}))
        raise SystemExit(1) from None
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
