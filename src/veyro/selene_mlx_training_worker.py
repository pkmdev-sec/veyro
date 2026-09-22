"""Pinned Apple Silicon MLX-LM worker for supervised Selene LoRA and QLoRA."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_MAX_INPUT_BYTES = 16 * 1024 * 1024
_MAX_RECORD_BYTES = 2 * 1024 * 1024


def main() -> None:
    try:
        payload = _payload()
        records = _read_records(Path(payload["training_path"]))
        development = _read_records(Path(payload["development_path"]))
        _train(payload, records, development)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
        print(f"Selene MLX training worker failed: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps({"status": "succeeded"}))


def _payload() -> dict:
    raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        raise ValueError("training payload exceeds 16 MiB")
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise ValueError("training payload is not valid JSON") from error
    required = {
        "protocol",
        "run_id",
        "run_sha256",
        "model_root",
        "model_identity",
        "training_path",
        "development_path",
        "output_directory",
        "framework",
        "framework_version",
        "hyperparameters",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("training payload fields do not match the worker protocol")
    if value["protocol"] != "selene-adapter-training-v1":
        raise ValueError("unsupported training protocol")
    for name in ("model_root", "training_path", "development_path", "output_directory"):
        path = Path(value[name])
        if not path.is_absolute():
            raise ValueError(f"{name} must be absolute")
    expected_root = os.environ.get("SELENE_MODEL_ROOT")
    if (
        expected_root is None
        or Path(expected_root).resolve() != Path(value["model_root"]).resolve()
    ):
        raise ValueError("model root does not match the pinned environment")
    if value["framework"] != "mlx-lm":
        raise ValueError("MLX worker requires the mlx-lm framework identity")
    observed_version = importlib.metadata.version("mlx-lm")
    if observed_version != value["framework_version"]:
        raise ValueError("installed mlx-lm version does not match the runtime manifest")
    settings = value["hyperparameters"]
    if not isinstance(settings, dict):
        raise ValueError("training hyperparameters must be an object")
    _validate_settings(settings)
    return value


def _read_records(path: Path) -> list[dict]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_INPUT_BYTES:
        raise ValueError(f"unsafe training records: {path}")
    records = []
    for number, line in enumerate(path.read_bytes().splitlines(), 1):
        if not line.strip():
            continue
        if len(line) > _MAX_RECORD_BYTES:
            raise ValueError(f"training record {number} exceeds 2 MiB")
        try:
            record = json.loads(line)
        except ValueError as error:
            raise ValueError(f"training record {number} is invalid JSON") from error
        required = {"id", "prompt", "chosen"}
        if not isinstance(record, dict) or not required <= set(record):
            raise ValueError(f"training record {number} is missing required fields")
        if any(not isinstance(record[name], str) or not record[name] for name in required):
            raise ValueError(f"training record {number} has invalid text fields")
        if not isinstance(record.get("hard_negative", False), bool):
            raise ValueError(f"training record {number} has invalid hard-negative flag")
        records.append(record)
    if not records:
        raise ValueError(f"training records are empty: {path}")
    return records


def _validate_settings(settings: dict) -> None:
    required = {
        "stage",
        "method",
        "seed",
        "epochs",
        "learning_rate",
        "max_sequence_length",
        "batch_size",
        "gradient_accumulation_steps",
        "lora_rank",
        "lora_alpha",
        "lora_layers",
        "lora_dropout",
        "target_modules",
        "hard_negative_weight",
        "preference_beta",
        "save_steps",
        "logging_steps",
    }
    if set(settings) != required:
        raise ValueError("training hyperparameter fields do not match the worker protocol")
    if settings["stage"] != "supervised":
        raise ValueError("the pinned MLX worker supports supervised training only")
    if settings["method"] not in {"lora", "qlora"}:
        raise ValueError("MLX training method must be lora or qlora")
    numeric = (
        settings["epochs"],
        settings["learning_rate"],
        settings["lora_dropout"],
        settings["hard_negative_weight"],
    )
    if any(isinstance(value, bool) or not math.isfinite(value) for value in numeric):
        raise ValueError("training numeric hyperparameters must be finite")
    weight = settings["hard_negative_weight"]
    if int(weight) != weight:
        raise ValueError("MLX hard-negative weight must be an integer replication factor")


def _model_is_quantized(model_root: Path) -> bool:
    path = model_root / "config.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("MLX model config is unavailable or unsafe")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("MLX model config must be an object")
    return isinstance(value.get("quantization"), dict) or isinstance(
        value.get("quantization_config"), dict
    )


def _chat_records(records: list[dict], hard_negative_weight: int) -> list[dict]:
    converted = []
    for record in records:
        copies = hard_negative_weight if record.get("hard_negative", False) else 1
        example = {
            "messages": [
                {"role": "user", "content": record["prompt"]},
                {"role": "assistant", "content": record["chosen"]},
            ]
        }
        converted.extend(example for _ in range(copies))
    return converted


def _mlx_config(payload: dict, data: Path, adapter: Path, training_examples: int) -> dict:
    settings = payload["hyperparameters"]
    effective_batch = settings["batch_size"] * settings["gradient_accumulation_steps"]
    iterations = max(1, math.ceil(settings["epochs"] * training_examples / effective_batch))
    return {
        "model": payload["model_root"],
        "train": True,
        "fine_tune_type": "lora",
        "data": str(data),
        "seed": settings["seed"],
        "num_layers": settings["lora_layers"],
        "batch_size": settings["batch_size"],
        "iters": iterations,
        "val_batches": -1,
        "learning_rate": settings["learning_rate"],
        "steps_per_report": settings["logging_steps"],
        "steps_per_eval": settings["logging_steps"],
        "adapter_path": str(adapter),
        "save_every": settings["save_steps"],
        "max_seq_length": settings["max_sequence_length"],
        "grad_checkpoint": True,
        "grad_accumulation_steps": settings["gradient_accumulation_steps"],
        "mask_prompt": True,
        "report_to": None,
        "lora_parameters": {
            "rank": settings["lora_rank"],
            "dropout": settings["lora_dropout"],
            "scale": settings["lora_alpha"] / settings["lora_rank"],
            "keys": settings["target_modules"],
        },
    }


def _train(payload: dict, records: list[dict], development: list[dict]) -> None:
    settings = payload["hyperparameters"]
    model_root = Path(payload["model_root"]).resolve(strict=True)
    quantized = _model_is_quantized(model_root)
    if settings["method"] == "qlora" and not quantized:
        raise ValueError("MLX QLoRA requires an already quantized pinned model")
    if settings["method"] == "lora" and quantized:
        raise ValueError("a quantized MLX model must be declared as QLoRA")
    output = Path(payload["output_directory"])
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError("training output must be a new directory with an existing parent")
    output.mkdir(mode=0o700)
    data = output / ".training-data"
    data.mkdir(mode=0o700)
    weight = int(settings["hard_negative_weight"])
    training = _chat_records(records, weight)
    validation = _chat_records(development, weight)
    _write_jsonl(data / "train.jsonl", training)
    _write_jsonl(data / "valid.jsonl", validation)
    adapter = output / "adapter"
    configuration = _mlx_config(payload, data, adapter, len(training))
    config_path = output / "mlx-lora-config.json"
    config_path.write_text(json.dumps(configuration, indent=2, allow_nan=False) + "\n")
    command = [sys.executable, "-B", "-I", "-m", "mlx_lm.lora", "--config", str(config_path)]
    stdout_path = output / "mlx-lora.stdout.log"
    stderr_path = output / "mlx-lora.stderr.log"
    started = time.monotonic()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.run(command, stdout=stdout, stderr=stderr, check=False)
    elapsed = time.monotonic() - started
    _ensure_nonempty_log(stdout_path)
    _ensure_nonempty_log(stderr_path)
    if process.returncode != 0:
        raise RuntimeError(f"mlx_lm.lora exited with status {process.returncode}")
    required = (adapter / "adapters.safetensors", adapter / "adapter_config.json")
    if any(
        path.is_symlink() or not path.is_file() or path.stat().st_size == 0 for path in required
    ):
        raise ValueError("mlx-lm did not produce a complete adapter")
    shutil.rmtree(data)
    metrics = {
        "framework": "mlx-lm",
        "framework_version": payload["framework_version"],
        "method": settings["method"],
        "training_examples": len(training),
        "development_examples": len(validation),
        "iterations": configuration["iters"],
        "elapsed_seconds": elapsed,
        "child_exit_code": process.returncode,
    }
    (output / "training-metrics.json").write_text(
        json.dumps(metrics, indent=2, allow_nan=False) + "\n"
    )
    _write_adapter_manifest(payload, output)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("x") as stream:
        for record in records:
            stream.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")


def _ensure_nonempty_log(path: Path) -> None:
    if path.stat().st_size == 0:
        path.write_text("(no output)\n")


def _write_adapter_manifest(payload: dict, output: Path) -> None:
    artifacts = []
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "adapter-manifest.json":
            continue
        artifacts.append(
            {
                "path": path.relative_to(output).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    if not artifacts:
        raise ValueError("MLX training produced no adapter artifacts")
    manifest = {
        "schema_version": 1,
        "protocol": "selene-adapter-artifact-v1",
        "run_id": payload["run_id"],
        "run_sha256": payload["run_sha256"],
        "base_model_identity": payload["model_identity"],
        "framework": payload["framework"],
        "framework_version": payload["framework_version"],
        "stage": payload["hyperparameters"]["stage"],
        "method": payload["hyperparameters"]["method"],
        "terminal_status": "succeeded",
        "artifacts": artifacts,
    }
    (output / "adapter-manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False) + "\n"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
