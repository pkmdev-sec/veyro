from __future__ import annotations

import hashlib
import json
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from veyro.grounding_data import DatasetRole
from veyro.selene_training import (
    ApprovalStatus,
    LicenseApproval,
    ModelFile,
    TrainableModelManifest,
    TrainingHyperparameters,
    TrainingMethod,
    TrainingRun,
    TrainingRuntime,
    TrainingStage,
    preflight_training,
    run_training,
)


def sha(path_or_bytes: Path | bytes | str) -> str:
    if isinstance(path_or_bytes, Path):
        data = path_or_bytes.read_bytes()
    elif isinstance(path_or_bytes, str):
        data = path_or_bytes.encode()
    else:
        data = path_or_bytes
    return hashlib.sha256(data).hexdigest()


def manifest_sha(files: list[ModelFile]) -> str:
    payload = [item.model_dump(mode="json") for item in files]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_json(path: Path, value) -> str:
    path.write_text(value.model_dump_json(indent=2))
    return sha(path)


def license_approval(
    root: Path,
    subject: str,
    name: str,
    *,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
) -> LicenseApproval:
    path = root / f"{name}-license.txt"
    path.write_text(f"Licence terms for {subject}\n")
    return LicenseApproval(
        subject=subject,
        license_id=f"{name}-license",
        license_text_path=path,
        license_text_sha256=sha(path),
        source_url=f"https://example.test/{name}-license",
        reviewed_by="independent-licence-reviewer",
        reviewed_at=datetime(2026, 9, 22, tzinfo=UTC),
        status=status,
        approved_uses=frozenset({"fine_tune", "quantize", "redistribute", "deploy"}),
    )


def fake_launcher(path: Path, *, succeeds: bool = True) -> None:
    if succeeds:
        path.write_text(
            """#!/usr/bin/env python3
import hashlib, json
from pathlib import Path
payload = json.load(__import__('sys').stdin)
out = Path(payload['output_directory'])
out.mkdir()
adapter = out / 'adapter.bin'
adapter.write_bytes(b'trained-adapter')
manifest = {
    'schema_version': 1,
    'protocol': 'selene-adapter-artifact-v1',
    'run_id': payload['run_id'],
    'run_sha256': payload['run_sha256'],
    'base_model_identity': payload['model_identity'],
    'framework': payload['framework'],
    'framework_version': payload['framework_version'],
    'stage': payload['hyperparameters']['stage'],
    'method': payload['hyperparameters']['method'],
    'terminal_status': 'succeeded',
    'artifacts': [{
        'path': adapter.name,
        'size': adapter.stat().st_size,
        'sha256': hashlib.sha256(adapter.read_bytes()).hexdigest(),
    }],
}
(out / 'adapter-manifest.json').write_text(json.dumps(manifest))
print(json.dumps({'status': 'succeeded'}))
"""
        )
    else:
        path.write_text(
            """#!/usr/bin/env python3
import sys
print('training failed', file=sys.stderr)
raise SystemExit(3)
"""
        )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def fixture_run(
    tmp_path: Path,
    *,
    succeeds: bool = True,
    approval_status: ApprovalStatus = ApprovalStatus.APPROVED,
    expected_peak_bytes: int = 1,
) -> TrainingRun:
    model_root = tmp_path / "model"
    model_root.mkdir()
    definitions = [
        ("model.safetensors", b"weights", "weight"),
        ("config.json", b"{}", "config"),
        ("tokenizer.json", b"{}", "tokenizer"),
        ("model.safetensors.index.json", b"{}", "metadata"),
    ]
    files = []
    for name, content, kind in definitions:
        path = model_root / name
        path.write_bytes(content)
        files.append(ModelFile(path=name, size=len(content), sha256=sha(content), kind=kind))
    repository = "AtlaAI/Selene-1-Mini-Llama-3.1-8B"
    base_model = "meta-llama/Llama-3.1-8B-Instruct"
    model = TrainableModelManifest(
        repository=repository,
        revision="a" * 40,
        base_model=base_model,
        root=model_root,
        repository_file_manifest_sha256=manifest_sha(files),
        files=files,
        approvals=[
            license_approval(tmp_path, repository, "selene", status=approval_status),
            license_approval(tmp_path, base_model, "llama"),
        ],
    )
    model_path = tmp_path / "model-manifest.json"
    model_digest = write_json(model_path, model)

    launcher = tmp_path / "training-worker.py"
    fake_launcher(launcher, succeeds=succeeds)
    package_manifest = tmp_path / "packages.txt"
    package_manifest.write_text("framework==1.0\n")
    python = Path(sys.executable).resolve()
    runtime = TrainingRuntime(
        framework="test-framework",
        framework_version="1.0",
        python=python,
        python_sha256=sha(python),
        launcher=launcher,
        launcher_sha256=sha(launcher),
        package_manifest=package_manifest,
        package_manifest_sha256=sha(package_manifest),
        supported_methods=frozenset({TrainingMethod.LORA, TrainingMethod.QLORA}),
        supported_stages=frozenset({TrainingStage.SUPERVISED, TrainingStage.PREFERENCE}),
    )
    runtime_path = tmp_path / "runtime-manifest.json"
    runtime_digest = write_json(runtime_path, runtime)

    dataset_path = tmp_path / "dataset-manifest.json"
    dataset_path.write_text('{"fixture": true}\n')
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    output_parent = tmp_path / "outputs"
    output_parent.mkdir()
    return TrainingRun(
        id="training-attempt",
        created_at=datetime(2026, 9, 22, tzinfo=UTC),
        model_manifest=model_path,
        model_manifest_sha256=model_digest,
        runtime_manifest=runtime_path,
        runtime_manifest_sha256=runtime_digest,
        dataset_manifest=dataset_path,
        dataset_manifest_sha256=sha(dataset_path),
        output_directory=output_parent / "adapter",
        receipt_directory=receipts,
        expected_peak_bytes=expected_peak_bytes,
        safety_reserve_bytes=1,
        timeout_seconds=30,
        hyperparameters=TrainingHyperparameters(
            stage=TrainingStage.SUPERVISED,
            method=TrainingMethod.LORA,
            seed=0,
            epochs=1,
            learning_rate=0.0001,
            max_sequence_length=1024,
            batch_size=1,
            gradient_accumulation_steps=1,
            lora_rank=8,
            lora_alpha=16,
            lora_layers=16,
            lora_dropout=0.05,
            target_modules=["q_proj", "v_proj"],
        ),
    )


class FakeDataset:
    def __init__(self, digest: str):
        self.manifest_sha256 = digest
        self.case_counts = {
            "training": 2,
            "development": 1,
            "calibration": 1,
            "qualification": 1,
        }
        self.cases = MappingProxyType({role: () for role in DatasetRole})
        self.labels = MappingProxyType({role: MappingProxyType({}) for role in DatasetRole})


def patch_dataset(monkeypatch, run: TrainingRun) -> None:
    import veyro.selene_training as training

    dataset = FakeDataset(run.dataset_manifest_sha256)
    monkeypatch.setattr(training, "validate_dataset_manifest", lambda _path: dataset)
    monkeypatch.setattr(
        training,
        "export_training_records",
        lambda _dataset, role, preference=False: [
            {
                "id": f"{role.value}-case",
                "prompt": "evidence prompt",
                "chosen": '{"result":"yes"}',
                **({"rejected": '{"result":"no"}'} if preference else {}),
            }
        ],
    )


def test_preflight_verifies_weights_runtime_licenses_dataset_and_disk(tmp_path, monkeypatch):
    run = fixture_run(tmp_path)
    patch_dataset(monkeypatch, run)
    report = preflight_training(run)
    assert report.ready is True
    assert report.blockers == []
    assert report.model_identity.endswith("@" + "a" * 40)
    assert report.dataset_case_counts["qualification"] == 1
    assert report.free_bytes >= report.required_bytes


def test_preflight_blocks_unapproved_license_without_starting(tmp_path, monkeypatch):
    run = fixture_run(tmp_path, approval_status=ApprovalStatus.PENDING)
    patch_dataset(monkeypatch, run)
    report = preflight_training(run)
    assert report.ready is False
    assert any(item.startswith("license_not_approved") for item in report.blockers)
    assert not list(run.receipt_directory.iterdir())
    assert not run.output_directory.exists()


def test_preflight_blocks_insufficient_disk_and_existing_output(tmp_path, monkeypatch):
    run = fixture_run(tmp_path, expected_peak_bytes=10_000)
    patch_dataset(monkeypatch, run)
    import veyro.selene_training as training

    monkeypatch.setattr(
        training.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=99, free=1),
    )
    run.output_directory.mkdir()
    report = preflight_training(run)
    assert report.ready is False
    assert "insufficient_disk_space" in report.blockers
    assert "output_directory_already_exists" in report.blockers


def test_successful_run_claims_first_attempt_and_verifies_adapter(tmp_path, monkeypatch):
    run = fixture_run(tmp_path)
    patch_dataset(monkeypatch, run)
    import veyro.selene_training as training

    monkeypatch.setattr(training, "_offline_command", lambda command: command)
    result = run_training(run)
    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert result.adapter_manifest_sha256 is not None
    assert (run.output_directory / "adapter.bin").read_bytes() == b"trained-adapter"
    attempt = run.receipt_directory / f"{run.id}.attempt.json"
    terminal = run.receipt_directory / f"{run.id}.result.json"
    assert attempt.is_file()
    assert terminal.is_file()
    assert json.loads(attempt.read_text())["preflight"]["ready"] is True

    with pytest.raises(ValueError, match="preflight blocked"):
        run_training(run)


def test_failed_worker_produces_terminal_failure_and_cannot_retry(tmp_path, monkeypatch):
    run = fixture_run(tmp_path, succeeds=False)
    patch_dataset(monkeypatch, run)
    import veyro.selene_training as training

    monkeypatch.setattr(training, "_offline_command", lambda command: command)
    result = run_training(run)
    assert result.status == "failed"
    assert result.exit_code == 3
    assert result.error == "training worker exited unsuccessfully"
    terminal = json.loads((run.receipt_directory / f"{run.id}.result.json").read_text())
    assert terminal["status"] == "failed"
    assert terminal["worker_stderr_sha256"] == sha(b"training failed\n")
    with pytest.raises(ValueError, match="preflight blocked"):
        run_training(run)


def test_manifest_digest_drift_blocks_before_claim(tmp_path, monkeypatch):
    run = fixture_run(tmp_path)
    patch_dataset(monkeypatch, run)
    run.model_manifest.write_text(run.model_manifest.read_text() + "\n")
    with pytest.raises(ValueError, match="model manifest SHA-256 mismatch"):
        preflight_training(run)
    assert not list(run.receipt_directory.iterdir())


def test_runtime_rejects_unsupported_method_or_stage(tmp_path, monkeypatch):
    run = fixture_run(tmp_path)
    patch_dataset(monkeypatch, run)
    payload = json.loads(run.runtime_manifest.read_text())
    payload["supported_methods"] = ["qlora"]
    payload["supported_stages"] = ["preference"]
    run.runtime_manifest.write_text(json.dumps(payload))
    changed = run.model_copy(update={"runtime_manifest_sha256": sha(run.runtime_manifest)})
    report = preflight_training(changed)
    assert report.ready is False
    assert "training_method_not_supported" in report.blockers
    assert "training_stage_not_supported" in report.blockers


def test_training_run_requires_absolute_paths_and_finite_timeout(tmp_path):
    run = fixture_run(tmp_path)
    payload = run.model_dump(mode="json")
    payload["output_directory"] = "relative-output"
    with pytest.raises(ValueError, match="must be absolute"):
        TrainingRun.model_validate_json(json.dumps(payload))

    payload = run.model_dump(mode="json")
    payload["timeout_seconds"] = "Infinity"
    with pytest.raises(ValueError):
        TrainingRun.model_validate_json(json.dumps(payload))
