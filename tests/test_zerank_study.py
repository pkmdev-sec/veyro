from __future__ import annotations

import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

from veyro import selene_training


def test_offline_worker_process_has_a_deadline(tmp_path, monkeypatch):
    launcher = tmp_path / "worker.py"
    launcher.write_text("import time\ntime.sleep(60)\n")
    launcher.chmod(0o700)
    python = tmp_path / "venv-python"
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(str(Path(sys.executable).resolve()))} "$@"\n')
    python.chmod(0o700)
    runtime = SimpleNamespace(
        python=python,
        python_sha256=selene_training._sha256(python),
        launcher=launcher,
        launcher_sha256=selene_training._sha256(launcher),
    )
    monkeypatch.setattr(selene_training, "_offline_command", lambda command: command)

    exit_code, stdout, _stderr, timed_out = selene_training._run_worker(
        runtime,
        {"operation": "deadline-check"},
        timeout=0.05,
        model_root=tmp_path,
    )

    assert timed_out is True
    assert exit_code != 0
    assert stdout == b""
