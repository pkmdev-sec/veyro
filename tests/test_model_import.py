from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import veyro.model_import as model_import_module
from veyro.model_import import (
    GLIFORMER_LARGE_V1_WEIGHTS,
    ArtifactImportError,
    PinnedArtifact,
    import_pinned_artifact,
)


def spec_for(content: bytes) -> PinnedArtifact:
    return PinnedArtifact(
        filename="weights.bin",
        expected_bytes=len(content),
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )


def test_imports_verified_local_artifact_atomically(tmp_path: Path) -> None:
    content = b"pinned model weights"
    source = tmp_path / "download.bin"
    source.write_bytes(content)
    destination = tmp_path / "model" / "weights.bin"

    result = import_pinned_artifact(source, destination, spec_for(content))

    assert result.installed is True
    assert result.destination == destination
    assert result.bytes_written == len(content)
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert destination.read_bytes() == content
    assert list(destination.parent.glob("*.part")) == []


def test_verified_existing_artifact_is_idempotent(tmp_path: Path) -> None:
    content = b"already installed"
    destination = tmp_path / "weights.bin"
    destination.write_bytes(content)

    result = import_pinned_artifact(tmp_path / "unused.bin", destination, spec_for(content))

    assert result.installed is False
    assert destination.read_bytes() == content


def test_rejects_wrong_digest_and_removes_partial_file(tmp_path: Path) -> None:
    source = tmp_path / "download.bin"
    source.write_bytes(b"wrong")
    destination = tmp_path / "model" / "weights.bin"
    expected = spec_for(b"right")

    with pytest.raises(ArtifactImportError, match="size|SHA-256"):
        import_pinned_artifact(source, destination, expected)

    assert not destination.exists()
    assert list(destination.parent.glob("*.part")) == []


def test_rejects_html_even_if_size_and_digest_match(tmp_path: Path) -> None:
    content = b"<!doctype html><html><body>blocked</body></html>"
    source = tmp_path / "download.bin"
    source.write_bytes(content)

    with pytest.raises(ArtifactImportError, match="HTML"):
        import_pinned_artifact(source, tmp_path / "weights.bin", spec_for(content))


def test_refuses_to_overwrite_invalid_existing_artifact(tmp_path: Path) -> None:
    content = b"correct"
    source = tmp_path / "download.bin"
    source.write_bytes(content)
    destination = tmp_path / "weights.bin"
    destination.write_bytes(b"do not overwrite")

    with pytest.raises(ArtifactImportError, match="refusing to overwrite"):
        import_pinned_artifact(source, destination, spec_for(content))

    assert destination.read_bytes() == b"do not overwrite"


def test_refuses_destination_created_during_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"correct"
    source = tmp_path / "download.bin"
    source.write_bytes(content)
    destination = tmp_path / "weights.bin"

    def create_competing_destination(_source: Path, target: Path) -> None:
        Path(target).write_bytes(b"competing file")
        raise FileExistsError

    monkeypatch.setattr(model_import_module.os, "link", create_competing_destination)

    with pytest.raises(ArtifactImportError, match="appeared during import"):
        import_pinned_artifact(source, destination, spec_for(content))

    assert destination.read_bytes() == b"competing file"
    assert list(tmp_path.glob("*.part")) == []


def test_refuses_symbolic_link_destination(tmp_path: Path) -> None:
    content = b"correct"
    source = tmp_path / "download.bin"
    source.write_bytes(content)
    target = tmp_path / "target.bin"
    target.write_bytes(content)
    destination = tmp_path / "weights.bin"
    destination.symlink_to(target)

    with pytest.raises(ArtifactImportError, match="symbolic link"):
        import_pinned_artifact(source, destination, spec_for(content))

    assert destination.is_symlink()
    assert target.read_bytes() == content


def test_rejects_insecure_artifact_url(tmp_path: Path) -> None:
    with pytest.raises(ArtifactImportError, match="HTTPS"):
        import_pinned_artifact(
            "http://artifacts.example/weights.bin",
            tmp_path / "weights.bin",
            spec_for(b"weights"),
        )


def test_pinned_gliformer_identity_matches_canary_manifest() -> None:
    manifest_path = (
        Path(__file__).parents[1] / "config/baselines/jeff-gliformer-large-v1-shadow.json"
    )
    manifest = json.loads(manifest_path.read_text())

    assert GLIFORMER_LARGE_V1_WEIGHTS.filename == manifest["model"]["weights_file"]
    assert GLIFORMER_LARGE_V1_WEIGHTS.expected_bytes == manifest["model"]["expected_bytes"]
    assert GLIFORMER_LARGE_V1_WEIGHTS.expected_sha256 == manifest["model"]["expected_sha256"]


def test_canary_environment_matches_manifest_and_contains_no_secret() -> None:
    root = Path(__file__).parents[1]
    manifest = json.loads(
        (root / "config/baselines/jeff-gliformer-large-v1-shadow.json").read_text()
    )
    settings = {
        name: value
        for name, value in (
            line.split("=", 1)
            for line in (root / "config/canaries/jeff-gliformer-large-v1.env")
            .read_text()
            .splitlines()
            if line and not line.startswith("#")
        )
    }

    assert "JEFF_API_KEYS" not in settings
    assert settings["JEFF_HOST"] == manifest["server"]["host"]
    assert int(settings["JEFF_PORT"]) == manifest["server"]["port"]
    assert int(settings["JEFF_MAX_STATE_CHARS"]) == manifest["server"]["max_state_characters"]
    assert int(settings["JEFF_MAX_QUESTIONS"]) == manifest["server"]["max_questions"]
    assert int(settings["JEFF_MAX_LABELS"]) == manifest["server"]["max_labels_per_question"]
    assert settings["JEFF_BACKEND"] == manifest["inference"]["backend"]
    assert settings["JEFF_DEVICE"] == manifest["inference"]["device"]
    assert settings["JEFF_DTYPE"] == manifest["inference"]["dtype"]
    assert settings["JEFF_ATTN"] == manifest["inference"]["attention"]
    assert float(settings["JEFF_TEMPERATURE"]) == manifest["inference"]["temperature"]
    assert settings["JEFF_ISOLATE"] == manifest["inference"]["isolate"]
    assert settings["JEFF_NOUL_MODE"] == manifest["inference"]["noul_mode"]
    assert settings["JEFF_STATE_FORMAT"] == manifest["inference"]["state_format"]
    assert bool(int(settings["JEFF_COMPILE"])) is manifest["inference"]["compile"]
    assert bool(int(settings["JEFF_WARMUP"])) is manifest["inference"]["warmup"]
    assert int(settings["JEFF_MAX_BATCH"]) == manifest["inference"]["max_batch"]
    assert int(settings["JEFF_MAX_WAIT_MS"]) == manifest["inference"]["max_wait_ms"]
    assert int(settings["JEFF_MAX_QUEUE"]) == manifest["inference"]["max_queue"]
