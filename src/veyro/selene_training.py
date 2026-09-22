"""Pinned, offline Selene adapter-training control and durable receipts."""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from veyro.grounding_data import (
    DatasetRole,
    export_training_records,
    validate_dataset_manifest,
)

TRAINING_PROTOCOL = "selene-adapter-training-v1"
_HASH_PATTERN = r"^[0-9a-f]{64}$"
_ID_PATTERN = r"^[a-z][a-z0-9_.-]{0,127}$"
_REQUIRED_USES = frozenset({"fine_tune", "quantize", "redistribute", "deploy"})
_MAX_WORKER_RESPONSE_BYTES = 1024 * 1024
_OFFICIAL_REPOSITORY = "AtlaAI/Selene-1-Mini-Llama-3.1-8B"
_OFFICIAL_BASE_MODEL = "meta-llama/Llama-3.1-8B-Instruct"
_OFFLINE_SANDBOX = "(version 1) (allow default) (deny network-outbound)"


class Config(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
        validate_default=True,
    )


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TrainingMethod(StrEnum):
    LORA = "lora"
    QLORA = "qlora"


class TrainingStage(StrEnum):
    SUPERVISED = "supervised"
    PREFERENCE = "preference"


class ModelFile(Config):
    path: str
    size: int = Field(ge=1)
    sha256: str = Field(pattern=_HASH_PATTERN)
    kind: Literal["weight", "config", "tokenizer", "metadata"]

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts or value == ".":
            raise ValueError("model file paths must be root-relative")
        return value


class LicenseApproval(Config):
    subject: str = Field(min_length=1, max_length=1000)
    license_id: str = Field(min_length=1, max_length=200)
    license_text_path: Path
    license_text_sha256: str = Field(pattern=_HASH_PATTERN)
    source_url: str = Field(min_length=1, max_length=2000)
    reviewed_by: str = Field(min_length=1, max_length=500)
    reviewed_at: datetime
    status: ApprovalStatus
    approved_uses: frozenset[Literal["fine_tune", "quantize", "redistribute", "deploy"]]

    @field_validator("license_text_path")
    @classmethod
    def absolute_license_path(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("licence text path must be absolute")
        return path


class TrainableModelManifest(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-trainable-model-v1"] = "selene-trainable-model-v1"
    repository: str = Field(min_length=1, max_length=1000)
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    base_model: str = Field(min_length=1, max_length=1000)
    root: Path
    repository_file_manifest_sha256: str = Field(pattern=_HASH_PATTERN)
    files: list[ModelFile] = Field(min_length=4, max_length=10_000)
    approvals: list[LicenseApproval] = Field(default_factory=list, max_length=16)

    @field_validator("root")
    @classmethod
    def absolute_model_root(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("trainable model root must be absolute")
        return path

    @model_validator(mode="after")
    def complete_unique_manifest(self) -> Self:
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("trainable model file paths must be unique")
        kinds = {item.kind for item in self.files}
        if not {"weight", "config", "tokenizer"} <= kinds:
            raise ValueError("trainable model needs weights, config, and tokenizer files")
        return self

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)


class TrainingRuntime(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-training-runtime-v1"] = "selene-training-runtime-v1"
    framework: str = Field(min_length=1, max_length=200)
    framework_version: str = Field(min_length=1, max_length=200)
    python: Path
    python_sha256: str = Field(pattern=_HASH_PATTERN)
    launcher: Path
    launcher_sha256: str = Field(pattern=_HASH_PATTERN)
    package_manifest: Path
    package_manifest_sha256: str = Field(pattern=_HASH_PATTERN)
    supported_methods: frozenset[TrainingMethod] = Field(min_length=1)
    supported_stages: frozenset[TrainingStage] = Field(min_length=1)

    @field_validator("python", "launcher", "package_manifest")
    @classmethod
    def absolute_runtime_paths(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("training runtime paths must be absolute")
        return path


class TrainingHyperparameters(Config):
    stage: TrainingStage
    method: TrainingMethod
    seed: int = Field(ge=0, le=2**32 - 1)
    epochs: float = Field(gt=0, le=100)
    learning_rate: float = Field(gt=0, le=1)
    max_sequence_length: int = Field(ge=128, le=131_072)
    batch_size: int = Field(ge=1, le=1024)
    gradient_accumulation_steps: int = Field(ge=1, le=65_536)
    lora_rank: int = Field(ge=1, le=1024)
    lora_alpha: int = Field(ge=1, le=65_536)
    lora_layers: int = Field(ge=1, le=1024)
    lora_dropout: float = Field(ge=0, lt=1)
    target_modules: list[str] = Field(min_length=1, max_length=256)
    hard_negative_weight: float = Field(default=1.0, ge=1, le=100)
    preference_beta: float = Field(default=0.1, gt=0, le=10)
    save_steps: int = Field(default=100, ge=1)
    logging_steps: int = Field(default=10, ge=1)

    @field_validator("target_modules")
    @classmethod
    def unique_modules(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("LoRA target modules must be unique and nonempty")
        return value


class TrainingRun(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-adapter-training-v1"] = TRAINING_PROTOCOL
    id: str = Field(pattern=_ID_PATTERN)
    created_at: datetime
    model_manifest: Path
    model_manifest_sha256: str = Field(pattern=_HASH_PATTERN)
    runtime_manifest: Path
    runtime_manifest_sha256: str = Field(pattern=_HASH_PATTERN)
    dataset_manifest: Path
    dataset_manifest_sha256: str = Field(pattern=_HASH_PATTERN)
    output_directory: Path
    receipt_directory: Path
    expected_peak_bytes: int = Field(gt=0)
    safety_reserve_bytes: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0, le=7 * 24 * 60 * 60)
    hyperparameters: TrainingHyperparameters

    @field_validator(
        "model_manifest",
        "runtime_manifest",
        "dataset_manifest",
        "output_directory",
        "receipt_directory",
    )
    @classmethod
    def absolute_run_paths(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("training run paths must be absolute")
        return path

    @field_validator("timeout_seconds")
    @classmethod
    def finite_timeout(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("training timeout must be finite")
        return value

    def sha256(self) -> str:
        return _json_sha256(self.model_dump(mode="json"))


class TrainingPreflight(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-training-preflight-v1"] = "selene-training-preflight-v1"
    run_sha256: str = Field(pattern=_HASH_PATTERN)
    model_identity: str
    model_bytes: int = Field(ge=0)
    dataset_manifest_sha256: str = Field(pattern=_HASH_PATTERN)
    dataset_case_counts: dict[str, int]
    free_bytes: int = Field(ge=0)
    required_bytes: int = Field(ge=0)
    ready: bool
    blockers: list[str]


class AdapterArtifact(Config):
    path: str
    size: int = Field(ge=1)
    sha256: str = Field(pattern=_HASH_PATTERN)

    @field_validator("path")
    @classmethod
    def relative_artifact_path(cls, value: str) -> str:
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts or value == ".":
            raise ValueError("adapter artifact paths must be output-relative")
        return value


class AdapterManifest(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-adapter-artifact-v1"] = "selene-adapter-artifact-v1"
    run_id: str = Field(pattern=_ID_PATTERN)
    run_sha256: str = Field(pattern=_HASH_PATTERN)
    base_model_identity: str
    framework: str
    framework_version: str
    stage: TrainingStage
    method: TrainingMethod
    terminal_status: Literal["succeeded"]
    artifacts: list[AdapterArtifact] = Field(min_length=1)


class TrainingAttempt(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-training-attempt-v1"] = "selene-training-attempt-v1"
    run_id: str
    run_sha256: str = Field(pattern=_HASH_PATTERN)
    started_at: datetime
    preflight: TrainingPreflight


class TrainingResult(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-training-result-v1"] = "selene-training-result-v1"
    run_id: str
    run_sha256: str = Field(pattern=_HASH_PATTERN)
    status: Literal["succeeded", "failed", "timed_out", "interrupted"]
    started_at: datetime
    finished_at: datetime
    elapsed_seconds: float = Field(ge=0)
    exit_code: int | None
    worker_stdout_sha256: str = Field(pattern=_HASH_PATTERN)
    worker_stderr_sha256: str = Field(pattern=_HASH_PATTERN)
    adapter_manifest_sha256: str | None = Field(default=None, pattern=_HASH_PATTERN)
    error: str | None = None


def preflight_training(run: TrainingRun) -> TrainingPreflight:
    model = _load_manifest(
        run.model_manifest,
        run.model_manifest_sha256,
        TrainableModelManifest,
        "model",
    )
    runtime = _load_manifest(
        run.runtime_manifest,
        run.runtime_manifest_sha256,
        TrainingRuntime,
        "runtime",
    )
    dataset_bytes = _bound_bytes(run.dataset_manifest, run.dataset_manifest_sha256, "dataset")
    dataset = validate_dataset_manifest(run.dataset_manifest)
    if dataset.manifest_sha256 != hashlib.sha256(dataset_bytes).hexdigest():
        raise ValueError("dataset manifest changed during validation")

    blockers: list[str] = []
    _verify_model(model, blockers)
    _verify_runtime(runtime, blockers)
    _verify_approvals(model, blockers)
    if model.repository != _OFFICIAL_REPOSITORY or model.base_model != _OFFICIAL_BASE_MODEL:
        blockers.append("unsupported_trainable_checkpoint")
    observed_manifest = _json_sha256([item.model_dump(mode="json") for item in model.files])
    if observed_manifest != model.repository_file_manifest_sha256:
        blockers.append("repository_file_manifest_mismatch")
    if run.hyperparameters.method not in runtime.supported_methods:
        blockers.append("training_method_not_supported")
    if run.hyperparameters.stage not in runtime.supported_stages:
        blockers.append("training_stage_not_supported")
    if run.hyperparameters.stage is TrainingStage.PREFERENCE:
        records = export_training_records(dataset, DatasetRole.TRAINING, preference=True)
        if not records:
            blockers.append("preference_records_missing")
    if run.output_directory.exists():
        blockers.append("output_directory_already_exists")
    attempt_path, result_path = _receipt_paths(run)
    if attempt_path.exists() or result_path.exists():
        blockers.append("run_id_already_attempted")
    if not run.receipt_directory.is_dir():
        blockers.append("receipt_directory_missing")
    if not run.output_directory.parent.is_dir():
        blockers.append("output_parent_missing")

    disk_root = _existing_parent(run.output_directory)
    free_bytes = shutil.disk_usage(disk_root).free
    required_bytes = run.expected_peak_bytes + run.safety_reserve_bytes
    if free_bytes < required_bytes:
        blockers.append("insufficient_disk_space")
    identity = f"{model.repository}@{model.revision}"
    return TrainingPreflight(
        run_sha256=run.sha256(),
        model_identity=identity,
        model_bytes=model.total_bytes,
        dataset_manifest_sha256=run.dataset_manifest_sha256,
        dataset_case_counts=dataset.case_counts,
        free_bytes=free_bytes,
        required_bytes=required_bytes,
        ready=not blockers,
        blockers=blockers,
    )


def run_training(run: TrainingRun) -> TrainingResult:
    preflight = preflight_training(run)
    if not preflight.ready:
        raise ValueError(f"training preflight blocked: {preflight.blockers}")
    model = _load_manifest(
        run.model_manifest,
        run.model_manifest_sha256,
        TrainableModelManifest,
        "model",
    )
    runtime = _load_manifest(
        run.runtime_manifest,
        run.runtime_manifest_sha256,
        TrainingRuntime,
        "runtime",
    )
    dataset = validate_dataset_manifest(run.dataset_manifest)
    attempt_path, result_path = _receipt_paths(run)
    started = datetime.now(UTC)
    attempt = TrainingAttempt(
        run_id=run.id,
        run_sha256=run.sha256(),
        started_at=started,
        preflight=preflight,
    )
    _claim_json(attempt_path, attempt.model_dump(mode="json"))
    began = time.monotonic()
    stdout = b""
    stderr = b""
    exit_code: int | None = None
    status: Literal["succeeded", "failed", "timed_out", "interrupted"] = "failed"
    error: str | None = None
    adapter_digest: str | None = None

    try:
        training_records = export_training_records(
            dataset,
            DatasetRole.TRAINING,
            preference=run.hyperparameters.stage is TrainingStage.PREFERENCE,
        )
        development_records = export_training_records(
            dataset,
            DatasetRole.DEVELOPMENT,
            preference=run.hyperparameters.stage is TrainingStage.PREFERENCE,
        )
        with tempfile.TemporaryDirectory(
            prefix=f"{run.id}-", dir=run.receipt_directory
        ) as temporary:
            temporary_root = Path(temporary)
            training_path = temporary_root / "training.jsonl"
            development_path = temporary_root / "development.jsonl"
            _write_jsonl(training_path, training_records)
            _write_jsonl(development_path, development_records)
            payload = {
                "protocol": TRAINING_PROTOCOL,
                "run_id": run.id,
                "run_sha256": run.sha256(),
                "model_root": str(model.root),
                "model_identity": preflight.model_identity,
                "training_path": str(training_path),
                "development_path": str(development_path),
                "output_directory": str(run.output_directory),
                "framework": runtime.framework,
                "framework_version": runtime.framework_version,
                "hyperparameters": run.hyperparameters.model_dump(mode="json"),
            }
            exit_code, stdout, stderr, timed_out = _run_worker(
                runtime, payload, run.timeout_seconds, model.root
            )
            if timed_out:
                status = "timed_out"
                error = "training worker exceeded its frozen deadline"
            elif exit_code != 0:
                status = "failed"
                error = "training worker exited unsuccessfully"
            else:
                response = _worker_response(stdout)
                if response != {"status": "succeeded"}:
                    raise ValueError("training worker returned an invalid success response")
                adapter_path = run.output_directory / "adapter-manifest.json"
                adapter_bytes = _read_regular_file(adapter_path, maximum=_MAX_WORKER_RESPONSE_BYTES)
                adapter = AdapterManifest.model_validate_json(adapter_bytes)
                _verify_adapter_manifest(run, preflight, runtime, adapter)
                adapter_digest = hashlib.sha256(adapter_bytes).hexdigest()
                postflight_blockers: list[str] = []
                _verify_model(model, postflight_blockers)
                _verify_runtime(runtime, postflight_blockers)
                if postflight_blockers:
                    raise ValueError(
                        f"training inputs changed during the run: {postflight_blockers}"
                    )
                status = "succeeded"
    except (OSError, RuntimeError, ValueError, TypeError) as caught:
        status = "failed"
        error = str(caught)
    except BaseException:
        status = "interrupted"
        error = "training control process was interrupted"
        _finish_result(
            run,
            result_path,
            started,
            began,
            status,
            exit_code,
            stdout,
            stderr,
            adapter_digest,
            error,
        )
        raise

    return _finish_result(
        run,
        result_path,
        started,
        began,
        status,
        exit_code,
        stdout,
        stderr,
        adapter_digest,
        error,
    )


def _finish_result(
    run: TrainingRun,
    path: Path,
    started: datetime,
    began: float,
    status: Literal["succeeded", "failed", "timed_out", "interrupted"],
    exit_code: int | None,
    stdout: bytes,
    stderr: bytes,
    adapter_digest: str | None,
    error: str | None,
) -> TrainingResult:
    result = TrainingResult(
        run_id=run.id,
        run_sha256=run.sha256(),
        status=status,
        started_at=started,
        finished_at=datetime.now(UTC),
        elapsed_seconds=time.monotonic() - began,
        exit_code=exit_code,
        worker_stdout_sha256=hashlib.sha256(stdout).hexdigest(),
        worker_stderr_sha256=hashlib.sha256(stderr).hexdigest(),
        adapter_manifest_sha256=adapter_digest,
        error=error,
    )
    _claim_json(path, result.model_dump(mode="json"))
    return result


def _verify_model(model: TrainableModelManifest, blockers: list[str]) -> None:
    try:
        root = model.root.resolve(strict=True)
        info = root.stat()
        if not root.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("unsafe trainable model root")
    except (OSError, ValueError):
        blockers.append("trainable_model_root_unavailable")
        return
    for item in model.files:
        try:
            _verified_file(root / item.path, item.sha256, size=item.size)
        except (OSError, ValueError):
            blockers.append(f"trainable_model_file_invalid:{item.path}")


def _verify_runtime(runtime: TrainingRuntime, blockers: list[str]) -> None:
    for name, path, digest in (
        ("python", runtime.python, runtime.python_sha256),
        ("launcher", runtime.launcher, runtime.launcher_sha256),
        (
            "package_manifest",
            runtime.package_manifest,
            runtime.package_manifest_sha256,
        ),
    ):
        try:
            _verified_file(path, digest, executable=name in {"python", "launcher"})
        except (OSError, ValueError):
            blockers.append(f"training_runtime_invalid:{name}")


def _verify_approvals(model: TrainableModelManifest, blockers: list[str]) -> None:
    approvals = {item.subject: item for item in model.approvals}
    if len(approvals) != len(model.approvals):
        blockers.append("duplicate_license_approval_subject")
    for subject in (model.repository, model.base_model):
        approval = approvals.get(subject)
        if approval is None:
            blockers.append(f"license_approval_missing:{subject}")
            continue
        if approval.status is not ApprovalStatus.APPROVED:
            blockers.append(f"license_not_approved:{subject}")
        if approval.approved_uses != _REQUIRED_USES:
            blockers.append(f"license_uses_incomplete:{subject}")
        try:
            _verified_file(
                approval.license_text_path,
                approval.license_text_sha256,
            )
        except (OSError, ValueError):
            blockers.append(f"license_text_invalid:{subject}")


def _verify_adapter_manifest(
    run: TrainingRun,
    preflight: TrainingPreflight,
    runtime: TrainingRuntime,
    manifest: AdapterManifest,
) -> None:
    if (
        manifest.run_id != run.id
        or manifest.run_sha256 != run.sha256()
        or manifest.base_model_identity != preflight.model_identity
        or manifest.framework != runtime.framework
        or manifest.framework_version != runtime.framework_version
        or manifest.stage is not run.hyperparameters.stage
        or manifest.method is not run.hyperparameters.method
    ):
        raise ValueError("adapter manifest identity mismatch")
    root = run.output_directory.resolve(strict=True)
    for item in manifest.artifacts:
        path = root / item.path
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError("adapter artifact escapes output directory")
        _verified_file(resolved, item.sha256, size=item.size)


def _run_worker(
    runtime: TrainingRuntime,
    payload: dict,
    timeout: float,
    model_root: Path,
) -> tuple[int, bytes, bytes, bool]:
    command = _offline_command([str(runtime.python), "-B", "-I", str(runtime.launcher)])
    environment = _offline_environment(model_root)
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=stdout_file,
            stderr=stderr_file,
            env=environment,
            start_new_session=True,
        )
        timed_out = False
        try:
            process.communicate(json.dumps(payload, allow_nan=False).encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if timed_out or process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
        stdout = _bounded_output(stdout_file, "stdout")
        stderr = _bounded_output(stderr_file, "stderr")
    return process.returncode, stdout, stderr, timed_out


def _worker_response(stdout: bytes) -> dict:
    try:
        value = json.loads(stdout)
    except ValueError as error:
        raise ValueError("training worker returned invalid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("training worker response must be an object")
    return value


def _offline_command(command: list[str]) -> list[str]:
    if sys_platform() != "darwin":
        raise ValueError("offline training requires a supported OS network sandbox")
    if _network_outbound_denied():
        return command
    sandbox = shutil.which("sandbox-exec")
    if sandbox is None:
        raise ValueError("offline training requires sandbox-exec on macOS")
    return [sandbox, "-p", _OFFLINE_SANDBOX, *command]


def _network_outbound_denied() -> bool:
    sandbox_check = ctypes.CDLL(None).sandbox_check
    sandbox_check.restype = ctypes.c_int
    return sandbox_check(os.getpid(), b"network-outbound", 0) != 0


def sys_platform() -> str:
    import sys

    return sys.platform


def _offline_environment(model_root: Path) -> dict[str, str]:
    allowed = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "SELENE_MODEL_ROOT": str(model_root),
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        }
    )
    return environment


def _load_manifest(path: Path, digest: str, model: type[Config], name: str):
    data = _bound_bytes(path, digest, name)
    return model.model_validate_json(data)


def _bound_bytes(path: Path, digest: str, name: str) -> bytes:
    data = _read_regular_file(path, maximum=16 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"{name} manifest SHA-256 mismatch")
    return data


def _verified_file(
    path: Path,
    expected_sha256: str,
    *,
    size: int | None = None,
    executable: bool = False,
) -> Path:
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
        or (executable and not os.access(resolved, os.X_OK))
    ):
        raise ValueError(f"unsafe training file: {path}")
    if size is not None and info.st_size != size:
        raise ValueError(f"training file size mismatch: {path}")
    if _sha256(resolved) != expected_sha256:
        raise ValueError(f"training file SHA-256 mismatch: {path}")
    return resolved


def _read_regular_file(path: Path, *, maximum: int) -> bytes:
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise ValueError(f"unsafe or oversized file: {path}")
    data = resolved.read_bytes()
    if len(data) != info.st_size:
        raise ValueError(f"file changed while reading: {path}")
    return data


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _claim_json(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _receipt_paths(run: TrainingRun) -> tuple[Path, Path]:
    return (
        run.receipt_directory / f"{run.id}.attempt.json",
        run.receipt_directory / f"{run.id}.result.json",
    )


def _existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        if candidate == candidate.parent:
            raise ValueError("no existing parent for training output")
        candidate = candidate.parent
    return candidate


def _bounded_output(stream, name: str) -> bytes:
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    if size > _MAX_WORKER_RESPONSE_BYTES:
        raise ValueError(f"training worker {name} exceeds 1 MiB")
    stream.seek(0)
    return stream.read()
