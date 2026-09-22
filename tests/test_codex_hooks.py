from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from veyro.bridges import (
    AgentBridge,
    validate_connection,
    validate_control_result,
    validate_event_batch,
)
from veyro.bridges import codex_hooks as codex
from veyro.models import (
    BridgeCapability,
    ControlOutcome,
    ControlRequest,
    QueueFollowUp,
    StopSession,
    SupervisionEventType,
)
from veyro.supervision.reducer import replay_session


def native_hook(repository, name="SessionEnd", session=None, **extra):
    return json.dumps(
        {
            "session_id": session or str(uuid4()),
            "cwd": str(repository),
            "hook_event_name": name,
            "transcript_path": "/SECRET/transcript",
            "reason": "other",
            **extra,
        }
    ).encode()


@pytest.fixture
async def listener(tmp_path, monkeypatch):
    async def verified(*args):
        pass

    monkeypatch.setattr(codex, "verify_codex", verified)
    instance = codex.CodexHookListener(tmp_path, tmp_path / "home", Path("/unused"))
    await instance.start()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.mark.parametrize("name", codex.HOOK_NAMES)
def test_sanitize_every_hook_drops_content(tmp_path, name):
    metadata = codex.sanitize_hook(
        native_hook(
            tmp_path,
            name,
            prompt="SECRET",
            tool_input={"password": "SECRET"},
            tool_response="SECRET",
            last_assistant_message="SECRET",
            model="SECRET",
            permission_mode="SECRET",
            agent_type="SECRET",
            tool_name="SECRET",
            turn_id="SECRET",
            tool_use_id="SECRET",
            agent_id="SECRET",
        )
    )
    assert "SECRET" not in metadata.model_dump_json()
    assert metadata.tool_kind == "other"
    assert metadata.turn_sha256 == metadata.agent_sha256


@pytest.mark.parametrize("raw", [b"[]", b"{}", b"not json", b"x" * (codex.MAX_HOOK_BYTES + 1)])
def test_bad_input_is_rejected(raw):
    with pytest.raises((ValueError, codex.CodexHookError)):
        codex.sanitize_hook(raw)


async def test_real_hook_process_to_private_broker(listener):
    import sys

    raw = native_hook(listener.repository, "UserPromptSubmit", prompt="SECRET")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "veyro.bridges.codex_hooks",
        "publish",
        "--connection",
        str(listener.connection_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, errors = await process.communicate(raw)
    assert process.returncode == 0
    assert output == errors == b""
    bridge = next(iter(listener.bridges.values()))
    assert isinstance(bridge, AgentBridge)
    validate_connection(bridge.identity, bridge.capabilities)
    events = bridge.store.replay_events()
    validate_event_batch(bridge.identity, events)
    assert events[0].event_type is SupervisionEventType.USER_PROMPT_SUBMITTED
    assert "SECRET" not in bridge.store.events_path.read_text()
    assert bridge.store.events_path.stat().st_mode & 0o077 == 0


async def test_boundaries_do_not_claim_execution_or_completion(listener):
    session = str(uuid4())
    for name in ("SessionStart", "PreToolUse", "PostToolUse", "Stop", "SessionEnd", "SessionStart"):
        listener._ingest(codex.sanitize_hook(native_hook(listener.repository, name, session)))
    bridge = listener.bridges[session]
    events = bridge.store.replay_events()
    assert all(e.event_type is SupervisionEventType.UNKNOWN for e in events)
    state = replay_session(bridge.identity, events)
    assert state.completed_tools == state.completion_claims == 0
    assert state.unknown_events == 6
    assert not bridge.capabilities.supports(BridgeCapability.REPLAY_EVENTS)


async def test_child_prompt_is_not_root_prompt(listener):
    event = listener._ingest(
        codex.sanitize_hook(
            native_hook(
                listener.repository,
                "UserPromptSubmit",
                agent_id="child-1",
            )
        )
    )
    assert event.event_type is SupervisionEventType.UNKNOWN
    assert event.payload["agent_sha256"]


async def test_multiple_sessions_are_separate_and_mapping_survives_restart(listener):
    raw = native_hook(listener.repository)
    event = listener._ingest(codex.sanitize_hook(raw))
    second = listener._ingest(codex.sanitize_hook(native_hook(listener.repository)))
    assert event.session != second.session
    await listener.close()
    replacement = codex.CodexHookListener(
        listener.repository, listener.codex_home, listener.executable
    )
    await replacement.start()
    try:
        resumed = replacement._ingest(codex.sanitize_hook(raw))
        assert resumed.session == event.session
        assert resumed.sequence == 2
    finally:
        await replacement.close()


async def test_wrong_repository_and_unknown_fields_rejected(listener):
    with pytest.raises(codex.CodexHookError, match="repository"):
        listener._ingest(codex.sanitize_hook(native_hook(listener.repository / "other")))
    metadata = codex.sanitize_hook(native_hook(listener.repository)).model_dump(mode="json")
    with pytest.raises(ValueError):
        codex.HookMetadata.model_validate({**metadata, "prompt": "SECRET"})


async def test_socket_auth_and_bounds(listener):
    config = json.loads(listener.connection_path.read_text())
    for message in [
        {
            "token": "wrong",
            "metadata": codex.sanitize_hook(native_hook(listener.repository)).model_dump(
                mode="json"
            ),
        },
        {"token": config["token"], "metadata": {"prompt": "SECRET"}},
    ]:
        reader, writer = await asyncio.open_unix_connection(config["socket"])
        writer.write(json.dumps(message).encode() + b"\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), timeout=2) == b""
        writer.close()
        await writer.wait_closed()
    assert not listener.bridges
    reader, writer = await asyncio.open_unix_connection(config["socket"])
    writer.write(b"x" * (codex.MAX_FRAME_BYTES + 2) + b"\n")
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), timeout=2) == b""
    writer.close()
    await writer.wait_closed()


async def test_publish_refuses_exposed_connection_and_symlink(listener):
    connection = listener.connection_path
    raw = native_hook(listener.repository)
    connection.chmod(0o644)
    with pytest.raises(codex.CodexHookError, match="private"):
        codex.publish_hook(connection, raw)
    connection.chmod(0o600)
    link = connection.parent / "link"
    link.symlink_to(connection)
    with pytest.raises(codex.CodexHookError, match="private"):
        codex.publish_hook(link, raw)


async def test_hook_outage_emits_no_provider_feedback(tmp_path):
    import sys

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "veyro.bridges.codex_hooks",
        "publish",
        "--connection",
        str(tmp_path / "missing"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, errors = await process.communicate(native_hook(tmp_path))
    assert process.returncode == 0 and output == errors == b""


async def test_unsupported_controls_are_honest(listener):
    event = listener._ingest(codex.sanitize_hook(native_hook(listener.repository)))
    bridge = listener.bridges[event.session.provider_session_id]
    for intent in (QueueFollowUp(message="hello"), StopSession(reason="stop")):
        request = ControlRequest(session=bridge.identity, command_id=str(uuid4()), intent=intent)
        answer = await bridge.execute(request)
        validate_control_result(request, answer, bridge.capabilities)
        assert answer.outcome is ControlOutcome.UNSUPPORTED



async def test_unknown_native_version_fails_closed(tmp_path, monkeypatch):
    async def run(*args, **kwargs):
        return 0, b"codex-cli 0.155.0"

    monkeypatch.setattr(codex, "_run_native", run)
    with pytest.raises(codex.CodexHookError, match="version"):
        await codex.verify_codex(Path("/unused"), tmp_path, tmp_path)


async def test_close_wakes_event_reader_and_cleans_socket(listener):
    event = listener._ingest(codex.sanitize_hook(native_hook(listener.repository)))
    bridge = listener.bridges[event.session.provider_session_id]
    stream = bridge.events()
    assert await anext(stream) == event
    directory = listener.connection_path.parent
    await listener.close()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert not directory.exists()


async def test_two_listeners_cannot_write_one_session(listener):
    raw = native_hook(listener.repository)
    listener._ingest(codex.sanitize_hook(raw))
    other = codex.CodexHookListener(listener.repository, listener.codex_home, listener.executable)
    await other.start()
    try:
        with pytest.raises(BlockingIOError):
            other._ingest(codex.sanitize_hook(raw))
    finally:
        await other.close()




async def test_generated_hook_ignores_project_python_and_environment(listener, tmp_path):
    import os
    import shlex

    package = tmp_path / "veyro"
    package.mkdir()
    (package / "__init__.py").write_text("print('PROJECT_PACKAGE_EXECUTED')")
    command = codex.hook_configuration(listener.connection_path)
    argv = shlex.split(command["hooks"]["SessionEnd"][0]["hooks"][0]["command"])
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, errors = await process.communicate(native_hook(listener.repository))
    assert process.returncode == 0 and output == errors == b""
    assert len(listener.bridges) == 1


async def test_restart_preserves_observation_history_and_observation_only_capabilities(listener):
    raw = native_hook(listener.repository)
    first = listener._ingest(codex.sanitize_hook(raw))
    await listener.close()
    for sequence in (2, 3):
        other = codex.CodexHookListener(
            listener.repository,
            listener.codex_home,
            listener.executable,
        )
        await other.start()
        try:
            event = other._ingest(codex.sanitize_hook(raw))
            assert event.session == first.session and event.sequence == sequence
            bridge = other.bridges[event.session.provider_session_id]
            assert not bridge.capabilities.supports(BridgeCapability.QUEUE_FOLLOW_UP)
            assert not bridge.store.capabilities.supports(BridgeCapability.QUEUE_FOLLOW_UP)
        finally:
            await other.close()
