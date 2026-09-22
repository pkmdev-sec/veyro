"""Pinned, offline Laya typed-evaluator adapter."""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from veyro.evaluators import ChoiceQuestion, NoulQuestion, Question, ScoreQuestion, outcome_labels
from veyro.readout import READOUT_PROTOCOL

MAX_RESPONSE_BYTES = 1024 * 1024
REQUIRED_LAYA_FILES = frozenset(
    {
        "rl_agent_api.py",
        "rl_common.py",
        "typed-decisions/model.safetensors",
        "typed-decisions/rl_agent_config.json",
        "typed-decisions/encoder/config.json",
        "typed-decisions/tokenizer/tokenizer.json",
        "typed-decisions/tokenizer/tokenizer_config.json",
    }
)
_OFFLINE_SANDBOX = "(version 1) (allow default) (deny network-outbound)"
_ISOLATED_BOOTSTRAP = (
    "import runpy,sys; "
    "runtime,launcher,*args=sys.argv[1:]; "
    "sys.path.insert(0,runtime); "
    "sys.argv=[launcher,*args]; "
    "runpy.run_path(launcher,run_name='__main__')"
)


class LayaProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    kind: Literal["laya"] = "laya"
    python: Path
    python_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_root: Path
    runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_layout_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    launcher: Path
    launcher_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_root: Path
    checkpoint: Literal["typed-decisions"] = "typed-decisions"
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    device: Literal["cpu", "mps"] = "cpu"
    files: dict[str, str]

    @field_validator("python", "runtime_root", "launcher", "model_root", mode="before")
    @classmethod
    def absolute_expanded_path(cls, value) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("Laya paths must be absolute or home-relative")
        return path

    @field_validator("files")
    @classmethod
    def valid_files(cls, files: dict[str, str]) -> dict[str, str]:
        for name, digest in files.items():
            path = Path(name)
            if (
                not name
                or path.is_absolute()
                or ".." in path.parts
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise ValueError("Laya files need relative paths and SHA-256 digests")
        return files

    @model_validator(mode="after")
    def complete_runtime_manifest(self) -> Self:
        if set(self.files) != REQUIRED_LAYA_FILES:
            missing = sorted(REQUIRED_LAYA_FILES - set(self.files))
            extra = sorted(set(self.files) - REQUIRED_LAYA_FILES)
            raise ValueError(f"Laya runtime manifest mismatch: missing={missing}, extra={extra}")
        return self


class _VerifiedRuntime(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    python: Path
    runtime_root: Path
    launcher: Path
    model_root: Path


def load_laya_profiles(path: Path | None = None) -> dict[str, LayaProfile]:
    if path is None:
        source = Path(__file__).resolve().parents[2] / "config" / "laya-profiles.json"
        installed = Path(sys.prefix) / "share" / "veyro" / "laya-profiles.json"
        path = source if source.is_file() else installed
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not data:
            raise ValueError("profiles must be a nonempty object")
        profiles = {key: LayaProfile.model_validate(value) for key, value in data.items()}
        if any(key != profile.id for key, profile in profiles.items()):
            raise ValueError("profile keys must match profile IDs")
        return profiles
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Cannot load Laya profiles from {path}: {error}") from error


def laya_model_identity(profile: LayaProfile) -> str:
    digest = profile.files[f"{profile.checkpoint}/model.safetensors"]
    manifest = {
        "identity_version": 1,
        "python_sha256": profile.python_sha256,
        "runtime_sha256": profile.runtime_sha256,
        "runtime_layout_sha256": profile.runtime_layout_sha256,
        "launcher_sha256": profile.launcher_sha256,
        "checkpoint": profile.checkpoint,
        "revision": profile.revision,
        "device": profile.device,
        "files": profile.files,
    }
    manifest_digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return (
        f"convaiinnovations/laya/{profile.checkpoint}"
        f"@revision:{profile.revision}@sha256:{digest}"
        f"@manifest-sha256:{manifest_digest}"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Laya runtime bundle contains a symbolic link: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _immutable_tree_layout_sha256(root: Path) -> str:
    if sys.platform != "darwin" or not hasattr(stat, "UF_IMMUTABLE"):
        raise ValueError("immutable Laya runtime bundles require macOS")
    digest = hashlib.sha256()
    for path in sorted([root, *root.rglob("*")]):
        if path.is_symlink():
            raise ValueError(f"Laya runtime bundle contains a symbolic link: {path}")
        info = path.stat(follow_symlinks=False)
        if info.st_uid != os.getuid() or not info.st_flags & stat.UF_IMMUTABLE:
            raise ValueError(f"Laya runtime entry is not user-owned and immutable: {path}")
        relative = "." if path == root else path.relative_to(root).as_posix()
        kind = "d" if path.is_dir() else "f" if path.is_file() else "x"
        record = (
            f"{kind}\0{relative}\0{info.st_size}\0{stat.S_IMODE(info.st_mode)}".encode()
        )
        digest.update(len(record).to_bytes(4, "big"))
        digest.update(record)
    return digest.hexdigest()


def _verified_file(path: Path, expected: str, *, executable: bool = False) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as error:
        raise ValueError(f"Laya runtime file is unavailable: {path}") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
        or (executable and not os.access(resolved, os.X_OK))
    ):
        raise ValueError(f"Unsafe Laya runtime file: {path}")
    actual = _sha256(resolved)
    if actual != expected:
        raise ValueError(f"Laya SHA-256 mismatch for {path}: expected {expected}, found {actual}")
    return resolved


def _verify_runtime(profile: LayaProfile) -> _VerifiedRuntime:
    python = _verified_file(profile.python, profile.python_sha256, executable=True)
    try:
        runtime_root = profile.runtime_root.resolve(strict=True)
        runtime_info = runtime_root.stat()
    except OSError as error:
        raise ValueError(f"Laya runtime bundle is unavailable: {profile.runtime_root}") from error
    if (
        not runtime_root.is_dir()
        or runtime_info.st_uid != os.getuid()
        or runtime_info.st_mode & 0o022
    ):
        raise ValueError("Unsafe Laya runtime bundle")
    runtime_digest = _tree_sha256(runtime_root)
    if runtime_digest != profile.runtime_sha256:
        raise ValueError(
            "Laya runtime SHA-256 mismatch: "
            f"expected {profile.runtime_sha256}, found {runtime_digest}"
        )
    if profile.runtime_layout_sha256 is not None:
        layout_digest = _immutable_tree_layout_sha256(runtime_root)
        if layout_digest != profile.runtime_layout_sha256:
            raise ValueError(
                "Laya immutable runtime layout mismatch: "
                f"expected {profile.runtime_layout_sha256}, found {layout_digest}"
            )
    launcher = _verified_file(profile.launcher, profile.launcher_sha256, executable=True)
    try:
        root = profile.model_root.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"Laya model root is unavailable: {profile.model_root}") from error
    if not root.is_dir():
        raise ValueError("Laya model root must be a directory")
    for name, expected in profile.files.items():
        _verified_file(root / name, expected)
    return _VerifiedRuntime(
        python=python,
        runtime_root=runtime_root,
        launcher=launcher,
        model_root=root,
    )


def _laya_questions(questions: Mapping[str, Question]) -> dict[str, dict]:
    mapped = {}
    for name, question in questions.items():
        if len(outcome_labels(question)) == 1:
            continue
        if isinstance(question, NoulQuestion):
            mapped[name] = {
                "type": "noul",
                "instructions": question.prompt,
                "criteria": {
                    "false": "The criterion is not fully established.",
                    "true": "The entire criterion is established.",
                },
            }
        elif isinstance(question, ChoiceQuestion):
            mapped[name] = {
                "type": "choice",
                "instructions": question.prompt,
                "criteria": {
                    outcome.label: outcome.description for outcome in question.outcomes
                },
            }
        elif isinstance(question, ScoreQuestion):
            mapped[name] = {
                "type": "score",
                "instructions": question.prompt,
                "criteria": [outcome.description for outcome in question.outcomes],
            }
        else:  # pragma: no cover - the discriminated Question union is exhaustive
            raise AssertionError(f"unsupported evaluator question: {type(question).__name__}")
    return mapped


def _probability(value: object, label: str) -> float:
    if isinstance(value, bool) or type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"Laya returned an invalid probability for {label}")
    number = float(value)
    if not 0 <= number <= 1:
        raise ValueError(f"Laya returned an invalid probability for {label}")
    return number


def _distribution(values: Mapping[str, object], labels: list[str]) -> list[float]:
    if set(values) != set(labels):
        raise ValueError("Laya response outcomes do not match the evaluator question")
    probabilities = [_probability(values[label], label) for label in labels]
    total = math.fsum(probabilities)
    # The bundled Laya API reports four-decimal probabilities. Accept only its
    # bounded rounding error, then restore an exact transport distribution.
    if not math.isclose(total, 1, rel_tol=0, abs_tol=0.01):
        raise ValueError("Laya probabilities must sum to one")
    return [value / total for value in probabilities]


def _normalize_laya_response(
    profile: LayaProfile,
    questions: Mapping[str, Question],
    raw: object,
    *,
    latency_ms: float,
) -> dict:
    if not isinstance(raw, dict) or raw.get("model") != "rl-agent":
        raise ValueError("Laya returned an invalid response envelope")
    expected_path = str(profile.model_root.resolve() / profile.checkpoint)
    if raw.get("checkpoint") != profile.checkpoint or raw.get("checkpoint_path") != expected_path:
        raise ValueError("Laya checkpoint identity mismatch")
    answers = raw.get("answers")
    mapped = _laya_questions(questions)
    if not isinstance(answers, dict) or set(answers) != set(mapped):
        raise ValueError("Laya response question set mismatch")

    predictions = {}
    for name in mapped:
        question = questions[name]
        answer = answers[name]
        if not isinstance(answer, dict) or answer.get("type") != question.type:
            raise ValueError(f"Laya returned an invalid answer for {name}")
        labels = outcome_labels(question)
        if isinstance(question, NoulQuestion):
            yes = _probability(answer.get("noul"), "yes")
            probabilities = [round(1 - yes, 12), yes]
        elif isinstance(question, ChoiceQuestion):
            values = answer.get("probabilities")
            if not isinstance(values, dict) or answer.get("choice") not in labels:
                raise ValueError(f"Laya returned an invalid choice for {name}")
            probabilities = _distribution(values, labels)
        else:
            values = answer.get("probabilities")
            indices = [str(index) for index in range(len(labels))]
            if not isinstance(values, dict):
                raise ValueError(f"Laya returned invalid score probabilities for {name}")
            probabilities = _distribution(values, indices)
        predictions[name] = {"outcomes": labels, "probabilities": probabilities}

    usage = raw.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("Laya response is missing usage")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if (
        type(input_tokens) is not int
        or input_tokens < 0
        or type(output_tokens) is not int
        or output_tokens != 0
    ):
        raise ValueError("Laya returned invalid usage")
    return {
        "protocol": READOUT_PROTOCOL,
        "predictions": predictions,
        "metrics": {
            "backend": "laya",
            "probability_source": "laya_rounded_4dp_renormalized",
            "latency_ms": latency_ms,
            "input_tokens": input_tokens,
            "generated_tokens": output_tokens,
        },
    }


def _offline_command(command: list[str]) -> list[str]:
    if sys.platform != "darwin":
        raise ValueError("offline Laya evaluation requires a supported OS network sandbox")
    if _network_outbound_denied():
        return command
    sandbox = shutil.which("sandbox-exec")
    if sandbox is None:
        raise ValueError("offline Laya evaluation requires sandbox-exec on macOS")
    return [sandbox, "-p", _OFFLINE_SANDBOX, *command]


def _network_outbound_denied() -> bool:
    sandbox_check = ctypes.CDLL(None).sandbox_check
    sandbox_check.restype = ctypes.c_int
    return sandbox_check(os.getpid(), b"network-outbound", 0) != 0


def _offline_environment(model_root: Path) -> dict[str, str]:
    allowed = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment.update(
        {
            "LAYA_MODEL_ROOT": str(model_root),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "USE_TF": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        }
    )
    return environment


def evaluate_laya(
    profile: LayaProfile,
    state: object,
    questions: Mapping[str, Question],
    *,
    timeout: float,
) -> dict:
    if not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError("Laya timeout must be finite and at most 120 seconds")
    runtime = _verify_runtime(profile)
    mapped = _laya_questions(questions)
    payload = json.dumps({"state": state, "questions": mapped}, allow_nan=False)
    began = time.monotonic()
    try:
        process = subprocess.run(
            _offline_command(
                [
                    str(runtime.python),
                    "-B",
                    "-I",
                    "-S",
                    "-c",
                    _ISOLATED_BOOTSTRAP,
                    str(runtime.runtime_root),
                    str(runtime.launcher),
                    "--model",
                    profile.checkpoint,
                    "--device",
                    profile.device,
                ]
            ),
            input=payload,
            cwd=runtime.model_root,
            env=_offline_environment(runtime.model_root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("Laya evaluation exceeded its deadline") from error
    if process.returncode != 0:
        raise RuntimeError(f"Laya evaluation failed: {process.stderr[-2000:]}")
    if len(process.stdout.encode()) > MAX_RESPONSE_BYTES:
        raise ValueError("Laya response exceeds 1 MiB")
    try:
        raw = json.loads(process.stdout)
    except ValueError as error:
        raise ValueError("Laya returned invalid JSON") from error
    # Detect replacement or mutation during inference before trusting the result.
    _verify_runtime(profile)
    return _normalize_laya_response(
        profile,
        questions,
        raw,
        latency_ms=(time.monotonic() - began) * 1000,
    )
