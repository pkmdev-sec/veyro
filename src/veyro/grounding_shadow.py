"""Pinned, offline Selene grounding inference for non-authoritative shadow use."""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from veyro.grounding import (
    GroundingAssessment,
    GroundingCase,
    GroundingDecision,
    GroundingResponse,
    render_grounding_prompt,
    validate_grounding,
)
from veyro.selene_calibration import CalibrationArtifact, calibrated_probability

_HASH_PATTERN = r"^[0-9a-f]{64}$"
_MAX_RESPONSE_BYTES = 1024 * 1024
_OFFLINE_SANDBOX = "(version 1) (allow default) (deny network-outbound)"


class Config(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
        validate_default=True,
    )


class ShadowFile(Config):
    root: Literal["base_model", "adapter"]
    path: str
    size: int = Field(ge=1)
    sha256: str = Field(pattern=_HASH_PATTERN)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts or value == ".":
            raise ValueError("shadow artifact paths must be root-relative")
        return value


class GroundingShadowProfile(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-grounding-shadow-profile-v1"] = "selene-grounding-shadow-profile-v1"
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    model_identity: str = Field(min_length=1, max_length=2000)
    python: Path
    python_sha256: str = Field(pattern=_HASH_PATTERN)
    launcher: Path
    launcher_sha256: str = Field(pattern=_HASH_PATTERN)
    package_manifest: Path
    package_manifest_sha256: str = Field(pattern=_HASH_PATTERN)
    base_model_root: Path
    adapter_root: Path
    files: list[ShadowFile] = Field(min_length=2, max_length=10_000)
    calibration: Path | None = None
    calibration_sha256: str | None = Field(default=None, pattern=_HASH_PATTERN)
    max_output_tokens: int = Field(default=2048, ge=64, le=8192)
    timeout_seconds: float = Field(default=120, gt=0, le=300)

    @field_validator(
        "python",
        "launcher",
        "package_manifest",
        "base_model_root",
        "adapter_root",
        "calibration",
    )
    @classmethod
    def absolute_paths(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("shadow profile paths must be absolute")
        return path

    @field_validator("timeout_seconds")
    @classmethod
    def finite_timeout(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("shadow timeout must be finite")
        return value

    @model_validator(mode="after")
    def complete_unique_files(self) -> Self:
        identities = [(item.root, item.path) for item in self.files]
        if len(identities) != len(set(identities)):
            raise ValueError("shadow artifact paths must be unique within each root")
        if {item.root for item in self.files} != {"base_model", "adapter"}:
            raise ValueError("shadow profile requires base-model and adapter artifacts")
        if (self.calibration is None) != (self.calibration_sha256 is None):
            raise ValueError("shadow calibration path and SHA-256 must be paired")
        return self


class _WorkerUsage(Config):
    prompt_tokens: int = Field(ge=1)
    completion_tokens: int = Field(ge=1)


class _WorkerResponse(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-grounding-worker-v1"] = "selene-grounding-worker-v1"
    model_identity: str
    content: str = Field(min_length=1, max_length=512_000)
    decision_logits: dict[Literal["yes", "no"], float]
    usage: _WorkerUsage

    @field_validator("decision_logits")
    @classmethod
    def exact_finite_logits(cls, value: dict[str, float]) -> dict[str, float]:
        if set(value) != {"yes", "no"} or any(
            isinstance(item, bool) or not math.isfinite(item) for item in value.values()
        ):
            raise ValueError("worker must return exact finite Yes and No logits")
        return value


class GroundingShadowReceipt(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["selene-grounding-shadow-v1"] = "selene-grounding-shadow-v1"
    authority: Literal["shadow_only"] = "shadow_only"
    profile_id: str
    model_identity: str
    case_id: str
    case_sha256: str = Field(pattern=_HASH_PATTERN)
    prompt_sha256: str = Field(pattern=_HASH_PATTERN)
    raw_conditional_yes_probability: float = Field(ge=0, le=1)
    calibrated_yes_probability: float | None = Field(default=None, ge=0, le=1)
    calibration_sha256: str | None = Field(default=None, pattern=_HASH_PATTERN)
    reported_result: Literal["yes", "no"]
    would_authorize_completion: bool
    assessment: GroundingAssessment
    grounding_decision: GroundingDecision
    prompt_tokens: int = Field(ge=1)
    completion_tokens: int = Field(ge=1)
    latency_ms: float = Field(ge=0)


def evaluate_grounding_shadow(
    profile: GroundingShadowProfile,
    case: GroundingCase,
) -> GroundingShadowReceipt:
    """Run one offline assessment while keeping its decision non-authoritative."""
    python, launcher = _verify_profile(profile)
    _reject_retired_case(case)
    prompt = render_grounding_prompt(case)
    payload = {
        "schema_version": 1,
        "protocol": "selene-grounding-worker-v1",
        "model_identity": profile.model_identity,
        "base_model_root": str(profile.base_model_root),
        "adapter_root": str(profile.adapter_root),
        "prompt": prompt,
        "max_output_tokens": profile.max_output_tokens,
    }
    started = time.monotonic()
    try:
        process = subprocess.run(
            _offline_command([str(python), "-B", "-I", str(launcher)]),
            input=json.dumps(payload, allow_nan=False),
            capture_output=True,
            text=True,
            timeout=profile.timeout_seconds,
            check=False,
            start_new_session=True,
            env=_offline_environment(profile),
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("Selene grounding shadow exceeded its deadline") from error
    latency_ms = (time.monotonic() - started) * 1000
    if process.returncode != 0:
        raise RuntimeError(f"Selene grounding shadow failed: {process.stderr[-2000:]}")
    if len(process.stdout.encode()) > _MAX_RESPONSE_BYTES:
        raise ValueError("Selene grounding shadow response exceeds 1 MiB")
    worker = _WorkerResponse.model_validate_json(process.stdout)
    if worker.model_identity != profile.model_identity:
        raise ValueError("Selene grounding shadow model identity mismatch")
    response = GroundingResponse.model_validate_json(worker.content)
    yes_logit = worker.decision_logits["yes"]
    no_logit = worker.decision_logits["no"]
    probability = _conditional_yes_probability(yes_logit, no_logit)
    calibration = _load_calibration(profile)
    calibrated = (
        calibrated_probability(calibration, yes_logit, no_logit)
        if calibration is not None
        else None
    )
    forced_result = "yes" if yes_logit > no_logit else "no" if no_logit > yes_logit else None
    if forced_result is None or forced_result != response.result:
        raise ValueError("generated result does not match exact forced-choice logits")
    assessment = GroundingAssessment(
        **response.model_dump(mode="python"),
        model_identity=worker.model_identity,
        generated_at=datetime.now(UTC),
        raw_response_sha256=hashlib.sha256(worker.content.encode()).hexdigest(),
        conditional_yes_probability=probability,
    )
    decision = validate_grounding(case, assessment)
    _verify_profile(profile)
    return GroundingShadowReceipt(
        profile_id=profile.id,
        model_identity=profile.model_identity,
        case_id=case.id,
        case_sha256=case.sha256(),
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        raw_conditional_yes_probability=probability,
        calibrated_yes_probability=calibrated,
        calibration_sha256=profile.calibration_sha256,
        reported_result=response.result,
        would_authorize_completion=decision.authorizes_completion,
        assessment=assessment,
        grounding_decision=decision,
        prompt_tokens=worker.usage.prompt_tokens,
        completion_tokens=worker.usage.completion_tokens,
        latency_ms=latency_ms,
    )


def _reject_retired_case(case: GroundingCase) -> None:
    if case.origin_study_id == "selene-holdout-v1" or any(
        item.source is not None and item.source.path.startswith(".audit/selene-holdout-v1")
        for item in case.evidence
    ):
        raise ValueError("retired Selene holdout cases cannot be rerun in shadow mode")


def _verify_profile(profile: GroundingShadowProfile) -> tuple[Path, Path]:
    python = _verified_file(
        profile.python,
        profile.python_sha256,
        executable=True,
        allow_symlink=True,
        allow_root_owner=True,
    )
    launcher = _verified_file(profile.launcher, profile.launcher_sha256, executable=True)
    _verified_file(profile.package_manifest, profile.package_manifest_sha256)
    roots = {
        "base_model": _verified_root(profile.base_model_root),
        "adapter": _verified_root(profile.adapter_root),
    }
    for item in profile.files:
        root = roots[item.root]
        path = root / item.path
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError("shadow artifact escapes its pinned root")
        _verified_file(resolved, item.sha256, size=item.size)
    if profile.calibration is not None and profile.calibration_sha256 is not None:
        _verified_file(profile.calibration, profile.calibration_sha256)
    return python, launcher


def _load_calibration(profile: GroundingShadowProfile) -> CalibrationArtifact | None:
    if profile.calibration is None:
        return None
    artifact = CalibrationArtifact.model_validate_json(profile.calibration.read_bytes())
    if artifact.model_identity != profile.model_identity:
        raise ValueError("shadow calibration model identity mismatch")
    return artifact


def _verified_root(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if (
        path.is_symlink()
        or not resolved.is_dir()
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise ValueError(f"unsafe shadow artifact root: {path}")
    return resolved


def _verified_file(
    path: Path,
    expected_sha256: str,
    *,
    size: int | None = None,
    executable: bool = False,
    allow_symlink: bool = False,
    allow_root_owner: bool = False,
) -> Path:
    is_symlink = path.is_symlink()
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    allowed_owners = {os.getuid(), 0} if allow_root_owner else {os.getuid()}
    if (
        (is_symlink and not allow_symlink)
        or (is_symlink and path.lstat().st_uid not in allowed_owners)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid not in allowed_owners
        or info.st_mode & 0o022
        or (executable and not os.access(resolved, os.X_OK))
    ):
        raise ValueError(f"unsafe shadow runtime file: {path}")
    if size is not None and info.st_size != size:
        raise ValueError(f"shadow file size mismatch: {path}")
    if _sha256(resolved) != expected_sha256:
        raise ValueError(f"shadow file SHA-256 mismatch: {path}")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _conditional_yes_probability(yes_logit: float, no_logit: float) -> float:
    maximum = max(yes_logit, no_logit)
    yes = math.exp(yes_logit - maximum)
    no = math.exp(no_logit - maximum)
    return yes / (yes + no)


def _offline_command(command: list[str]) -> list[str]:
    if sys.platform != "darwin":
        raise ValueError("offline shadow inference requires a supported OS network sandbox")
    if _network_outbound_denied():
        return command
    sandbox = shutil.which("sandbox-exec")
    if sandbox is None:
        raise ValueError("offline shadow inference requires sandbox-exec on macOS")
    return [sandbox, "-p", _OFFLINE_SANDBOX, *command]


def _network_outbound_denied() -> bool:
    sandbox_check = ctypes.CDLL(None).sandbox_check
    sandbox_check.restype = ctypes.c_int
    return sandbox_check(os.getpid(), b"network-outbound", 0) != 0


def _offline_environment(profile: GroundingShadowProfile) -> dict[str, str]:
    allowed = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "SELENE_BASE_MODEL_ROOT": str(profile.base_model_root),
            "SELENE_ADAPTER_ROOT": str(profile.adapter_root),
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        }
    )
    return environment
