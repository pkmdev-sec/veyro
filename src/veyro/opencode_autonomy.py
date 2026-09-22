from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _decision_marker(state: dict) -> str:
    return json.dumps(state.get("decision"), sort_keys=True)


def _await_checkpoint(state_path: Path, previous: str) -> dict:
    config = _read_json(state_path.with_name("config.json"))
    deadline = config.get("deadline")
    if not isinstance(deadline, (int, float)):
        deadline = time.time() + 1
    elif "judge" not in config:
        check_budget = len(config.get("checks", [])) * config.get("check_timeout", 0) + 5
        deadline = min(deadline, time.time() + check_budget)
    while time.time() < deadline:
        state = _read_json(state_path)
        if isinstance(state.get("decision"), dict) and _decision_marker(state) != previous:
            return state
        if state_path.with_name("adapter-error.json").exists():
            return {}
        time.sleep(0.05)
    return {}


def _proxy_environment() -> tuple[object | None, dict[str, str] | None]:
    if os.environ.get("VEYRO_OLLAMA_NATIVE_PROXY") != "1":
        return None, None
    from veyro.ollama_proxy import start

    environment = os.environ.copy()
    config = json.loads(environment.get("OPENCODE_CONFIG_CONTENT", "{}"))
    models = config["provider"]["veyro-local"]["models"]
    if not isinstance(models, dict) or len(models) != 1:
        raise ValueError("local proxy requires exactly one pinned model")
    model, model_config = next(iter(models.items()))
    limit = model_config.get("limit") if isinstance(model_config, dict) else None
    context_size = limit.get("context") if isinstance(limit, dict) else None
    if isinstance(context_size, bool) or not isinstance(context_size, int):
        raise ValueError("local proxy requires one integer model context limit")
    server, base_url = start(model, context_size=context_size)
    config["provider"]["veyro-local"]["options"]["baseURL"] = base_url
    environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    return server, environment


def run(state_path: Path, command: list[str]) -> int:
    server, environment = _proxy_environment()
    try:
        continuation_command = command[:-1]
        last_continuation = 0
        while True:
            previous = _decision_marker(_read_json(state_path))
            result = subprocess.run(command, check=False, env=environment)
            state = _await_checkpoint(state_path, previous)
            decision = state.get("decision")
            if not isinstance(decision, dict):
                return result.returncode or 1
            if decision.get("action") != "continue":
                return result.returncode or 1
            session = state.get("session")
            message = decision.get("message")
            continuation = decision.get("continuations")
            if (
                not isinstance(session, str)
                or not isinstance(message, str)
                or not message
                or isinstance(continuation, bool)
                or not isinstance(continuation, int)
                or continuation <= last_continuation
            ):
                return 1
            last_continuation = continuation
            command = [*continuation_command, "--session", session, message]
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


def main() -> int:
    if len(sys.argv) < 4:
        raise SystemExit("usage: opencode_autonomy.py STATE_PATH COMMAND [ARG ...]")
    return run(Path(sys.argv[1]), sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
