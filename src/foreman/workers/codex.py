from __future__ import annotations

from pathlib import Path

from foreman.agents import AgentId, agent_definition
from foreman.workers.base import coding_mission, mission_for, verification_mission
from foreman.workers.native_cli import NativeCliWorker


class CodexWorker(NativeCliWorker):
    """Codex exec adapter retained as the non-steerable fallback."""

    def __init__(
        self,
        *,
        executable: str = "codex",
        sandbox: str = "workspace-write",
        output_limit: int = 50_000,
        graceful_termination_seconds: float = 5.0,
    ) -> None:
        super().__init__(
            agent_definition(AgentId.CODEX),
            output_limit=output_limit,
            graceful_termination_seconds=graceful_termination_seconds,
        )
        self.executable = executable
        self.sandbox = sandbox

    def command(self, repository: Path, mission: str) -> list[str]:
        return [
            self.executable,
            "exec",
            "--cd",
            str(repository.resolve()),
            "--sandbox",
            self.sandbox,
            "--color",
            "never",
            "--json",
            mission,
        ]



__all__ = ["CodexWorker", "coding_mission", "mission_for", "verification_mission"]
