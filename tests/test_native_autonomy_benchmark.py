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


def test_forced_repair_fixture_fails_only_first_veyro_checkpoint(tmp_path):
    check, originals = BENCHMARK["write_live_fixture"](tmp_path, True)
    (tmp_path / "slug.py").write_text(
        "import re\ndef slug(text): return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n"
    )
    state = tmp_path / ".veyro" / "autonomy" / "run" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"phase": "building"}))

    building = BENCHMARK["subprocess"].run(
        [BENCHMARK["sys"].executable, check.name], cwd=tmp_path, capture_output=True, text=True
    )
    state.write_text(json.dumps({"phase": "verifying"}))
    first = BENCHMARK["subprocess"].run(
        [BENCHMARK["sys"].executable, check.name], cwd=tmp_path, capture_output=True, text=True
    )
    state.write_text(json.dumps({"phase": "verifying", "continuations": 1}))
    second = BENCHMARK["subprocess"].run(
        [BENCHMARK["sys"].executable, check.name], cwd=tmp_path, capture_output=True, text=True
    )

    assert building.returncode == 0
    assert first.returncode != 0
    assert second.returncode == 0
    assert all(path.read_bytes() == original for path, original in originals.items())


def test_forced_review_fixture_uses_specification_only(tmp_path):
    check, originals = BENCHMARK["write_live_fixture"](tmp_path, False, True)
    text = check.read_text()

    assert "reread the original task and current implementation" in text
    assert "CAFÉ" not in text
    assert "[^a-z0-9]" not in text
    assert all(path.read_bytes() == original for path, original in originals.items())
