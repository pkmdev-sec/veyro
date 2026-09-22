"""Immutable offline deployment quantization for verified MLX Selene adapters."""

from __future__ import annotations

import hashlib
import math
import os
import time
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from veyro.selene_training import (
    _OFFICIAL_BASE_MODEL,
    _OFFICIAL_REPOSITORY,
    AdapterManifest,
    Config,
    TrainableModelManifest,
    _claim_json,
    _existing_parent,
    _json_sha256,
    _load_manifest,
    _run_worker,
    _sha256,
    _verified_file,
    _verify_approvals,
    _verify_model,
    _worker_response,
)

_HASH_PATTERN = r"^[0-9a-f]{64}$"
_ID_PATTERN = r"^[a-z][a-z0-9_.-]{0,127}$"


class QuantizationKind(StrEnum):
    Q4_K_M = "q4_k_m"


class QuantizationRuntime(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-quantization-runtime-v1"] = "selene-quantization-runtime-v1"
    framework: Literal["mlx-lm"] = "mlx-lm"
    framework_version: str = Field(min_length=1, max_length=200)
    python: Path
    python_sha256: str = Field(pattern=_HASH_PATTERN)
    launcher: Path
    launcher_sha256: str = Field(pattern=_HASH_PATTERN)
    package_manifest: Path
    package_manifest_sha256: str = Field(pattern=_HASH_PATTERN)
    quantizer: Path
    quantizer_sha256: str = Field(pattern=_HASH_PATTERN)
    quantizer_version: str = Field(min_length=1, max_length=500)
    supported_quantizations: frozenset[QuantizationKind] = Field(min_length=1)

    @field_validator("python", "launcher", "package_manifest", "quantizer")
    @classmethod
    def absolute_runtime_paths(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("quantization runtime paths must be absolute")
        return path


class QuantizationRun(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-deployment-quantization-v1"] = "selene-deployment-quantization-v1"
    id: str = Field(pattern=_ID_PATTERN)
    created_at: datetime
    model_manifest: Path
    model_manifest_sha256: str = Field(pattern=_HASH_PATTERN)
    adapter_manifest: Path
    adapter_manifest_sha256: str = Field(pattern=_HASH_PATTERN)
    runtime_manifest: Path
    runtime_manifest_sha256: str = Field(pattern=_HASH_PATTERN)
    output_directory: Path
    receipt_directory: Path
    quantization: QuantizationKind
    expected_peak_bytes: int = Field(gt=0)
    safety_reserve_bytes: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0, le=7 * 24 * 60 * 60)

    @field_validator(
        "model_manifest",
        "adapter_manifest",
        "runtime_manifest",
        "output_directory",
        "receipt_directory",
    )
    @classmethod
    def absolute_run_paths(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("quantization run paths must be absolute")
        return path

    @field_validator("timeout_seconds")
    @classmethod
    def finite_timeout(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("quantization timeout must be finite")
        return value

    def sha256(self) -> str:
        return _json_sha256(self.model_dump(mode="json"))


class QuantizationPreflight(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-quantization-preflight-v1"] = "selene-quantization-preflight-v1"
    run_sha256: str = Field(pattern=_HASH_PATTERN)
    model_identity: str
    adapter_manifest_sha256: str = Field(pattern=_HASH_PATTERN)
    free_bytes: int = Field(ge=0)
    required_bytes: int = Field(ge=0)
    ready: bool
    blockers: list[str]


class DeploymentArtifact(Config):
    path: str
    size: int = Field(ge=1)
    sha256: str = Field(pattern=_HASH_PATTERN)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts or value == ".":
            raise ValueError("deployment artifact paths must be output-relative")
        return value


class DeploymentManifest(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-deployment-artifact-v1"] = "selene-deployment-artifact-v1"
    run_id: str = Field(pattern=_ID_PATTERN)
    run_sha256: str = Field(pattern=_HASH_PATTERN)
    base_model_identity: str
    adapter_manifest_sha256: str = Field(pattern=_HASH_PATTERN)
    framework: Literal["mlx-lm"] = "mlx-lm"
    framework_version: str
    quantizer_sha256: str = Field(pattern=_HASH_PATTERN)
    quantizer_version: str
    quantization: QuantizationKind
    terminal_status: Literal["succeeded"]
    artifacts: list[DeploymentArtifact] = Field(min_length=1)


class QuantizationAttempt(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-quantization-attempt-v1"] = "selene-quantization-attempt-v1"
    run_id: str
    run_sha256: str = Field(pattern=_HASH_PATTERN)
    started_at: datetime
    preflight: QuantizationPreflight


class QuantizationResult(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-quantization-result-v1"] = "selene-quantization-result-v1"
    run_id: str
    run_sha256: str = Field(pattern=_HASH_PATTERN)
    status: Literal["succeeded", "failed", "timed_out", "interrupted"]
    started_at: datetime
    finished_at: datetime
    elapsed_seconds: float = Field(ge=0)
    exit_code: int | None
    worker_stdout_sha256: str = Field(pattern=_HASH_PATTERN)
    worker_stderr_sha256: str = Field(pattern=_HASH_PATTERN)
    deployment_manifest_sha256: str | None = Field(default=None, pattern=_HASH_PATTERN)
    error: str | None = None


class _Loaded(Config):
    model: TrainableModelManifest
    adapter: AdapterManifest
    runtime: QuantizationRuntime
    adapter_directory: Path


def preflight_quantization(run: QuantizationRun) -> QuantizationPreflight:
    loaded = _load_inputs(run)
    blockers: list[str] = []
    _verify_model(loaded.model, blockers)
    _verify_approvals(loaded.model, blockers)
    if (
        loaded.model.repository != _OFFICIAL_REPOSITORY
        or loaded.model.base_model != _OFFICIAL_BASE_MODEL
    ):
        blockers.append("unsupported_trainable_checkpoint")
    observed_manifest = _json_sha256([item.model_dump(mode="json") for item in loaded.model.files])
    if observed_manifest != loaded.model.repository_file_manifest_sha256:
        blockers.append("repository_file_manifest_mismatch")
    _verify_runtime(loaded.runtime, blockers)
    _verify_adapter(run, loaded, blockers)
    if run.quantization not in loaded.runtime.supported_quantizations:
        blockers.append("quantization_not_supported")
    if loaded.adapter.framework != "mlx-lm":
        blockers.append("adapter_framework_not_supported")
    if loaded.adapter.base_model_identity != _model_identity(loaded.model):
        blockers.append("adapter_model_identity_mismatch")
    attempt, result = _receipt_paths(run)
    if attempt.exists() or result.exists():
        blockers.append("quantization_attempt_already_exists")
    if run.output_directory.exists():
        blockers.append("output_directory_already_exists")
    if not run.receipt_directory.is_dir():
        blockers.append("receipt_directory_missing")
    try:
        receipt_parent = _existing_parent(run.receipt_directory)
        output_parent = _existing_parent(run.output_directory)
        if (
            receipt_parent.stat().st_uid != os.getuid()
            or output_parent.stat().st_uid != os.getuid()
        ):
            blockers.append("unsafe_output_owner")
        free = min(
            os.statvfs(receipt_parent).f_bavail * os.statvfs(receipt_parent).f_frsize,
            os.statvfs(output_parent).f_bavail * os.statvfs(output_parent).f_frsize,
        )
    except OSError:
        free = 0
        blockers.append("disk_preflight_failed")
    required = run.expected_peak_bytes + run.safety_reserve_bytes
    if free < required:
        blockers.append("insufficient_disk_space")
    return QuantizationPreflight(
        run_sha256=run.sha256(),
        model_identity=_model_identity(loaded.model),
        adapter_manifest_sha256=run.adapter_manifest_sha256,
        free_bytes=free,
        required_bytes=required,
        ready=not blockers,
        blockers=blockers,
    )


def run_quantization(run: QuantizationRun) -> QuantizationResult:
    preflight = preflight_quantization(run)
    if not preflight.ready:
        raise ValueError(f"quantization preflight blocked: {', '.join(preflight.blockers)}")
    loaded = _load_inputs(run)
    attempt_path, result_path = _receipt_paths(run)
    started = datetime.now(UTC)
    began = time.monotonic()
    _claim_json(
        attempt_path,
        QuantizationAttempt(
            run_id=run.id,
            run_sha256=run.sha256(),
            started_at=started,
            preflight=preflight,
        ).model_dump(mode="json"),
    )
    payload = {
        "protocol": run.protocol,
        "run_id": run.id,
        "run_sha256": run.sha256(),
        "model_root": str(loaded.model.root),
        "model_identity": preflight.model_identity,
        "adapter_root": str(loaded.adapter_directory),
        "adapter_manifest_sha256": run.adapter_manifest_sha256,
        "adapter_method": loaded.adapter.method.value,
        "output_directory": str(run.output_directory),
        "framework": loaded.runtime.framework,
        "framework_version": loaded.runtime.framework_version,
        "quantizer": str(loaded.runtime.quantizer),
        "quantizer_sha256": loaded.runtime.quantizer_sha256,
        "quantizer_version": loaded.runtime.quantizer_version,
        "quantization": run.quantization.value,
    }
    stdout = b""
    stderr = b""
    exit_code: int | None = None
    status: Literal["succeeded", "failed", "timed_out", "interrupted"] = "failed"
    deployment_digest = None
    error = None
    try:
        exit_code, stdout, stderr, timed_out = _run_worker(
            loaded.runtime, payload, run.timeout_seconds, loaded.model.root
        )
        if timed_out:
            status = "timed_out"
            error = "quantization worker exceeded its deadline"
        elif exit_code != 0:
            error = "quantization worker exited unsuccessfully"
        else:
            _worker_response(stdout)
            manifest_path = run.output_directory / "deployment-manifest.json"
            manifest = DeploymentManifest.model_validate_json(manifest_path.read_bytes())
            _verify_deployment(run, preflight, loaded, manifest)
            postflight_blockers: list[str] = []
            _verify_model(loaded.model, postflight_blockers)
            _verify_adapter(run, loaded, postflight_blockers)
            _verify_runtime(loaded.runtime, postflight_blockers)
            if postflight_blockers:
                raise ValueError(
                    "quantization inputs changed during the run: " + ", ".join(postflight_blockers)
                )
            deployment_digest = _sha256(manifest_path)
            status = "succeeded"
    except KeyboardInterrupt:
        status = "interrupted"
        error = "quantization control process was interrupted"
    except (OSError, RuntimeError, TypeError, ValueError) as failure:
        error = str(failure)
    result = QuantizationResult(
        run_id=run.id,
        run_sha256=run.sha256(),
        status=status,
        started_at=started,
        finished_at=datetime.now(UTC),
        elapsed_seconds=time.monotonic() - began,
        exit_code=exit_code,
        worker_stdout_sha256=hashlib.sha256(stdout).hexdigest(),
        worker_stderr_sha256=hashlib.sha256(stderr).hexdigest(),
        deployment_manifest_sha256=deployment_digest,
        error=error,
    )
    _claim_json(result_path, result.model_dump(mode="json"))
    return result


def _load_inputs(run: QuantizationRun) -> _Loaded:
    model = _load_manifest(
        run.model_manifest,
        run.model_manifest_sha256,
        TrainableModelManifest,
        "model manifest",
    )
    adapter = _load_manifest(
        run.adapter_manifest,
        run.adapter_manifest_sha256,
        AdapterManifest,
        "adapter manifest",
    )
    runtime = _load_manifest(
        run.runtime_manifest,
        run.runtime_manifest_sha256,
        QuantizationRuntime,
        "quantization runtime manifest",
    )
    adapter_directory = _adapter_directory(run.adapter_manifest.parent, adapter)
    return _Loaded(
        model=model,
        adapter=adapter,
        runtime=runtime,
        adapter_directory=adapter_directory,
    )


def _adapter_directory(root: Path, adapter: AdapterManifest) -> Path:
    candidates = {
        Path(item.path).parent
        for item in adapter.artifacts
        if Path(item.path).name == "adapter_config.json"
    } & {
        Path(item.path).parent
        for item in adapter.artifacts
        if Path(item.path).name in {"adapters.safetensors", "adapter.safetensors"}
    }
    if len(candidates) != 1:
        raise ValueError("adapter manifest does not identify one complete MLX adapter")
    directory = (root / candidates.pop()).resolve(strict=True)
    if not directory.is_relative_to(root.resolve(strict=True)):
        raise ValueError("adapter directory escapes its manifest root")
    return directory


def _verify_runtime(runtime: QuantizationRuntime, blockers: list[str]) -> None:
    for name, path, digest, executable in (
        ("python", runtime.python, runtime.python_sha256, True),
        ("launcher", runtime.launcher, runtime.launcher_sha256, True),
        ("package_manifest", runtime.package_manifest, runtime.package_manifest_sha256, False),
        ("quantizer", runtime.quantizer, runtime.quantizer_sha256, True),
    ):
        try:
            _verified_file(path, digest, executable=executable)
        except (OSError, ValueError):
            blockers.append(f"quantization_runtime_invalid:{name}")


def _verify_adapter(
    run: QuantizationRun,
    loaded: _Loaded,
    blockers: list[str],
) -> None:
    root = run.adapter_manifest.parent.resolve(strict=True)
    for artifact in loaded.adapter.artifacts:
        try:
            path = (root / artifact.path).resolve(strict=True)
            if not path.is_relative_to(root):
                raise ValueError("adapter artifact escapes manifest root")
            _verified_file(path, artifact.sha256, size=artifact.size)
        except (OSError, ValueError):
            blockers.append(f"adapter_artifact_invalid:{artifact.path}")


def _verify_deployment(
    run: QuantizationRun,
    preflight: QuantizationPreflight,
    loaded: _Loaded,
    manifest: DeploymentManifest,
) -> None:
    if (
        manifest.run_id != run.id
        or manifest.run_sha256 != run.sha256()
        or manifest.base_model_identity != preflight.model_identity
        or manifest.adapter_manifest_sha256 != run.adapter_manifest_sha256
        or manifest.framework_version != loaded.runtime.framework_version
        or manifest.quantizer_sha256 != loaded.runtime.quantizer_sha256
        or manifest.quantizer_version != loaded.runtime.quantizer_version
        or manifest.quantization is not run.quantization
    ):
        raise ValueError("deployment manifest identity mismatch")
    root = run.output_directory.resolve(strict=True)
    quantized_models = 0
    for artifact in manifest.artifacts:
        path = (root / artifact.path).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError("deployment artifact escapes output directory")
        _verified_file(path, artifact.sha256, size=artifact.size)
        if path.suffix == ".gguf":
            quantized_models += 1
    if quantized_models != 1:
        raise ValueError("deployment manifest requires exactly one GGUF model")


def _model_identity(model: TrainableModelManifest) -> str:
    return f"{model.repository}@{model.revision}"


def _receipt_paths(run: QuantizationRun) -> tuple[Path, Path]:
    return (
        run.receipt_directory / f"{run.id}.quantization-attempt.json",
        run.receipt_directory / f"{run.id}.quantization-result.json",
    )
