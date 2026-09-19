from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pty
import re
import selectors
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path
from uuid import UUID

NATIVE_EXECUTABLE = Path(
    "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/"
    "@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex"
)
NATIVE_SHA256 = "4f85982624b3898c8991cb80c0981b2aa71070e3537046c9a95950318a95afcc"
VERSION = "0.154.0"
MAX_INPUT = 16_384
SANDBOX = Path("/usr/bin/sandbox-exec")


class CanaryBlocked(RuntimeError):
    """A fixed, non-sensitive reason that the live proof could not be established."""


def _metadata(raw: bytes) -> dict[str, str]:
    if len(raw) > MAX_INPUT:
        raise ValueError("hook input exceeds limit")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("hook input must be an object")
    if payload.get("hook_event_name") != "SessionEnd" or payload.get("reason") != "other":
        raise ValueError("unexpected hook event")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or len(session_id) != 36:
        raise ValueError("invalid session identity")
    return {
        "hook_event_name": "SessionEnd",
        "session_id": str(UUID(session_id)),
        "reason": "other",
    }


def _capture_hook(destination: Path) -> None:
    # This module is copied into the private tree and run with Python -I -S.
    metadata = _metadata(sys.stdin.buffer.read(MAX_INPUT + 1))
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(metadata, output, sort_keys=True)


def _environment(directory: Path) -> dict[str, str]:
    return {
        "HOME": str(directory / "home"),
        "CODEX_HOME": str(directory / "codex"),
        "XDG_CONFIG_HOME": str(directory / "home" / ".config"),
        "XDG_CACHE_HOME": str(directory / "home" / ".cache"),
        "TMPDIR": str(directory / "tmp"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "SHELL": "/bin/sh",
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
    }


def _prepare(directory: Path) -> tuple[dict[str, str], Path]:
    for name in ("home", "codex", "repo", "tmp"):
        (directory / name).mkdir(mode=0o700)
    env = _environment(directory)
    evidence = directory / "evidence.json"
    hook = directory / "hook.py"
    hook.write_text(Path(__file__).read_text())
    hook.chmod(0o600)
    # The project interpreter may live inside the real home, which the sandbox denies.
    command = shlex.join(
        [
            "/usr/bin/python3",
            "-I",
            "-S",
            str(hook),
            "--capture-hook",
            str(evidence),
        ]
    )
    config = directory / "codex" / "config.toml"
    config.write_text(
        'model = "foreman-no-model"\n'
        'model_provider = "foreman-canary"\n'
        "check_for_update_on_startup = false\n"
        'cli_auth_credentials_store = "file"\n'
        "[analytics]\nenabled = false\n"
        "[feedback]\nenabled = false\n"
        "[model_providers.foreman-canary]\n"
        'name = "Offline canary"\n'
        'base_url = "http://127.0.0.1:1/v1"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = false\n"
        "supports_websockets = false\n"
    )
    config.chmod(0o600)
    hooks = directory / "codex" / "hooks.json"
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": command,
                                    "timeout": 1,
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
    hooks.chmod(0o600)
    return env, evidence


def _sandbox_profile(real_home: Path) -> str:
    # Fail closed instead of falling back to an unsandboxed native process.
    policies = [
        Path("/etc/codex"),
        Path("/private/etc/codex"),
        Path("/Library/Managed Preferences/com.openai.codex.plist"),
    ]
    paths = " ".join(f"(subpath {json.dumps(str(path))})" for path in policies)
    # Honor installed managed requirements; only writes to policy paths are denied.
    return (
        "(version 1)(allow default)(deny network*)"
        f"(deny file-read* file-write* (subpath {json.dumps(str(real_home))}))"
        f"(deny file-write* {paths})"
    )


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    # Reap the leader before checking for surviving descendants (including on macOS).
    process.poll()
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can reject a group signal while the leader is exiting; reap before retry.
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        raise CanaryBlocked("cannot kill canary process group") from None
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired as error:
        raise CanaryBlocked("process cleanup timed out") from error
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return
    except PermissionError:
        raise CanaryBlocked("cannot verify canary process group cleanup") from None
    raise CanaryBlocked("canary process group remained after cleanup")


_ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _drive_lifecycle(process: subprocess.Popen[bytes], master: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    screen = b""
    directory_approved = False
    hooks_approved = False
    hooks_opened = False
    quit_sent = False
    blocked_reason = None
    pending_action: tuple[str, bytes, float] | None = None
    submit_at: float | None = None
    with selectors.DefaultSelector() as selector:
        selector.register(master, selectors.EVENT_READ)
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stage = (
                    "after hook approval input"
                    if hooks_approved
                    else "after directory approval input"
                    if directory_approved
                    else "before directory approval"
                )
                raise CanaryBlocked(f"native lifecycle timed out {stage}")
            if submit_at is not None and time.monotonic() >= submit_at:
                os.write(master, b"\r")
                submit_at = None
            chunk = b""
            if selector.select(min(remaining, 0.05)):
                try:
                    chunk = os.read(master, 16_384)
                except OSError:
                    break
                if not chunk:
                    break
            screen = (screen + chunk)[-65_536:]
            # Complete native terminal negotiation before sending lifecycle keys.
            if b"\x1b[6n" in chunk:
                os.write(master, b"\x1b[1;1R")
            if b"\x1b[c" in chunk or b"\x1b[0c" in chunk:
                os.write(master, b"\x1b[?1;2c")
            if b"\x1b]11;?" in chunk:
                os.write(master, b"\x1b]11;rgb:0000/0000/0000\x1b\\")
            visible = re.sub(rb"\s+", b"", _ANSI.sub(b"", screen))
            if (
                b"/etc/codex" in visible
                and b"Operationnotpermitted" in visible
                and b"config" in visible
            ):
                blocked_reason = "native startup requires denied installed configuration"
            action = None
            if (
                not directory_approved
                and b"Doyoutrustthecontents" in visible
                and b"Yes,continue" in visible
                and b"Pressenter" in visible
            ):
                action = ("directory", b"\r")
            elif (
                not hooks_approved
                and b"Hooksneedreview" in visible
                and b"1hookisneworchanged." in visible
                and b"Trustallandcontinue" in visible
                and b"Pressenter" in visible
            ):
                action = ("trust-hooks", b"2\r")
            elif (
                directory_approved
                and not hooks_approved
                and not hooks_opened
                and b"?forshortcuts" in visible
            ):
                action = ("open-hooks", b"\x15/hooks\r")
            elif hooks_approved and not quit_sent and b"?forshortcuts" in visible:
                action = ("quit", b"\x15/quit\r")
            if action is None:
                pending_action = None
                continue
            if pending_action is None or pending_action[:2] != action:
                # Codex draws protected screens, then drains input until 10 ms of quiet.
                pending_action = (*action, time.monotonic() + 0.1)
                continue
            if time.monotonic() < pending_action[2]:
                continue
            if action[0] in {"open-hooks", "quit"}:
                # Let the native paste-burst buffer settle before submitting a local command.
                os.write(master, action[1][:-1])
                submit_at = time.monotonic() + 0.1
            else:
                os.write(master, action[1])
            directory_approved |= action[0] == "directory"
            hooks_approved |= action[0] == "trust-hooks"
            hooks_opened |= action[0] == "open-hooks"
            quit_sent |= action[0] == "quit"
            pending_action = None
            screen = b""
        try:
            exit_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise CanaryBlocked("native lifecycle did not exit") from error
    if blocked_reason is not None:
        raise CanaryBlocked(blocked_reason)
    if not hooks_approved or not quit_sent or exit_code != 0:
        raise CanaryBlocked("native trust and clean no-prompt quit were not completed")


def run_canary(*, executable: Path = NATIVE_EXECUTABLE, timeout: float = 30) -> dict[str, object]:
    if not 1 <= timeout <= 60:
        raise ValueError("timeout must be between 1 and 60 seconds")
    if sys.platform != "darwin" or not SANDBOX.is_file():
        raise CanaryBlocked("macOS network-deny sandbox is required")
    try:
        with executable.open("rb") as binary:
            digest = hashlib.file_digest(binary, "sha256").hexdigest()
    except OSError as error:
        raise CanaryBlocked("pinned native executable is unavailable") from error
    if digest != NATIVE_SHA256:
        raise CanaryBlocked("native executable does not match pinned Codex 0.154.0")
    real_home = Path.home().resolve()
    with tempfile.TemporaryDirectory(prefix="foreman-sup012-", dir="/tmp") as temporary:
        directory = Path(temporary).resolve()
        env, evidence = _prepare(directory)
        profile = _sandbox_profile(real_home)
        process: subprocess.Popen[bytes] | None = None
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
            process = subprocess.Popen(
                [
                    str(SANDBOX),
                    "-p",
                    profile,
                    str(executable.resolve()),
                    "--no-alt-screen",
                    "--enable",
                    "hooks",
                    "-c",
                    'model_provider="foreman-canary"',
                ],
                cwd=directory / "repo",
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
            os.close(slave)
            slave = -1
            _drive_lifecycle(process, master, timeout)
            if not evidence.is_file():
                raise CanaryBlocked("SessionEnd hook did not deliver stdin evidence")
            with evidence.open("rb") as source:
                metadata = _metadata(source.read(MAX_INPUT + 1))
            summary = {
                "provider": "codex",
                "version": VERSION,
                "native_sha256": digest,
                "event": metadata["hook_event_name"],
                "session_id_valid": True,
                "reason": metadata["reason"],
                "exit_code": process.returncode,
                "hook_trust": "native-private-approval",
                "prompt_sent": False,
                "network": "sandbox-deny",
                "temporary_tree_removed": True,
            }
        finally:
            os.close(master)
            if slave >= 0:
                os.close(slave)
            if process is not None:
                _stop_group(process)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the isolated no-prompt Codex hook canary")
    parser.add_argument("--executable", type=Path, default=NATIVE_EXECUTABLE)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--capture-hook", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.capture_hook is not None:
        try:
            _capture_hook(args.capture_hook)
        except (ValueError, OSError):
            raise SystemExit(1) from None
        return
    try:
        result = run_canary(executable=args.executable, timeout=args.timeout)
    except CanaryBlocked as error:
        print(json.dumps({"status": "blocked", "reason": str(error), "prompt_sent": False}))
        raise SystemExit(1) from None
    print(json.dumps({"status": "passed", **result}, sort_keys=True))


if __name__ == "__main__":
    main()
