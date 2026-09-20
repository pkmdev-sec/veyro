from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from veyro.models import EventType, WorkerRecord, WorkerType

EventCallback = Callable[[EventType, dict[str, object]], Awaitable[None]]


def worker_environment() -> dict[str, str]:
    """Return the inherited environment without Veyro supervisor credentials."""

    return {name: value for name, value in os.environ.items() if not name.startswith("TYPESAFE_")}


def codex_environment() -> dict[str, str]:
    """Compatibility alias for older Codex adapter callers."""

    return worker_environment()


def coding_mission(job: str) -> str:
    return f"""Original job:
{job}

You are a coding worker operating on this repository.
Inspect the existing repository and previous work before changing anything.
Continue working toward fully satisfying the original job.
Perform whatever investigation, implementation, debugging, or testing remains necessary.
Do not assume previous workers completed the job correctly.
Run appropriate tests before finishing.
Report what you did, what remains unresolved, and any problems you encountered.
"""


def verification_mission(job: str) -> str:
    return f"""Original job:
{job}

You are an independent verification worker.
Inspect the current repository against the original job.
Verify whether the implementation actually satisfies the request and run appropriate tests.
Look for missing requirements, incorrect behavior, regressions, incomplete implementation,
insufficient tests, and failures hidden by previous workers.
Do not assume previous workers were correct.
Fix any problems you can safely resolve, then report your findings clearly.
"""


def mission_for(worker_type: WorkerType, job: str) -> str:
    if worker_type is WorkerType.VERIFIER:
        return verification_mission(job)
    return coding_mission(job)


class Worker(Protocol):
    async def run(
        self,
        record: WorkerRecord,
        repository: Path,
        emit: EventCallback,
        timeout_seconds: float,
    ) -> WorkerRecord: ...

    async def terminate(self, reason: str) -> None: ...

    async def steer(self, message: str) -> bool: ...
