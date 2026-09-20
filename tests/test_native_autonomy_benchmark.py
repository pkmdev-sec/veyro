from __future__ import annotations

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCHMARK = runpy.run_path(str(Path(__file__).parents[1] / "tools/benchmark_native_autonomy.py"))


@pytest.mark.parametrize("acknowledged", [True, False])
def test_cleanup_only_stops_sessions_in_disposable_workspace(tmp_path, monkeypatch, acknowledged):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "list":
            result = {
                "sessions": [
                    {"sessionId": "owned", "cwd": str(tmp_path)},
                    {"sessionId": "unrelated", "cwd": str(tmp_path.parent)},
                ]
            }
        else:
            result = {"success": acknowledged}
        return SimpleNamespace(stdout=json.dumps(result))

    monkeypatch.setattr(BENCHMARK["subprocess"], "run", run)
    result = BENCHMARK["stop_disposable_prime"](tmp_path)
    assert result["success"] is acknowledged
    assert commands == [
        ["prime-agent", "list", "--json"],
        ["prime-agent", "stop", "owned", "--json"],
    ]
