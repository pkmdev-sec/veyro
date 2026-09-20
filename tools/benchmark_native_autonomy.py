#!/usr/bin/env python3
"""Repeatable local latency/decision benchmark and opt-in real native TUI canary."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import platform
import pty
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from datetime import UTC, datetime
from pathlib import Path

from veyro.agents import AgentId
from veyro.autonomy import AutonomyOptions, prepare_autonomy
from veyro.autonomy_check import checkpoint

CHECKER = Path(__file__).resolve().parents[1] / "src/veyro/autonomy_check.py"
CASES = [
    (" Hello, World! ", "hello-world"),
    ("A___B---C", "a-b-c"),
    ("...", ""),
    ("", ""),
    ("abc123", "abc123"),
    ("a\nb\tc", "a-b-c"),
    ("CAFÉ", "caf"),
    ("a  -- !! b", "a-b"),
]


def baseline(samples: int) -> dict:
    latencies = []
    cases = []
    with tempfile.TemporaryDirectory(prefix="veyro-benchmark-") as temporary:
        repo = Path(temporary)
        for index in range(samples):
            expected = index % 2 == 0
            command = shlex.join(
                [sys.executable, "-c", f"raise SystemExit({0 if expected else 1})"]
            )
            launch = prepare_autonomy(
                AgentId.PRIME_AGENT, repo, "Synthetic fixture", (), AutonomyOptions((command,))
            )
            config = launch.directory / "config.json"
            checkpoint(config, "benchmark", "start", claim=True)
            (launch.directory / "plan.md").write_text("Run the labelled fixture check.")
            checkpoint(config, "benchmark", "plan")
            started = time.perf_counter()
            process = subprocess.run(
                [sys.executable, str(CHECKER), str(config), "benchmark", "build"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            latency = (time.perf_counter() - started) * 1000
            latencies.append(latency)
            decision = json.loads(process.stdout)
            cases.append(
                {
                    "expected_complete": expected,
                    "actual_complete": decision["action"] == "complete",
                    "latency_ms": latency,
                }
            )
    true_positives = sum(c["expected_complete"] and c["actual_complete"] for c in cases)
    false_positives = sum(not c["expected_complete"] and c["actual_complete"] for c in cases)
    false_negatives = sum(c["expected_complete"] and not c["actual_complete"] for c in cases)
    ordered = sorted(latencies)
    return {
        "kind": "synthetic_completion_decisions",
        "samples": samples,
        "measurement": "cold Python hook process + real Python verifier + state/journal IO",
        "p50_ms": ordered[math.ceil(samples * 0.5) - 1],
        "p95_ms": ordered[math.ceil(samples * 0.95) - 1],
        "max_ms": max(latencies),
        "subsecond_p95": ordered[math.ceil(samples * 0.95) - 1] < 1000,
        "accuracy": sum(c["expected_complete"] == c["actual_complete"] for c in cases) / samples,
        "precision": true_positives / (true_positives + false_positives),
        "recall": true_positives / (true_positives + false_negatives),
        "false_completions": false_positives,
        "cases": cases,
        "limits": "Synthetic exit-code labels, not general coding-task or semantic-judge accuracy.",
    }


def stop_disposable_prime(workspace: Path) -> dict:
    """Closing the TUI does not stop its daemon-owned agent."""
    try:
        listed = subprocess.run(
            ["prime-agent", "list", "--json"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        owned = [
            session
            for session in json.loads(listed.stdout)["sessions"]
            if Path(session["cwd"]).resolve() == workspace.resolve()
        ]
        for session in owned:
            stopped = subprocess.run(
                ["prime-agent", "stop", session["sessionId"], "--json"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            if json.loads(stopped.stdout).get("success") is not True:
                raise ValueError("native stop was not acknowledged")
        return {"success": True, "stopped_sessions": len(owned)}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        return {"success": False, "error": str(error)}


def live(
    agent: str,
    model: str | None,
    timeout: float,
    judge: Path | None = None,
    local_profile: str | None = None,
    allow_uncalibrated: bool = False,
) -> dict:
    selected_profile = None
    if local_profile:
        from veyro.local_models import load_profiles

        selected_profile = load_profiles()[local_profile]
    workspace = Path(tempfile.mkdtemp(prefix=f"veyro-live-{agent}-"))
    workspace.chmod(0o700)
    (workspace / "slug.py").write_text("def slug(text):\n    return text\n")
    verifier = workspace / "verify.py"
    verifier.write_text(
        "from slug import slug\n"
        + "\n".join(f"assert slug({text!r}) == {expected!r}" for text, expected in CASES[:3])
        + "\n"
    )
    verifier_original = verifier.read_bytes()
    command = [
        sys.executable,
        "-m",
        "veyro",
        "agent",
        agent,
        "--repo",
        str(workspace),
        "--autonomous",
        "--check",
        shlex.join([sys.executable, "verify.py"]),
        "--timeout",
        str(timeout),
        "--prompt",
        "Implement slug(text) in slug.py: lowercase ASCII letters and digits, replace "
        "each run of other characters with one hyphen, and trim hyphens. "
        "Do not modify verify.py. Do not delegate this small task.",
    ]
    if judge is not None:
        command += ["--evaluator", str(judge.resolve())]
    if local_profile:
        command += ["--coding-profile", local_profile]
        if judge is not None:
            command += ["--evaluation-profile", local_profile]
        if allow_uncalibrated:
            command += ["--allow-uncalibrated-evaluator"]
    if model:
        command += ["--", "--model", model]
    version_probe = subprocess.run(
        [agent, "--version"], capture_output=True, text=True, check=True, timeout=15
    )
    version = (version_probe.stdout or version_probe.stderr).strip()
    started = time.monotonic()
    pid, terminal = pty.fork()
    if pid == 0:
        environment = os.environ.copy()
        environment["TERM"] = "xterm-256color"
        os.execvpe(sys.executable, command, environment)
    fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 140, 0, 0))
    state = {}
    exited = False
    run_directory = None
    try:
        with (workspace / "terminal.log").open("wb") as log:
            while time.monotonic() - started < timeout + 10:
                ready, _, _ = select.select([terminal], [], [], 0.1)
                if ready:
                    try:
                        output = os.read(terminal, 65536)
                    except OSError:
                        break
                    if not output:
                        break
                    log.write(output)
                    if b"\x1b[6n" in output:
                        os.write(terminal, b"\x1b[1;1R")
                    if b"\x1b[c" in output:
                        os.write(terminal, b"\x1b[?1;2c")
                states = list((workspace / ".veyro/autonomy").glob("*/state.json"))
                if states:
                    run_directory = states[0].parent
                    state = json.loads(states[0].read_text())
                    if state["phase"] in ("completed", "blocked"):
                        break
                    if (run_directory / "adapter-error.json").exists():
                        break
                waited, _ = os.waitpid(pid, os.WNOHANG)
                if waited:
                    exited = True
                    break
    finally:
        elapsed = time.monotonic() - started
        if not exited:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        os.close(terminal)
        if not exited:
            # The launcher forwards termination to its native process group.
            # waitpid is bounded by the launcher's deadline escalation.
            os.waitpid(pid, 0)
        cleanup = stop_disposable_prime(workspace) if agent == "prime-agent" else {"success": True}
    heldout = subprocess.run(
        [
            sys.executable,
            "-c",
            "from slug import slug; import json; "
            + f"print(json.dumps([slug(t) == e for t, e in {CASES!r}]))",
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=5,
    )
    try:
        correct = sum(json.loads(heldout.stdout))
    except (ValueError, TypeError):
        correct = 0
    events = []
    adapter_error = None
    if run_directory and (run_directory / "adapter-error.json").exists():
        adapter_error = json.loads((run_directory / "adapter-error.json").read_text())
    if run_directory and (run_directory / "events.jsonl").exists():
        events = [
            json.loads(line) for line in (run_directory / "events.jsonl").read_text().splitlines()
        ]
    return {
        "kind": "live_native_tui",
        "native_cleanup": cleanup,
        "agent": agent,
        "native_executable": shutil.which(agent),
        "version": version,
        "model": selected_profile.ollama_model if selected_profile else model,
        "model_manifest_digest": selected_profile.expected_digest if selected_profile else None,
        "model_requests": (
            [
                json.loads(line)
                for line in (run_directory / "model-requests.jsonl").read_text().splitlines()
            ]
            if run_directory and (run_directory / "model-requests.jsonl").exists()
            else []
        ),
        "local_profile": local_profile,
        "phase": state.get("phase", "not_started"),
        "adapter_error": adapter_error,
        "judge_enabled": judge is not None,
        "judge_result": state.get("decision", {}).get("judge"),
        "elapsed_seconds": elapsed,
        "assertions_passed": correct,
        "assertions_total": len(CASES),
        "accuracy": correct / len(CASES),
        "verifier_unchanged": verifier.read_bytes() == verifier_original,
        "transitions": [e["reason"] for e in events],
        "checkpoint_latency_ms": [e["latency_ms"] for e in events],
        "artifact_directory": str(workspace),
        "human_task_inputs": 0,
        "limits": (
            "One slug fixture, real provider and PTY; "
            "terminal closed only after outcome or timeout."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--live", choices=["prime-agent", "opencode"])
    parser.add_argument("--model")
    parser.add_argument("--judge", type=Path)
    parser.add_argument("--local-profile", choices=["small", "14b"])
    parser.add_argument("--allow-uncalibrated-judge", action="store_true")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("--samples must be at least 2")
    if args.local_profile and args.model:
        parser.error("--local-profile cannot be combined with --model")
    if args.judge is not None and not args.local_profile:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    result = {
        "schema_version": 1,
        "timestamp": datetime.now(UTC).isoformat(),
        "platform": platform.system(),
        "python": platform.python_version(),
        "result": live(
            args.live,
            args.model,
            args.timeout,
            args.judge,
            args.local_profile,
            args.allow_uncalibrated_judge,
        )
        if args.live
        else baseline(args.samples),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    data = result["result"]
    passed = (
        (
            data.get("phase") == "completed"
            and data["accuracy"] == 1
            and data["verifier_unchanged"]
            and data["adapter_error"] is None
            and data["native_cleanup"]["success"]
            and (not data["judge_enabled"] or data["judge_result"]["status"] == "passed")
        )
        if args.live
        else (data["subsecond_p95"] and data["accuracy"] == 1)
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
