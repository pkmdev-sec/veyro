from __future__ import annotations

import hashlib
import json
import shlex
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from veyro import selene_quantization, selene_quantization_worker
from veyro.selene_quantization import (
    DeploymentArtifact,
    DeploymentManifest,
    QuantizationKind,
    QuantizationRun,
    QuantizationRuntime,
    preflight_quantization,
    run_quantization,
)
from veyro.selene_training import (
    AdapterArtifact,
    AdapterManifest,
    ApprovalStatus,
    LicenseApproval,
    ModelFile,
    TrainableModelManifest,
    TrainingMethod,
    TrainingStage,
    _json_sha256,
)


def sha(path_or_value: Path | bytes | str) -> str:
    if isinstance(path_or_value, Path):
        value = path_or_value.read_bytes()
    elif isinstance(path_or_value, str):
        value = path_or_value.encode()
    else:
        value = path_or_value
    return hashlib.sha256(value).hexdigest()


def executable(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> None:
    path.write_text(content)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def write_model(tmp_path: Path) -> tuple[Path, TrainableModelManifest]:
    root = tmp_path / "model"
    root.mkdir()
    files = []
    for name, kind, content in (
        ("model.safetensors", "weight", b"weights"),
        ("config.json", "config", b"{}"),
        ("tokenizer.json", "tokenizer", b"tokenizer"),
        ("README.md", "metadata", b"card"),
    ):
        path = root / name
        path.write_bytes(content)
        files.append(ModelFile(path=name, size=path.stat().st_size, sha256=sha(path), kind=kind))
    uses = frozenset({"fine_tune", "quantize", "redistribute", "deploy"})
    approvals = []
    for index, subject in enumerate(
        (
            "AtlaAI/Selene-1-Mini-Llama-3.1-8B",
            "meta-llama/Llama-3.1-8B-Instruct",
        )
    ):
        text = tmp_path / f"license-{index}.txt"
        text.write_text("approved licence text")
        approvals.append(
            LicenseApproval(
                subject=subject,
                license_id=f"license-{index}",
                license_text_path=text,
                license_text_sha256=sha(text),
                source_url="https://example.test/license",
                reviewed_by="independent-licence-reviewer",
                reviewed_at=datetime(2026, 9, 22, tzinfo=UTC),
                status=ApprovalStatus.APPROVED,
                approved_uses=uses,
            )
        )
    manifest = TrainableModelManifest(
        repository="AtlaAI/Selene-1-Mini-Llama-3.1-8B",
        revision="a" * 40,
        base_model="meta-llama/Llama-3.1-8B-Instruct",
        root=root,
        repository_file_manifest_sha256=_json_sha256(
            [item.model_dump(mode="json") for item in files]
        ),
        files=files,
        approvals=approvals,
    )
    path = tmp_path / "model-manifest.json"
    path.write_text(manifest.model_dump_json(indent=2))
    return path, manifest


def write_adapter(tmp_path: Path, model: TrainableModelManifest) -> tuple[Path, AdapterManifest]:
    root = tmp_path / "training-output"
    adapter = root / "adapter"
    adapter.mkdir(parents=True)
    weights = adapter / "adapters.safetensors"
    config = adapter / "adapter_config.json"
    weights.write_bytes(b"adapter weights")
    config.write_text("{}")
    artifacts = [
        AdapterArtifact(
            path=f"adapter/{path.name}",
            size=path.stat().st_size,
            sha256=sha(path),
        )
        for path in (weights, config)
    ]
    manifest = AdapterManifest(
        run_id="training-run",
        run_sha256="b" * 64,
        base_model_identity=f"{model.repository}@{model.revision}",
        framework="mlx-lm",
        framework_version="0.31.3",
        stage=TrainingStage.SUPERVISED,
        method=TrainingMethod.LORA,
        terminal_status="succeeded",
        artifacts=artifacts,
    )
    path = root / "adapter-manifest.json"
    path.write_text(manifest.model_dump_json(indent=2))
    return path, manifest


def write_runtime(tmp_path: Path) -> tuple[Path, QuantizationRuntime]:
    launcher = tmp_path / "quantization-worker.py"
    executable(launcher, "#!/usr/bin/env python3\n")
    packages = tmp_path / "quantization-packages.txt"
    packages.write_text("mlx-lm==0.31.3\n")
    quantizer = tmp_path / "llama-quantize"
    executable(quantizer)
    python = tmp_path / "venv-python"
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(str(Path(sys.executable).resolve()))} "$@"\n')
    python.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    runtime = QuantizationRuntime(
        framework_version="0.31.3",
        python=python,
        python_sha256=sha(python),
        launcher=launcher,
        launcher_sha256=sha(launcher),
        package_manifest=packages,
        package_manifest_sha256=sha(packages),
        quantizer=quantizer,
        quantizer_sha256=sha(quantizer),
        quantizer_version="llama.cpp@test",
        supported_quantizations=frozenset({QuantizationKind.Q4_K_M}),
    )
    path = tmp_path / "quantization-runtime.json"
    path.write_text(runtime.model_dump_json(indent=2))
    return path, runtime


def quantization_run(tmp_path: Path, *, identifier: str = "deployment-run"):
    model_path, model = write_model(tmp_path)
    adapter_path, _ = write_adapter(tmp_path, model)
    runtime_path, _ = write_runtime(tmp_path)
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    run = QuantizationRun(
        id=identifier,
        created_at=datetime(2026, 9, 22, tzinfo=UTC),
        model_manifest=model_path,
        model_manifest_sha256=sha(model_path),
        adapter_manifest=adapter_path,
        adapter_manifest_sha256=sha(adapter_path),
        runtime_manifest=runtime_path,
        runtime_manifest_sha256=sha(runtime_path),
        output_directory=tmp_path / "deployment-output",
        receipt_directory=receipts,
        quantization=QuantizationKind.Q4_K_M,
        expected_peak_bytes=1,
        safety_reserve_bytes=1,
        timeout_seconds=60,
    )
    return run


def test_quantization_preflight_verifies_model_adapter_runtime_licence_and_disk(tmp_path):
    run = quantization_run(tmp_path)
    result = preflight_quantization(run)
    assert result.ready is True
    assert result.blockers == []
    assert result.model_identity.endswith("@" + "a" * 40)

    adapter = run.adapter_manifest.parent / "adapter/adapters.safetensors"
    adapter.write_bytes(b"changed")
    blocked = preflight_quantization(run)
    assert blocked.ready is False
    assert any(item.startswith("adapter_artifact_invalid") for item in blocked.blockers)


def test_quantization_preflight_rejects_writable_interpreter_target(tmp_path):
    run = quantization_run(tmp_path)
    unsafe = tmp_path / "writable-python"
    unsafe.write_text("#!/bin/sh\nexit 0\n")
    unsafe.chmod(0o777)
    payload = json.loads(run.runtime_manifest.read_text())
    python = Path(payload["python"])
    python.unlink()
    python.symlink_to(unsafe)
    payload["python_sha256"] = sha(unsafe)
    run.runtime_manifest.write_text(json.dumps(payload))
    changed = run.model_copy(update={"runtime_manifest_sha256": sha(run.runtime_manifest)})

    result = preflight_quantization(changed)
    assert "quantization_runtime_invalid:python" in result.blockers


def test_quantization_preflight_refuses_an_unfunded_peak_disk_budget(tmp_path):
    run = quantization_run(tmp_path)
    run = run.model_copy(update={"expected_peak_bytes": 10**18})
    result = preflight_quantization(run)
    assert result.ready is False
    assert "insufficient_disk_space" in result.blockers


def test_quantization_run_is_first_attempt_only_and_verifies_output(tmp_path, monkeypatch):
    run = quantization_run(tmp_path)

    def fake_worker(runtime, payload, timeout, model_root):
        output = Path(payload["output_directory"])
        output.mkdir()
        artifact = output / "selene-q4_k_m.gguf"
        artifact.write_bytes(b"quantized model")
        manifest = DeploymentManifest(
            run_id=payload["run_id"],
            run_sha256=payload["run_sha256"],
            base_model_identity=payload["model_identity"],
            adapter_manifest_sha256=payload["adapter_manifest_sha256"],
            framework_version=payload["framework_version"],
            quantizer_sha256=payload["quantizer_sha256"],
            quantizer_version=payload["quantizer_version"],
            quantization=QuantizationKind(payload["quantization"]),
            terminal_status="succeeded",
            artifacts=[
                DeploymentArtifact(
                    path=artifact.name,
                    size=artifact.stat().st_size,
                    sha256=sha(artifact),
                )
            ],
        )
        (output / "deployment-manifest.json").write_text(manifest.model_dump_json(indent=2))
        return 0, b'{"status":"succeeded"}', b"", False

    monkeypatch.setattr(selene_quantization, "_run_worker", fake_worker)
    result = run_quantization(run)
    assert result.status == "succeeded"
    assert result.deployment_manifest_sha256 == sha(
        run.output_directory / "deployment-manifest.json"
    )
    assert (run.receipt_directory / f"{run.id}.quantization-attempt.json").exists()
    assert (run.receipt_directory / f"{run.id}.quantization-result.json").exists()
    assert "quantization_attempt_already_exists" in preflight_quantization(run).blockers


@pytest.mark.parametrize("adapter_method", ["lora", "qlora"])
def test_quantization_worker_fuses_then_quantizes_and_removes_fp16_intermediate(
    tmp_path, monkeypatch, adapter_method
):
    model = tmp_path / "model"
    adapter = tmp_path / "adapter"
    model.mkdir()
    adapter.mkdir()
    (adapter / "adapter_config.json").write_text("{}")
    (adapter / "adapters.safetensors").write_bytes(b"adapter")
    quantizer = tmp_path / "llama-quantize"
    executable(quantizer)
    output = tmp_path / "output"
    payload = {
        "run_id": "deployment-run",
        "run_sha256": "a" * 64,
        "model_root": str(model),
        "model_identity": "selene@test",
        "adapter_root": str(adapter),
        "adapter_manifest_sha256": "b" * 64,
        "adapter_method": adapter_method,
        "output_directory": str(output),
        "framework": "mlx-lm",
        "framework_version": "0.31.3",
        "quantizer": str(quantizer),
        "quantizer_sha256": sha(quantizer),
        "quantizer_version": "llama.cpp@test",
        "quantization": "q4_k_m",
    }
    calls = []

    def fake_run(command, stdout_path, stderr_path, *, cwd):
        calls.append(command)
        stdout_path.write_text("ok\n")
        stderr_path.write_text("(none)\n")
        if "mlx_lm.fuse" in command:
            fused = cwd / "fused-model"
            fused.mkdir()
            (fused / "model-f16.gguf").write_bytes(b"fp16")
        else:
            Path(command[2]).write_bytes(b"q4 model")
        return 0

    monkeypatch.setattr(selene_quantization_worker, "_run", fake_run)
    selene_quantization_worker._quantize(payload)
    assert "mlx_lm.fuse" in calls[0]
    assert ("--dequantize" in calls[0]) is (adapter_method == "qlora")
    assert calls[1][-1] == "Q4_K_M"
    assert not (output / ".fuse").exists()
    assert (output / "selene-q4_k_m.gguf").read_bytes() == b"q4 model"
    manifest = json.loads((output / "deployment-manifest.json").read_text())
    assert manifest["quantization"] == "q4_k_m"
    assert sum(item["path"].endswith(".gguf") for item in manifest["artifacts"]) == 1
