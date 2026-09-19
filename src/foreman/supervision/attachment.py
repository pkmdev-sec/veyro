from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from foreman.agents import AgentId
from foreman.bridges.codex_hooks import CodexJournalObservation, discover_hook_journals
from foreman.bridges.opencode import OpenCodeClient, OpenCodeServerBridge
from foreman.bridges.prime_agent import PrimeAgentDaemonBridge, PrimeDaemonClient
from foreman.models import CapabilitySet, SessionIdentity, SupervisionEvent
from foreman.models.attachment import (
    AttachmentHistory,
    AttachmentReport,
    SessionCandidate,
    SessionDiscovery,
)


class AttachmentError(RuntimeError):
    pass


class ObservationSource(Protocol):
    identity: SessionIdentity
    capabilities: CapabilitySet

    def events(self) -> AsyncIterator[SupervisionEvent]: ...
    async def close(self) -> None: ...


def _check_endpoint(provider: AgentId, socket: Path | None, server: str | None) -> None:
    if provider is AgentId.PRIME_AGENT and (socket is None or server is not None):
        raise AttachmentError("Prime Agent requires --socket and does not accept --server")
    if provider is AgentId.OPENCODE and (server is None or socket is not None):
        raise AttachmentError("OpenCode requires --server and does not accept --socket")
    if provider is AgentId.CODEX and (server is not None or socket is not None):
        raise AttachmentError("Codex journal attachment does not use a native endpoint")


async def discover_sessions(
    provider: AgentId,
    repository: Path,
    *,
    socket: Path | None = None,
    server: str | None = None,
    limit: int = 100,
) -> SessionDiscovery:
    if not 1 <= limit <= 1000:
        raise AttachmentError("discovery limit must be 1..1000")
    if provider in {AgentId.PI, AgentId.CLAUDE}:
        return SessionDiscovery(
            provider_id=provider.value,
            status="unsupported",
            detail="Provider bridge is outside the supervision roadmap.",
        )
    _check_endpoint(provider, socket, server)
    if provider is AgentId.CODEX:
        return discover_hook_journals(repository, limit=limit)
    if provider is AgentId.PRIME_AGENT:
        assert socket is not None
        client = PrimeDaemonClient(socket, timeout=5)
        try:
            await client.connect()
            return await client.discover(repository, limit=limit)
        finally:
            await client.close()
    assert server is not None
    client = OpenCodeClient.from_environment(server)
    try:
        return await client.discover(repository, limit=limit)
    finally:
        await client.close()


class ExistingSessionAttachment:
    """Own only observer resources; expose no control-dispatch method."""

    def __init__(self, source: ObservationSource, report: AttachmentReport):
        self._source = source
        self.report = report

    @property
    def incomplete_tail(self) -> bool:
        return (
            self._source.incomplete_tail
            if isinstance(self._source, CodexJournalObservation)
            else False
        )

    def events(self) -> AsyncIterator[SupervisionEvent]:
        return self._source.events()

    async def close(self) -> None:
        await self._source.close()


async def attach_existing(
    provider: AgentId,
    repository: Path,
    selector: str,
    *,
    socket: Path | None = None,
    server: str | None = None,
    after_sequence: int = 0,
    follow_journal: bool = False,
) -> ExistingSessionAttachment:
    if provider in {AgentId.PI, AgentId.CLAUDE}:
        raise AttachmentError("Provider bridge is outside the supervision roadmap")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", selector):
        raise AttachmentError("invalid session selector")
    if after_sequence < 0 or (after_sequence and provider is not AgentId.CODEX):
        raise AttachmentError("after-sequence applies only to Codex local journal history")
    _check_endpoint(provider, socket, server)
    repository = await asyncio.to_thread(repository.resolve)
    if provider is AgentId.CODEX:
        source = CodexJournalObservation(
            repository,
            selector,
            follow=follow_journal,
        )
        try:
            async with asyncio.timeout(5):
                await source.seek_after(after_sequence)
        except BaseException:
            await source.close()
            raise
        candidate = source.candidate
        history = AttachmentHistory(
            mode="local_hook_journal",
            after_sequence=after_sequence,
            detail="Local receipt order only. Native gaps and native liveness are unknown.",
        )
    elif provider is AgentId.PRIME_AGENT:
        assert socket is not None
        source = await PrimeAgentDaemonBridge.attach(
            socket_path=socket,
            active_session_id=selector,
            foreman_session_id=f"attach-{uuid4().hex}",
            repository=repository,
        )
        candidate = SessionCandidate(
            provider_id=provider.value,
            selector=selector,
            native_session_id=selector,
            repository=str(repository),
            provider_version=source.identity.provider_version,
            mode="native_daemon",
            native_liveness="loaded",
        )
        history = source.attachment_history
    else:
        assert server is not None
        client = OpenCodeClient.from_environment(server)
        try:
            source = await OpenCodeServerBridge.attach_connected(
                client=client,
                provider_session_id=selector,
                foreman_session_id=f"attach-{uuid4().hex}",
                repository=repository,
            )
        except BaseException:
            await client.close()
            raise
        candidate = SessionCandidate(
            provider_id=provider.value,
            selector=selector,
            native_session_id=selector,
            repository=str(repository),
            provider_version=source.identity.provider_version,
            mode="native_server",
            native_liveness="unknown",
        )
        history = AttachmentHistory(
            mode="unavailable",
            native_replay_status="unavailable",
            detail="Live SSE after connection only; no native history or transcript fetched.",
        )
    return ExistingSessionAttachment(
        source,
        AttachmentReport(
            candidate=candidate,
            identity=source.identity,
            history=history,
            observation=(
                "local_journal_only" if provider is AgentId.CODEX else "live_native_stream"
            ),
            provider_capabilities=source.capabilities,
        ),
    )
