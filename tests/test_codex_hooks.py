from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from foreman.bridges import (
    AgentBridge,
    validate_connection,
    validate_control_result,
    validate_event_batch,
)
from foreman.bridges import codex_hooks as codex
from foreman.models import (
    BridgeCapability,
    ControlOutcome,
    ControlRequest,
    QueueFollowUp,
    StopSession,
    SupervisionEventType,
)
from foreman.supervision.reducer import replay_session


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
        "foreman.bridges.codex_hooks",
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
        "foreman.bridges.codex_hooks",
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


async def queue_bridge(listener):
    listener.capabilities = codex.codex_hook_capabilities(allow_queue=True)
    event = listener._ingest(codex.sanitize_hook(native_hook(listener.repository)))
    return listener.bridges[event.session.provider_session_id]


async def test_queue_ack_is_exact_and_durable_at_most_once(listener, monkeypatch):
    bridge = await queue_bridge(listener)
    calls = []
    queue_id = str(uuid4())

    async def run(executable, args, **kwargs):
        calls.append(args)
        return (
            0,
            (
                f"Queued message {queue_id} for thread {bridge.identity.provider_session_id}.\n"
            ).encode(),
        )

    monkeypatch.setattr(codex, "_run_native", run)
    request = ControlRequest(
        session=bridge.identity, command_id="queue-1", intent=QueueFollowUp(message="SECRET")
    )
    result = await bridge.execute(request)
    assert result.outcome is ControlOutcome.EXECUTED
    validate_control_result(request, result, bridge.capabilities)
    assert await bridge.execute(request) == result
    assert len(calls) == 1
    assert calls[0][1:3] == ["--thread", bridge.identity.provider_session_id]
    for file in bridge.store.directory.glob("queue-*"):
        assert "SECRET" not in file.read_text()
    changed = request.model_copy(update={"intent": QueueFollowUp(message="different")})
    assert (await bridge.execute(changed)).outcome is ControlOutcome.REJECTED
    restarted = codex.CodexHookBridge(bridge.store, bridge.executable, bridge.codex_home)
    assert await restarted.execute(request) == result
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["timeout", "bad-ack", "wrong-session", "exit"])
async def test_uncertain_queue_is_not_retried(listener, monkeypatch, failure):
    bridge = await queue_bridge(listener)
    calls = []

    async def run(*args, **kwargs):
        calls.append(True)
        if failure == "timeout":
            raise TimeoutError
        if failure == "wrong-session":
            return 0, f"Queued message {uuid4()} for thread {uuid4()}.\n".encode()
        return (1 if failure == "exit" else 0), b"SECRET"

    monkeypatch.setattr(codex, "_run_native", run)
    request = ControlRequest(
        session=bridge.identity, command_id="queue", intent=QueueFollowUp(message="SECRET")
    )
    result = await bridge.execute(request)
    assert result.outcome is ControlOutcome.FAILED
    assert "uncertain" in result.detail
    assert await bridge.execute(request) == result
    assert len(calls) == 1
    assert "SECRET" not in result.model_dump_json()


async def test_pending_queue_claim_is_not_retried(listener, monkeypatch):
    bridge = await queue_bridge(listener)

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(codex, "_run_native", cancelled)
    request = ControlRequest(
        session=bridge.identity, command_id="queue", intent=QueueFollowUp(message="hello")
    )
    with pytest.raises(asyncio.CancelledError):
        await bridge.execute(request)
    answer = await bridge.execute(request)
    assert answer.outcome is ControlOutcome.FAILED
    assert "uncertain" in answer.detail


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


async def test_approval_gate_prevents_unapproved_queue(listener, monkeypatch):
    from foreman.models import AuthorizationOutcome, BoundaryAction, BoundaryOperation
    from foreman.models.rollout import RolloutPolicy
    from foreman.supervision.authorization import (
        AuthorizedControlDispatcher,
        ControlAuthorizationGate,
        control_request_sha256,
    )
    from foreman.supervision.boundary_policy import BoundaryPolicy

    bridge = await queue_bridge(listener)
    request = ControlRequest(
        session=bridge.identity,
        command_id="approval",
        intent=QueueFollowUp(message="Run tests"),
    )
    boundary = BoundaryPolicy().classify(
        BoundaryAction(
            session=bridge.identity,
            action_id=request.command_id,
            operation=BoundaryOperation.READ_REPOSITORY,
            target_sha256=control_request_sha256(request),
        )
    )
    state = replay_session(bridge.identity, bridge.store.replay_events())

    async def forbidden(*args, **kwargs):
        pytest.fail("unapproved native command executed")

    monkeypatch.setattr(codex, "_run_native", forbidden)
    result = await AuthorizedControlDispatcher(
        bridge,
        ControlAuthorizationGate(RolloutPolicy(mode="approval_required")),
    ).dispatch(request, state=state, boundary=boundary)
    assert result.authorization.outcome is AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED
    assert result.result is None
    assert not list(bridge.store.directory.glob("queue-*"))


async def test_generated_hook_ignores_project_python_and_environment(listener, tmp_path):
    import os
    import shlex

    package = tmp_path / "foreman"
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


async def test_queue_opt_in_and_opt_out_preserve_observation_history(listener):
    raw = native_hook(listener.repository)
    first = listener._ingest(codex.sanitize_hook(raw))
    await listener.close()
    for sequence, enabled in [(2, True), (3, False)]:
        other = codex.CodexHookListener(
            listener.repository,
            listener.codex_home,
            listener.executable,
            allow_queue=enabled,
        )
        await other.start()
        try:
            event = other._ingest(codex.sanitize_hook(raw))
            assert event.session == first.session and event.sequence == sequence
            bridge = other.bridges[event.session.provider_session_id]
            assert bridge.capabilities.supports(BridgeCapability.QUEUE_FOLLOW_UP) is enabled
            assert bridge.store.capabilities.supports(BridgeCapability.QUEUE_FOLLOW_UP) is enabled
        finally:
            await other.close()


@pytest.mark.parametrize("message", ["--help", "- Fix this", "Normal message"])
async def test_queue_message_is_one_option_value(listener, monkeypatch, message):
    bridge = await queue_bridge(listener)
    calls = []

    async def run(executable, args, **kwargs):
        calls.append(args)
        return 0, (
            f"Queued message {uuid4()} for thread {bridge.identity.provider_session_id}.\n"
        ).encode()

    monkeypatch.setattr(codex, "_run_native", run)
    request = ControlRequest(
        session=bridge.identity,
        command_id="hyphen",
        intent=QueueFollowUp(message=message),
    )
    assert (await bridge.execute(request)).outcome is ControlOutcome.EXECUTED
    assert calls[0][-1] == f"--message={message}"
