from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from veyro.autonomy import AutonomyOptions
    from veyro.native_session import ManagedNativeResult, NativeSessionRecord


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
    autonomy_versions: frozenset[str] = frozenset()

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
        interface_evidence="codex 0.154.0 hook schemas; codex exec/app-server --help",
        interface_stability="documented",
        capabilities=_COMMON_CAPABILITIES,
        autonomy_versions=frozenset({"0.154.0"}),
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
        autonomy_versions=frozenset({"0.9.5"}),
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
        autonomy_versions=frozenset({"1.18.30"}),
    ),
}


def agent_definition(agent_id: AgentId | str) -> AgentDefinition:
    return AGENTS[AgentId(agent_id)]


def _qualified_version(definition: AgentDefinition, output: str | None) -> str | None:
    if output is None or "\n" in output or "\r" in output:
        return None
    patterns = {
        AgentId.CODEX: r"codex-cli ([0-9]+\.[0-9]+\.[0-9]+)",
        AgentId.PRIME_AGENT: r"prime-agent ([0-9]+\.[0-9]+\.[0-9]+)",
        AgentId.OPENCODE: r"([0-9]+\.[0-9]+\.[0-9]+)",
    }
    pattern = patterns.get(definition.agent_id)
    match = re.fullmatch(pattern, output) if pattern else None
    if match is None or match.group(1) not in definition.autonomy_versions:
        return None
    return match.group(1)


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
            version = output if output else None
            if result.returncode != 0:
                error = f"version probe exited {result.returncode}"
        except (OSError, subprocess.SubprocessError) as exc:
            error = str(exc)
    qualified_version = _qualified_version(definition, version) if error is None else None
    return {
        "protocol_version": "1.0",
        "agent_id": definition.agent_id.value,
        "display_name": definition.display_name,
        "available": path is not None,
        "executable": path,
        "version": version,
        "qualified_version": qualified_version,
        "version_qualified": qualified_version is not None,
        "required_autonomy_versions": sorted(definition.autonomy_versions),
        "machine_interface": definition.machine_interface,
        "interface_evidence": definition.interface_evidence,
        "interface_stability": definition.interface_stability,
        "native_interactive": True,
        "managed_native": True,
        "managed_observation": ["process_lifecycle", "workspace_state"],
        "capabilities": sorted(definition.capabilities),
        "probe_error": error,
    }


def require_qualified_autonomy_version(
    definition: AgentDefinition, probe: Mapping[str, object]
) -> str:
    qualified = probe.get("qualified_version")
    if probe.get("probe_error") is not None or not probe.get("version_qualified"):
        observed = probe.get("version")
        required = ", ".join(sorted(definition.autonomy_versions)) or "none"
        raise ValueError(
            f"{definition.display_name} autonomous launch requires exact qualified version "
            f"{required}; observed {observed!r}"
        )
    if not isinstance(qualified, str) or qualified not in definition.autonomy_versions:
        raise ValueError(f"{definition.display_name} returned invalid qualification evidence")
    return qualified


def probe_agents() -> list[dict[str, object]]:
    return [probe_agent(definition) for definition in AGENTS.values()]


def _recover_local_model(launch) -> None:
    identity = launch.coding_model
    if identity is None:
        return
    record = {
        "coding_model": {
            "profile": identity.profile_id,
            "requested_model": identity.requested_model,
            "manifest_sha256": identity.manifest_sha256,
            "blob_sha256": identity.blob_sha256,
        }
    }
    try:
        result = subprocess.run(
            ["ollama", "stop", identity.requested_model],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        record.update(
            {"exit_code": result.returncode, "output": (result.stdout + result.stderr)[-2000:]}
        )
    except (OSError, subprocess.SubprocessError) as error:
        record.update({"exit_code": None, "error": str(error)})
    fd, temporary = tempfile.mkstemp(prefix=".model-recovery.", dir=launch.directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, launch.directory / "model-recovery.json")
    finally:
        Path(temporary).unlink(missing_ok=True)


def launch_native_agent(
    agent_id: AgentId,
    repository: Path,
    prompt: str | None,
    native_args: tuple[str, ...] = (),
    on_started: Callable[[NativeSessionRecord], None] | None = None,
    autonomy: AutonomyOptions | None = None,
) -> ManagedNativeResult:
    from veyro.native_session import ManagedNativeSession

    definition = agent_definition(agent_id)
    executable = shutil.which(definition.executable)
    if executable is None:
        raise FileNotFoundError(f"native agent executable not found: {definition.executable}")
    probe = probe_agent(definition)
    qualified_version = None
    if autonomy is not None:
        qualified_version = require_qualified_autonomy_version(definition, probe)
    command = definition.interactive_command(prompt, native_args)
    autonomous = None
    if autonomy is not None:
        from veyro.autonomy import prepare_autonomy

        autonomous = prepare_autonomy(agent_id, repository, prompt or "", native_args, autonomy)
        command = autonomous.command
    command[command.index(definition.executable)] = executable

    def completed() -> bool:
        return False

    result = ManagedNativeSession(
        definition=definition,
        repository=repository,
        command=command,
        executable=executable,
        agent_version=qualified_version
        if autonomy is not None
        else (probe["version"] if isinstance(probe["version"], str) else None),
        prompt=prompt,
        on_started=on_started,
        environment=autonomous.environment if autonomous else None,
        timeout_seconds=autonomy.timeout if autonomy else None,
        completion_check=completed if autonomous else None,
    ).run()
    if autonomous:
        from dataclasses import replace

        succeeded = completed()
        if autonomous.coding_model and not succeeded:
            _recover_local_model(autonomous)
        result = replace(
            result,
            autonomy_directory=autonomous.directory,
            exit_code=(result.exit_code or (0 if succeeded else 1)),
        )
    return result


def probes_json() -> str:
    return json.dumps({"protocol_version": "1.0", "agents": probe_agents()}, indent=2)
