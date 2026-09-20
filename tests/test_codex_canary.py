from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from veyro.supervision import codex_canary as canary

SESSION_ID = "019b3065-932e-72b4-96d7-b0ddbe7dd2da"


def payload(**changes: object) -> bytes:
    return json.dumps(
        {
            "hook_event_name": "SessionEnd",
            "session_id": SESSION_ID,
            "reason": "other",
            **changes,
        }
    ).encode()


def test_hook_reads_bounded_stdin_and_persists_metadata_only(tmp_path: Path) -> None:
    env, evidence = canary._prepare(tmp_path)
    hook = json.loads((tmp_path / "codex/hooks.json").read_text())
    command = hook["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
    result = subprocess.run(
        shlex.split(command),
        input=payload(
            prompt="secret",
            transcript_path="/must/not/read",
            cwd="private",
            tool_input="secret",
        ),
        env=env,
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""
    assert json.loads(evidence.read_text()) == {
        "hook_event_name": "SessionEnd",
        "session_id": SESSION_ID,
        "reason": "other",
    }
    assert evidence.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "codex").stat().st_mode & 0o777 == 0o700
    assert "trusted_hash" not in (tmp_path / "codex/config.toml").read_text()
    assert "trust_level" not in (tmp_path / "codex/config.toml").read_text()


@pytest.mark.parametrize(
    "raw",
    [
        b"x" * (canary.MAX_INPUT + 1),
        b"[]",
        b"null",
        b"{invalid",
        b"\xff",
        payload(hook_event_name="Stop"),
        payload(reason="quit"),
        payload(session_id="not-a-uuid"),
        payload(session_id=None),
        payload(session_id=23),
    ],
)
def test_hook_rejects_invalid_or_oversized_input(raw: bytes) -> None:
    with pytest.raises((ValueError, UnicodeError)):
        canary._metadata(raw)


def test_environment_does_not_inherit_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://private.invalid")
    monkeypatch.setenv("CODEX_HOME", "/real/config")
    env = canary._environment(Path("/tmp/private"))
    assert "OPENAI_API_KEY" not in env
    assert "HTTPS_PROXY" not in env
    assert env["HOME"] == "/tmp/private/home"
    assert env["CODEX_HOME"] == "/tmp/private/codex"
    assert env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"


def test_sandbox_denies_network_and_policy_writes_but_honors_requirements() -> None:
    profile = canary._sandbox_profile(Path("/Users/example"))
    assert "(deny network*)" in profile
    assert '(subpath "/Users/example")' in profile
    assert '(subpath "/etc/codex")' in profile
    assert '(subpath "/private/etc/codex")' in profile
    assert "com.openai.codex.plist" in profile
    assert '(deny file-write* (subpath "/etc/codex")' in profile
    assert '(deny file-read* file-write* (subpath "/Users/example"))' in profile


def test_hash_mismatch_never_launches_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "not-codex"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(canary.sys, "platform", "darwin")
    monkeypatch.setattr(canary, "SANDBOX", executable)
    launch = Mock()
    monkeypatch.setattr(canary.subprocess, "Popen", launch)
    with pytest.raises(canary.CanaryBlocked, match="does not match pinned"):
        canary.run_canary(executable=executable)
    launch.assert_not_called()


def test_missing_sandbox_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(canary, "SANDBOX", tmp_path / "absent")
    with pytest.raises(canary.CanaryBlocked, match="network-deny sandbox"):
        canary.run_canary()


def test_timeout_kills_group_and_removes_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    executable.write_bytes(b"test native binary")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    monkeypatch.setattr(canary, "NATIVE_SHA256", digest)
    monkeypatch.setattr(canary, "SANDBOX", executable)
    monkeypatch.setattr(canary.sys, "platform", "darwin")
    captured: dict[str, object] = {}
    original_launch = subprocess.Popen

    def launch(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        captured["args"] = args
        captured["directory"] = Path(kwargs["cwd"]).parent
        captured["env"] = kwargs["env"]
        process = original_launch(
            [sys.executable, "-I", "-S", "-c", "import signal; signal.pause()"],
            **kwargs,
        )
        captured["process"] = process
        return process

    monkeypatch.setattr(canary.subprocess, "Popen", launch)
    with pytest.raises(canary.CanaryBlocked, match="timed out"):
        canary.run_canary(executable=executable, timeout=1)
    process = captured["process"]
    assert process.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)
    assert not captured["directory"].exists()
    assert not any("bypass" in arg for arg in captured["args"])
    assert "OPENAI_API_KEY" not in captured["env"]


def test_cleanup_checks_group_after_leader_already_exited(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock(pid=999999, returncode=0)
    kill = Mock(side_effect=[None, None, ProcessLookupError])
    monkeypatch.setattr(canary.os, "killpg", kill)
    canary._stop_group(process)
    assert [call.args for call in kill.call_args_list] == [
        (999999, signal.SIGTERM),
        (999999, signal.SIGKILL),
        (999999, 0),
    ]
    assert [call.kwargs for call in process.wait.call_args_list] == [{"timeout": 1}, {"timeout": 2}]


def test_cleanup_rejects_surviving_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(canary.os, "killpg", Mock())
    with pytest.raises(canary.CanaryBlocked, match="remained"):
        canary._stop_group(Mock(pid=999999))


def test_cleanup_retries_after_macos_exit_race(monkeypatch: pytest.MonkeyPatch) -> None:
    kill = Mock(side_effect=[PermissionError, ProcessLookupError, ProcessLookupError])
    monkeypatch.setattr(canary.os, "killpg", kill)
    process = Mock(pid=999999)
    canary._stop_group(process)
    assert process.wait.call_count == 2


@pytest.mark.parametrize("deliver", [True, False])
def test_synthetic_pty_lifecycle_requires_hook_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deliver: bool,
) -> None:
    executable = tmp_path / "fixture-binary"
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(canary, "NATIVE_SHA256", hashlib.sha256(b"fixture").hexdigest())
    monkeypatch.setattr(canary, "SANDBOX", executable)
    monkeypatch.setattr(canary.sys, "platform", "darwin")
    original_launch = subprocess.Popen
    captured: list[Path] = []
    script = f"""
import json, os, pathlib, select, shlex, subprocess, sys, tty
tty.setraw(0)
def expect(message, expected):
    os.write(1, message)
    if b'Press enter' in message:
        # Native protected screens discard typeahead after drawing, before accepting keys.
        while select.select([0], [], [], 0.01)[0]:
            os.read(0, 4096)
    received = b''
    while len(received) < len(expected):
        received += os.read(0, len(expected) - len(received))
    assert received == expected
expect(b'\x1b[c', b'\x1b[?1;2c')
expect(b'Do you trust the contents Yes, continue Press enter', b'\\r')
expect(b'Hooks need review 1 hook is new or changed. Trust all and continue Press enter', b'2\\r')
expect(b'? for shortcuts', b'\\x15/quit')
assert not select.select([0], [], [], 0.02)[0], 'command and Enter were coalesced'
expect(b'', b'\\r')
if {deliver!r}:
    hooks = json.loads((pathlib.Path(os.environ['CODEX_HOME']) / 'hooks.json').read_text())
    command = hooks['hooks']['SessionEnd'][0]['hooks'][0]['command']
    result = subprocess.run(shlex.split(command), input={payload()!r}, capture_output=True)
    assert result.returncode == 0 and not result.stdout and not result.stderr
"""

    def launch(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        captured.append(Path(kwargs["cwd"]).parent)
        assert "--dangerously-bypass-hook-trust" not in args
        return original_launch([sys.executable, "-I", "-S", "-c", script], **kwargs)

    monkeypatch.setattr(canary.subprocess, "Popen", launch)
    if deliver:
        summary = canary.run_canary(executable=executable, timeout=3)
        assert summary["event"] == "SessionEnd"
        assert summary["session_id_valid"] is True
        assert summary["prompt_sent"] is False
        assert summary["exit_code"] == 0
        assert "session_id" not in summary
    else:
        with pytest.raises(canary.CanaryBlocked, match="did not deliver stdin evidence"):
            canary.run_canary(executable=executable, timeout=3)
    assert captured and not captured[0].exists()
