from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from veyro.agents import AgentId
from veyro.bridges.codex_hooks import (
    BRIDGE_ID,
    BRIDGE_VERSION,
    CODEX_VERSION,
    CodexHookBridge,
    CodexHookError,
    codex_hook_capabilities,
    sanitize_hook,
)
from veyro.bridges.opencode import OPENCODE_VERSION, OpenCodeClient, OpenCodeError
from veyro.bridges.prime_agent import (
    PrimeAgentDaemonBridge,
    PrimeDaemonClient,
    PrimeDaemonError,
)
from veyro.cli import app
from veyro.models import BridgeCapability, SessionIdentity
from veyro.supervision.attachment import AttachmentError, attach_existing, discover_sessions
from veyro.supervision.broker import BrokerStore


class PrimeListClient(PrimeDaemonClient):
    def __init__(self, rows):
        super().__init__(Path("/unused"))
        self.rows = rows
        self.commands = []

    async def request(self, command):
        self.commands.append(command)
        return {"data": {"sessions": self.rows}}


class OpenCodeListClient(OpenCodeClient):
    def __init__(self, rows):
        super().__init__("http://127.0.0.1:1234", username="user", password="test-password")
        self.rows = rows
        self.requests = []
        self.version = OPENCODE_VERSION

    async def health(self):
        return {"healthy": True, "version": self.version}

    async def request_json(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        return self.rows


async def test_prime_discovery_filters_repository_and_content(tmp_path):
    repository = tmp_path.resolve()
    client = PrimeListClient(
        [
            {"activeSessionId": "active-1", "cwd": str(repository), "summary": "SECRET"},
            {"activeSessionId": "other", "cwd": "/other", "firstMessage": "SECRET"},
            {"activeSessionId": "../../SECRET", "cwd": str(repository)},
            {"activeSessionId": None, "cwd": str(repository)},
        ]
    )
    report = await client.discover(repository)
    assert [s.selector for s in report.sessions] == ["active-1"]
    assert report.sessions[0].native_liveness == "loaded"
    assert report.skipped == 2
    assert "SECRET" not in report.model_dump_json()
    assert client.commands == [{"type": "list", "cwd": str(repository), "includeClientOwned": True}]


async def test_opencode_discovery_filters_metadata_and_bounds(tmp_path):
    repository = tmp_path.resolve()

    def row(selector, **kwargs):
        return {
            "id": selector,
            "directory": str(repository),
            "version": OPENCODE_VERSION,
            "title": "SECRET",
            **kwargs,
        }

    client = OpenCodeListClient([row("ses_1"), row("ses_2"), row("ses_3")])
    report = await client.discover(repository, limit=2)
    assert report.truncated
    assert len(report.sessions) == 2
    assert report.sessions[0].native_liveness == "unknown"
    assert "SECRET" not in report.model_dump_json()
    assert client.requests == [("GET", "/session?limit=3", {"directory": repository})]
    client.rows = [row("ses_1", version="old"), row("ses_2", directory="/other"), row("../bad")]
    report = await client.discover(repository)
    assert not report.sessions and report.skipped == 2
    client.version = "old"
    with pytest.raises(OpenCodeError, match="version"):
        await client.discover(repository)


@pytest.mark.parametrize(
    "client_type,error", [(PrimeListClient, PrimeDaemonError), (OpenCodeListClient, OpenCodeError)]
)
async def test_discovery_rejects_malformed_lists_and_limits(tmp_path, client_type, error):
    client = client_type({"SECRET": "invalid"})
    with pytest.raises(error):
        await client.discover(tmp_path)
    for limit in [0, 1001]:
        with pytest.raises(error):
            await client.discover(tmp_path, limit=limit)


class AttachablePrime(PrimeListClient):
    def __init__(self, repository):
        super().__init__([])
        self.hello = {}
        self.repository = repository
        self.native_id = "active-1"
        self.closed = False

    async def request(self, command):
        self.commands.append(command)
        if command["type"] == "detach":
            return {}
        return {
            "data": {
                "activeSessionId": self.native_id,
                "lastEventSequence": 42,
                "snapshot": {
                    "summary": {
                        "cwd": str(self.repository),
                        "messageCount": 7,
                        "firstMessage": "SECRET",
                    },
                    "messages": [{"text": "SECRET"}],
                },
                "replay": {"status": "unavailable", "reason": "SECRET"},
            }
        }

    async def next_outbound(self):
        return await asyncio.Queue().get()

    async def close(self):
        self.closed = True


async def test_prime_attach_binds_repo_discards_content_and_only_detaches(tmp_path):
    client = AttachablePrime(tmp_path.resolve())
    bridge = await PrimeAgentDaemonBridge.attach_connected(
        client=client,
        active_session_id="active-1",
        veyro_session_id="attachment",
        repository=tmp_path,
    )
    assert bridge.attachment_history.snapshot_message_count == 7
    assert bridge.attachment_history.native_sequence == 42
    assert bridge.attachment_history.native_replay_status == "unavailable"
    assert not bridge.attachment_history.native_history_complete
    assert "SECRET" not in bridge.attachment_history.model_dump_json()
    assert "SECRET" not in bridge._events[0].model_dump_json()
    await bridge.close()
    assert [c["type"] for c in client.commands] == ["attach", "detach"]
    assert client.closed


@pytest.mark.parametrize("wrong", ["repository", "session", "relative"])
async def test_prime_attach_rejects_wrong_binding(tmp_path, wrong):
    client = AttachablePrime(tmp_path.resolve())
    if wrong == "repository":
        client.repository = tmp_path / "other"
    elif wrong == "relative":
        client.repository = Path(".")
    else:
        client.native_id = "wrong"
    with pytest.raises(PrimeDaemonError, match="mismatch|wrong session"):
        await PrimeAgentDaemonBridge.attach_connected(
            client=client,
            active_session_id="active-1",
            veyro_session_id="attachment",
            repository=tmp_path,
        )


@pytest.fixture
def hook_journal(tmp_path):
    repository = tmp_path.resolve()
    identity = SessionIdentity(
        veyro_session_id="codex-local",
        provider_id="codex",
        provider_session_id=str(uuid4()),
        repository=str(repository),
        provider_version=CODEX_VERSION,
        bridge_id=BRIDGE_ID,
        bridge_version=BRIDGE_VERSION,
    )
    store = BrokerStore.create(repository, identity, codex_hook_capabilities(allow_queue=True))
    bridge = CodexHookBridge(store, Path("/must-not-execute"), repository / "native-home")
    for name in ["SessionStart", "UserPromptSubmit", "SessionEnd"]:
        bridge.ingest(
            sanitize_hook(
                json.dumps(
                    {
                        "session_id": identity.provider_session_id,
                        "cwd": str(repository),
                        "hook_event_name": name,
                        "prompt": "SECRET",
                        "transcript_path": "SECRET",
                    }
                ).encode()
            )
        )
    return repository, store, bridge


def tree_snapshot(repository):
    return {
        str(p.relative_to(repository)): (
            p.stat().st_mode,
            p.stat().st_mtime_ns,
            p.read_bytes() if p.is_file() else None,
        )
        for p in repository.rglob("*")
    }


async def test_codex_discovery_and_attachment_are_readonly_and_not_native(hook_journal):
    repository, store, _ = hook_journal
    before = tree_snapshot(repository)
    discovery = await discover_sessions(AgentId.CODEX, repository)
    assert len(discovery.sessions) == 1
    candidate = discovery.sessions[0]
    assert candidate.selector == store.identity.veyro_session_id
    assert candidate.native_liveness == "unknown"
    attachment = await attach_existing(
        AgentId.CODEX, repository, candidate.selector, after_sequence=1
    )
    try:
        assert attachment.report.observation == "local_journal_only"
        assert not attachment.report.controls_enabled
        assert not hasattr(attachment, "execute")
        assert not attachment.report.provider_capabilities.supports(
            BridgeCapability.ATTACH_EXISTING
        )
        assert not attachment.report.provider_capabilities.supports(
            BridgeCapability.QUEUE_FOLLOW_UP
        )
        assert not attachment.report.history.native_history_complete
        events = [e async for e in attachment.events()]
        assert [e.sequence for e in events] == [2, 3]
        assert "SECRET" not in attachment.report.model_dump_json()
        assert all("SECRET" not in e.model_dump_json() for e in events)
    finally:
        await attachment.close()
    assert tree_snapshot(repository) == before
    with pytest.raises(CodexHookError, match="cursor"):
        await attach_existing(AgentId.CODEX, repository, candidate.selector, after_sequence=4)


async def test_codex_follow_sees_later_appends_and_withholds_partial_tail(hook_journal):
    repository, store, bridge = hook_journal
    attachment = await attach_existing(
        AgentId.CODEX, repository, "codex-local", after_sequence=3, follow_journal=True
    )
    stream = attachment.events()
    try:
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        expected = bridge.ingest(
            sanitize_hook(
                json.dumps(
                    {
                        "session_id": store.identity.provider_session_id,
                        "cwd": str(repository),
                        "hook_event_name": "Stop",
                    }
                ).encode()
            )
        )
        assert await asyncio.wait_for(pending, 1) == expected
    finally:
        await stream.aclose()
        await attachment.close()
    with store.events_path.open("ab") as output:
        output.write(b'{"incomplete":')
    attachment = await attach_existing(AgentId.CODEX, repository, "codex-local", after_sequence=4)
    try:
        assert [e async for e in attachment.events()] == []
        assert attachment.incomplete_tail
    finally:
        await attachment.close()


@pytest.mark.parametrize("change", ["payload", "event_type", "native_type", "event_id"])
async def test_codex_rejects_content_and_invented_semantics(hook_journal, change):
    repository, store, _ = hook_journal
    row = json.loads(store.events_path.read_text().splitlines()[0])
    if change == "payload":
        row["payload"]["prompt"] = "SECRET"
    elif change == "native_type":
        row["provenance"]["native_event_type"] = "SECRET"
    elif change == "event_id":
        row["event_id"] = "SECRET"
    else:
        row["event_type"] = "session_completed"
    store.events_path.write_text(json.dumps(row) + "\n")
    attachment = await attach_existing(AgentId.CODEX, repository, "codex-local")
    try:
        with pytest.raises((CodexHookError, ValueError)):
            _ = [e async for e in attachment.events()]
    finally:
        await attachment.close()


async def test_empty_discovery_does_not_create_storage_and_deferred_stays_unsupported(tmp_path):
    for provider in [AgentId.CODEX, AgentId.PI, AgentId.CLAUDE]:
        report = await discover_sessions(provider, tmp_path)
        assert not report.sessions
        if provider is not AgentId.CODEX:
            assert report.status == "unsupported"
            with pytest.raises(AttachmentError, match="outside the supervision roadmap"):
                await attach_existing(provider, tmp_path, "session")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "provider,kwargs",
    [
        (AgentId.PRIME_AGENT, {}),
        (AgentId.OPENCODE, {}),
        (AgentId.CODEX, {"socket": Path("/wrong")}),
        (AgentId.PRIME_AGENT, {"socket": Path("/socket"), "after_sequence": 1}),
    ],
)
async def test_endpoint_and_cursor_are_explicit(tmp_path, provider, kwargs):
    with pytest.raises(AttachmentError):
        await attach_existing(provider, tmp_path, "valid", **kwargs)


def test_cli_codex_machine_output_is_bounded_and_content_free(hook_journal):
    repository, store, _ = hook_journal
    runner = CliRunner()
    common = ["--agent", "codex", "--repo", str(repository)]
    result = runner.invoke(app, ["sessions", *common])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["sessions"][0]["selector"] == "codex-local"
    result = runner.invoke(
        app, ["attach", *common, "--session", "codex-local", "--max-events", "1"]
    )
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert [line["type"] for line in lines] == ["attachment", "event", "attachment_end"]
    assert lines[-1]["reason"] == "event_limit"
    assert lines[-1]["last_sequence"] == 1
    assert "SECRET" not in result.output
    assert store.controls_path.read_bytes() == b""


def test_cli_errors_do_not_echo_native_errors_or_credentials(tmp_path, monkeypatch):
    from veyro.supervision import attachment

    async def failing(*args, **kwargs):
        raise RuntimeError("SECRET")

    monkeypatch.setattr(attachment, "discover_sessions", failing)
    result = CliRunner().invoke(app, ["sessions", "--agent", "codex", "--repo", str(tmp_path)])
    assert result.exit_code == 2
    assert "SECRET" not in result.output
    assert json.loads(result.stdout)["type"] == "error"


async def test_opencode_directory_and_limit_are_separate_query_parameters(tmp_path, monkeypatch):
    import io
    import urllib.parse

    from veyro.bridges import opencode

    seen = []

    class Response(io.BytesIO):
        status = 200

        def getcode(self):
            return 200

    class Opener:
        def open(self, request, **kwargs):
            seen.append(request.full_url)
            return Response(b"[]")

    monkeypatch.setattr(opencode.urllib.request, "build_opener", lambda *args: Opener())
    client = OpenCodeClient("http://127.0.0.1:1234", username="user", password="test-password")
    try:
        await client.request_json("GET", "/session?limit=5", directory=tmp_path)
    finally:
        await client.close()
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
    assert query == {"limit": ["5"], "directory": [str(tmp_path)]}


async def test_cli_canary_runs_real_executable_against_existing_journal(hook_journal):
    from veyro.supervision.attachment_canary import run_canary

    repository, _, _ = hook_journal
    before = tree_snapshot(repository)
    result = await run_canary(provider=AgentId.CODEX, repository=repository, selector="codex-local")
    assert result["status"] == "passed"
    assert result["observation"] == "local_journal_only"
    assert not result["controls_enabled"]
    assert tree_snapshot(repository) == before


async def test_codex_cursor_budget_is_cancellable_and_closes_reader(hook_journal, monkeypatch):
    from veyro.supervision import attachment as module
    from veyro.supervision.journal import ReadOnlyJournal

    repository, _, _ = hook_journal
    reader = ReadOnlyJournal.open(repository, "codex-local")
    monkeypatch.setattr(ReadOnlyJournal, "open", lambda *args: reader)
    original_timeout = asyncio.timeout
    budgets = []

    def immediate_timeout(seconds):
        budgets.append(seconds)
        return original_timeout(0)

    monkeypatch.setattr(module.asyncio, "timeout", immediate_timeout)
    with pytest.raises(TimeoutError):
        await attach_existing(AgentId.CODEX, repository, "codex-local", after_sequence=10**12)
    assert budgets == [5]
    assert reader.sequence == 0
    assert reader._descriptors == []


def test_loopback_opencode_credentials_never_use_environment_proxy(monkeypatch):
    import urllib.request

    monkeypatch.setenv("http_proxy", "http://proxy.invalid:8080")
    monkeypatch.setenv("no_proxy", "")
    client = OpenCodeClient("http://127.0.0.1:1234", username="user", password="test-password")
    assert all(
        not handler.proxies
        for handler in client._opener.handlers
        if isinstance(handler, urllib.request.ProxyHandler)
    )


@pytest.mark.parametrize("native_event_first", [False, True])
async def test_prime_eof_reaches_observer_as_metadata_failure(tmp_path, native_event_first):
    from veyro.models import SupervisionEventType

    client = PrimeDaemonClient(Path("/unused"))
    client._reader = asyncio.StreamReader()
    adapter_client = AttachablePrime(tmp_path.resolve())
    bridge = await PrimeAgentDaemonBridge.attach_connected(
        client=adapter_client,
        active_session_id="active-1",
        veyro_session_id="attachment",
        repository=tmp_path,
    )
    bridge._pump.cancel()
    await asyncio.gather(bridge._pump, return_exceptions=True)
    bridge._client = client
    if native_event_first:
        client._reader.feed_data(
            json.dumps(
                {
                    "type": "session_event",
                    "activeSessionId": "active-1",
                    "event": {"type": "turn_start"},
                }
            ).encode()
            + b"\n"
        )
    client._reader.feed_eof()
    await client._read_messages()
    bridge._pump = asyncio.create_task(bridge._pump_outbound())
    stream = bridge.events()
    try:
        assert (await anext(stream)).event_type is SupervisionEventType.SESSION_STARTED
        if native_event_first:
            assert (await anext(stream)).event_type is SupervisionEventType.TURN_STARTED
        failure = await asyncio.wait_for(anext(stream), 1)
        assert failure.event_type is SupervisionEventType.NORMALIZATION_FAILED
        assert failure.payload == {"error_type": "PrimeDaemonError"}
    finally:
        await stream.aclose()
        await bridge.close()


def test_cli_source_failure_is_not_successful_deadline(hook_journal, monkeypatch):
    from veyro.models import SupervisionEventType
    from veyro.supervision import attachment as module

    repository, store, _ = hook_journal
    original = module.attach_existing
    closed = []

    async def failed_attachment(*args, **kwargs):
        result = await original(*args, **kwargs)

        async def failure():
            event = store.replay_events()[0]
            yield event.model_copy(update={"event_type": SupervisionEventType.NORMALIZATION_FAILED})

        original_close = result.close

        async def close():
            closed.append(True)
            await original_close()

        result.events = failure
        result.close = close
        return result

    monkeypatch.setattr(module, "attach_existing", failed_attachment)
    result = CliRunner().invoke(
        app,
        [
            "attach",
            "--agent",
            "codex",
            "--repo",
            str(repository),
            "--session",
            "codex-local",
            "--watch-seconds",
            "1",
        ],
    )
    assert result.exit_code == 2
    assert "attachment_end" not in result.output
    assert json.loads(result.stdout.splitlines()[-1])["type"] == "error"
    assert closed == [True]
