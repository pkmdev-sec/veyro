from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from veyro.bridges.prime_agent import PrimeDaemonClient
from veyro.models import HumanApprovalEvidence
from veyro.supervision.prime_canary import default_socket_path


async def run_canary(*, repository: Path, socket: Path, approved_by: str) -> dict[str, object]:
    client = PrimeDaemonClient(socket, timeout=10)
    active_id = None
    process = None
    stopped = False
    temporary = tempfile.TemporaryDirectory(prefix="veyro-rollout-canary-")
    try:
        await client.connect()
        created = await client.request(
            {
                "type": "create",
                "noSession": True,
                "name": "veyro-sup015-executable-canary",
                "lifecycle": "resident",
                "config": {"cwd": str(repository)},
            }
        )
        active_id = created.get("data", {}).get("activeSessionId")
        if not isinstance(active_id, str):
            raise RuntimeError("native fixture identity missing")
        private = await asyncio.to_thread(Path(temporary.name).resolve)
        policy = private / "policy.json"
        proposal = private / "proposal.json"
        for path, data in [
            (policy, {"mode": "approval_required"}),
            (
                proposal,
                {
                    "command_id": "sup015-stop-fixture",
                    "operation": "unknown",
                    "intent": {
                        "action": "stop_session",
                        "reason": "Stop only this disposable no-prompt fixture.",
                    },
                },
            ),
        ]:
            await asyncio.to_thread(path.write_text, json.dumps(data))
            await asyncio.to_thread(path.chmod, 0o600)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "veyro.cli",
            "supervise",
            "--agent",
            "prime-agent",
            "--repo",
            str(repository),
            "--socket",
            str(socket),
            "--session",
            active_id,
            "--policy",
            str(policy),
            "--proposal",
            str(proposal),
            "--ledger-dir",
            str(private / "ledger"),
            "--timeout-seconds",
            "180",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=65537,
        )
        assert process.stdin is not None and process.stdout is not None
        digest = None
        decision = None
        async with asyncio.timeout(190):
            for _ in range(5):
                line = await process.stdout.readline()
                if not line:
                    break
                record = json.loads(line)
                if record.get("type") == "supervision":
                    identity = record["identity"]
                    if (
                        identity["provider_session_id"] != active_id
                        or identity["repository"] != str(repository)
                        or record["mode"] != "approval_required"
                    ):
                        raise RuntimeError("fixture supervision binding mismatch")
                    digest = record["request_sha256"]
                elif record.get("type") == "approval_required":
                    auth = record["authorization"]
                    if (
                        digest is None
                        or auth["request_sha256"] != digest
                        or auth["action"] != "stop_session"
                        or auth["command_id"] != "sup015-stop-fixture"
                    ):
                        raise RuntimeError("fixture approval binding mismatch")
                    now = datetime.now(UTC)
                    approval = HumanApprovalEvidence(
                        approval_id="sup015-explicit-fixture-stop",
                        request_sha256=digest,
                        decision="approve",
                        approved_by=approved_by,
                        issued_at=now,
                        expires_at=now + timedelta(minutes=2),
                    )
                    process.stdin.write(approval.model_dump_json().encode() + b"\n")
                    await process.stdin.drain()
                elif record.get("type") == "decision":
                    decision = record
            await process.wait()
        if (
            process.returncode != 0
            or decision is None
            or decision["authorization"]["outcome"] != "authorized"
            or (decision.get("delivery") or {}).get("outcome") != "executed"
            or decision.get("verification") not in {"session_failed", "session_completed"}
        ):
            reason = decision["authorization"]["reason"] if decision else "no_decision"
            raise RuntimeError("executable fixture stop failed: " + reason)
        remaining = await client.request({"type": "list", "includeClientOwned": True})
        if any(row.get("activeSessionId") == active_id for row in remaining["data"]["sessions"]):
            raise RuntimeError("fixture remained after verified stop")
        stopped = True
        claims = await asyncio.to_thread(lambda: list((private / "ledger").glob("*.json")))
        if len(claims) != 1:
            raise RuntimeError("fixture did not persist exactly one delivery claim")
        return {
            "status": "passed",
            "provider": "prime-agent",
            "version": "0.9.5",
            "executable": True,
            "mode": "approval_required",
            "stdin_approval": True,
            "control": "executed",
            "verification": decision["verification"],
            "checkpoint_assessed": decision["checkpoint_id"] is not None,
            "delivery_claims": 1,
            "prompt_sent": False,
            "native_fixture_removed": True,
        }
    finally:
        try:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            if active_id is not None and not stopped:
                await client.request({"type": "kill", "activeSessionId": active_id})
        finally:
            try:
                await client.close()
            finally:
                temporary.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify approved CLI control on a disposable Prime fixture"
    )
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--approve-stop", action="store_true")
    args = parser.parse_args()
    if not args.approve_stop:
        parser.error("--approve-stop is required")
    report = asyncio.run(
        run_canary(repository=args.repo.resolve(), socket=args.socket, approved_by=args.approved_by)
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
