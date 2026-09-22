from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from veyro.bridges.codex_hooks import CODEX_VERSION, codex_hook_capabilities
from veyro.bridges.opencode import (
    OPENCODE_API_VERSION,
    OPENCODE_VERSION,
    opencode_server_capabilities,
)
from veyro.bridges.prime_agent import (
    DAEMON_PROTOCOL_VERSION,
    DAEMON_SCHEMA_ID,
    DAEMON_SCHEMA_REVISION,
    PRIME_AGENT_VERSION,
    prime_daemon_capabilities,
)
from veyro.cli import app
from veyro.models.authorization import AuthorizationReason
from veyro.supervision.checkpoints import (
    AUTHORITATIVE_MODEL_CHECKPOINT,
    AUTHORITATIVE_PROVIDER_ID,
)

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def test_capability_matrix_matches_every_adapter_declaration():
    reference = (DOCS / "supervision-reference.md").read_text()
    matrices = [
        prime_daemon_capabilities(),
        opencode_server_capabilities(),
        codex_hook_capabilities(),
    ]
    expected = []
    for declarations in zip(*(matrix.declarations for matrix in matrices), strict=True):
        capability = declarations[0].capability
        assert all(row.capability is capability for row in declarations)
        cells = [
            f"supported / {row.stability.value}"
            if row.availability.value == "supported"
            else row.availability.value
            for row in declarations
        ]
        expected.append(f"| `{capability.value}` | " + " | ".join(cells) + " |")
    actual = [line for line in reference.splitlines() if line.startswith("| `")]
    assert actual == expected


def test_reference_pins_match_runtime():
    reference = (DOCS / "supervision-reference.md").read_text()
    for provider, version in [
        ("Prime Agent", PRIME_AGENT_VERSION),
        ("OpenCode", OPENCODE_VERSION),
        ("Codex", CODEX_VERSION),
    ]:
        assert f"| {provider} | `{version}` |" in reference
    for value in [
        f"protocol `{DAEMON_PROTOCOL_VERSION}`",
        f"revision `{DAEMON_SCHEMA_REVISION}`",
        f"`{DAEMON_SCHEMA_ID}`",
        f"OpenAPI `{OPENCODE_API_VERSION}`",
        f"`{AUTHORITATIVE_PROVIDER_ID}`",
        f"`{AUTHORITATIVE_MODEL_CHECKPOINT}`",
    ]:
        assert value in reference


def test_recovery_covers_every_authorization_reason():
    guide = (DOCS / "supervision-operator-guide.md").read_text()
    recovery = guide.split("## Recover without duplicate delivery", 1)[1]
    for reason in AuthorizationReason:
        assert f"`{reason.value}`" in recovery


@pytest.mark.parametrize(
    "command,flags",
    [
        ("sessions", ["--agent", "--repo", "--socket", "--server", "--limit"]),
        ("attach", ["--session", "--watch-seconds", "--max-events", "--after-sequence"]),
        ("supervise", ["--proposal", "--policy", "--ledger-dir", "--timeout-seconds"]),
    ],
)
def test_operator_flags_exist_in_executable_help(command, flags):
    guide = (DOCS / "supervision-operator-guide.md").read_text()
    result = CliRunner().invoke(app, [command, "--help"], color=False)
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    for flag in flags:
        assert flag in guide
        assert flag in plain


def test_operator_documents_link_to_real_files_and_test_paths():
    for name in [
        "supervision-operator-guide.md",
        "supervision-reference.md",
        "supervision-verification.md",
    ]:
        text = (DOCS / name).read_text()
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if "://" not in target:
                path, _, anchor = target.partition("#")
                linked = DOCS / path
                assert linked.is_file(), target
                if anchor:
                    headings = re.findall(r"^#+ (.+)$", linked.read_text(), re.MULTILINE)
                    slugs = {
                        re.sub(r"[^a-z0-9 _-]", "", heading.lower()).replace(" ", "-")
                        for heading in headings
                    }
                    assert anchor in slugs, target
        for target in re.findall(r"tests/[a-z_]+\.py", text):
            assert (ROOT / target).is_file(), target


def test_recorded_reports_match_current_pins_and_scope():
    record = json.loads((DOCS / "supervision-verification.json").read_text())
    reports = record["native_checks"]
    assert set(reports) == {"prime_observe", "prime_approved", "opencode", "codex"}
    for key, version in [
        ("prime_observe", PRIME_AGENT_VERSION),
        ("prime_approved", PRIME_AGENT_VERSION),
        ("opencode", OPENCODE_VERSION),
        ("codex", CODEX_VERSION),
    ]:
        report = reports[key]["report"]
        assert report["status"] == "passed"
        assert report["version"] == version
        assert report["prompt_sent"] is False
    for key in ["prime_observe", "opencode"]:
        report = reports[key]["report"]
        assert report["existing_session_preserved"] is True
        assert report["supervise_observe_only"] is True
        assert report["controls_enabled"] is False
        assert report["native_history_complete"] is False
    approved = reports["prime_approved"]["report"]
    assert approved["native_fixture_removed"] is True
    assert approved["delivery_claims"] == 1
    assert approved["verification"] in {"session_completed", "session_failed"}
    assert approved["checkpoint_assessed"] is True
    assert reports["codex"]["report"]["temporary_tree_removed"] is True
