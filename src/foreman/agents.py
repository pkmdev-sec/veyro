from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from foreman.native_session import ManagedNativeResult, NativeSessionRecord


class AgentId(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    PRIME_AGENT = "prime-agent"
    PI = "pi"
    OPENCODE = "opencode"


AdapterCapability = Literal["start", "stream", "cancel", "wait", "result", "steer"]


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_id: AgentId
    display_name: str
    executable: str
    machine_interface: str
    interface_evidence: str
    interface_stability: Literal["documented", "experimental"]
    capabilities: frozenset[AdapterCapability]

    def interactive_command(
        self, prompt: str | None = None, native_args: tuple[str, ...] = ()
    ) -> list[str]:
        command = [self.executable, *native_args]
        if prompt:
            if self.agent_id is AgentId.OPENCODE:
                command.extend(["--prompt", prompt])
            else:
                if prompt.startswith("-"):
                    command.append("--")
                command.append(prompt)
        return command

    def worker_command(self, repository: Path, mission: str) -> list[str]:
        repo = str(repository.resolve())
        if self.agent_id is AgentId.CODEX:
            return [
                self.executable,
                "exec",
                "--cd",
                repo,
                "--sandbox",
                "workspace-write",
                "--color",
                "never",
                "--json",
                mission,
            ]
        if self.agent_id is AgentId.CLAUDE:
            return [
                self.executable,
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
                "--permission-mode",
                "acceptEdits",
                mission,
            ]
        if self.agent_id is AgentId.PRIME_AGENT:
            return [
                self.executable,
                "--print",
                "--mode",
                "json",
                "--cwd",
                repo,
                mission,
            ]
        if self.agent_id is AgentId.PI:
            return [self.executable, "--print", "--mode", "json", mission]
        if self.agent_id is AgentId.OPENCODE:
            return [
                self.executable,
                "run",
                "--format",
                "json",
                "--dir",
                repo,
                mission,
            ]
        raise AssertionError(f"unmapped agent: {self.agent_id}")


_COMMON_CAPABILITIES: frozenset[AdapterCapability] = frozenset(
    {"start", "stream", "cancel", "wait", "result"}
)

AGENTS: dict[AgentId, AgentDefinition] = {
    AgentId.CODEX: AgentDefinition(
        agent_id=AgentId.CODEX,
        display_name="Codex",
        executable="codex",
        machine_interface="stable hooks and exec JSON lines; opt-in experimental app-server",
        interface_evidence="codex 0.154.0 hook schemas; codex queue/exec/app-server --help",
        interface_stability="documented",
        capabilities=_COMMON_CAPABILITIES,
    ),
    AgentId.CLAUDE: AgentDefinition(
        agent_id=AgentId.CLAUDE,
        display_name="Claude Code",
        executable="claude",
        machine_interface="stream-json over stdio",
        interface_evidence="claude --help: --print --output-format stream-json",
        interface_stability="documented",
        capabilities=_COMMON_CAPABILITIES,
    ),
    AgentId.PRIME_AGENT: AgentDefinition(
        agent_id=AgentId.PRIME_AGENT,
        display_name="Prime Agent",
        executable="prime-agent",
        machine_interface="daemon protocol 7/schema 29; JSON over stdio fallback",
        interface_evidence=(
            "prime-agent 0.9.5 daemon hello; installed daemon-protocol.d.ts; prime-agent --help"
        ),
        interface_stability="experimental",
        capabilities=_COMMON_CAPABILITIES,
    ),
    AgentId.PI: AgentDefinition(
        agent_id=AgentId.PI,
        display_name="Pi",
        executable="pi",
        machine_interface="JSON over stdio",
        interface_evidence="pi --help: --print --mode json",
        interface_stability="documented",
        capabilities=_COMMON_CAPABILITIES,
    ),
    AgentId.OPENCODE: AgentDefinition(
        agent_id=AgentId.OPENCODE,
        display_name="OpenCode",
        executable="opencode",
        machine_interface="authenticated loopback HTTP/SSE; JSON over stdio fallback",
        interface_evidence="opencode 1.18.30 serve/attach help; authenticated GET /doc",
        interface_stability="documented",
        capabilities=_COMMON_CAPABILITIES,
    ),
}


def agent_definition(agent_id: AgentId | str) -> AgentDefinition:
    return AGENTS[AgentId(agent_id)]


def probe_agent(definition: AgentDefinition) -> dict[str, object]:
    path = shutil.which(definition.executable)
    version = None
    error = None
    if path:
        try:
            result = subprocess.run(
                [path, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = result.stdout.strip() or result.stderr.strip()
            version = output.splitlines()[0] if output else None
            if result.returncode != 0:
                error = f"version probe exited {result.returncode}"
        except (OSError, subprocess.SubprocessError) as exc:
            error = str(exc)
    return {
        "protocol_version": "1.0",
        "agent_id": definition.agent_id.value,
        "display_name": definition.display_name,
        "available": path is not None,
        "executable": path,
        "version": version,
        "machine_interface": definition.machine_interface,
        "interface_evidence": definition.interface_evidence,
        "interface_stability": definition.interface_stability,
        "native_interactive": True,
        "managed_native": True,
        "managed_observation": ["process_lifecycle", "workspace_state"],
        "capabilities": sorted(definition.capabilities),
        "probe_error": error,
    }


def probe_agents() -> list[dict[str, object]]:
    return [probe_agent(definition) for definition in AGENTS.values()]


def launch_native_agent(
    agent_id: AgentId,
    repository: Path,
    prompt: str | None,
    native_args: tuple[str, ...] = (),
    on_started: Callable[[NativeSessionRecord], None] | None = None,
) -> ManagedNativeResult:
    from foreman.native_session import ManagedNativeSession

    definition = agent_definition(agent_id)
    executable = shutil.which(definition.executable)
    if executable is None:
        raise FileNotFoundError(f"native agent executable not found: {definition.executable}")
    probe = probe_agent(definition)
    command = definition.interactive_command(prompt, native_args)
    command[0] = executable
    return ManagedNativeSession(
        definition=definition,
        repository=repository,
        command=command,
        executable=executable,
        agent_version=probe["version"] if isinstance(probe["version"], str) else None,
        prompt=prompt,
        on_started=on_started,
    ).run()


def probes_json() -> str:
    return json.dumps({"protocol_version": "1.0", "agents": probe_agents()}, indent=2)
