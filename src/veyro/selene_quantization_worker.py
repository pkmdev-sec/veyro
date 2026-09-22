"""Isolated MLX fuse plus llama.cpp quantization worker for Selene adapters."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_MAX_REQUEST_BYTES = 2 * 1024 * 1024


def main() -> None:
    try:
        payload = _payload()
        _quantize(payload)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
        print(f"Selene quantization worker failed: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps({"status": "succeeded"}))


def _payload() -> dict:
    raw = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
    if len(raw) > _MAX_REQUEST_BYTES:
        raise ValueError("quantization payload exceeds 2 MiB")
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise ValueError("quantization payload is not valid JSON") from error
    required = {
        "protocol",
        "run_id",
        "run_sha256",
        "model_root",
        "model_identity",
        "adapter_root",
        "adapter_manifest_sha256",
        "adapter_method",
        "output_directory",
        "framework",
        "framework_version",
        "quantizer",
        "quantizer_sha256",
        "quantizer_version",
        "quantization",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("quantization payload fields do not match the worker protocol")
    if value["protocol"] != "selene-deployment-quantization-v1":
        raise ValueError("unsupported quantization protocol")
    if value["framework"] != "mlx-lm":
        raise ValueError("deployment quantization requires mlx-lm")
    if value["quantization"] != "q4_k_m":
        raise ValueError("unsupported deployment quantization")
    if value["adapter_method"] not in {"lora", "qlora"}:
        raise ValueError("unsupported adapter method")
    for name in ("model_root", "adapter_root", "output_directory", "quantizer"):
        if not Path(value[name]).is_absolute():
            raise ValueError(f"{name} must be absolute")
    expected_root = os.environ.get("SELENE_MODEL_ROOT")
    if (
        expected_root is None
        or Path(expected_root).resolve() != Path(value["model_root"]).resolve()
    ):
        raise ValueError("model root does not match the pinned environment")
    if importlib.metadata.version("mlx-lm") != value["framework_version"]:
        raise ValueError("installed mlx-lm version does not match the runtime manifest")
    quantizer = Path(value["quantizer"])
    if quantizer.is_symlink() or not quantizer.is_file() or not os.access(quantizer, os.X_OK):
        raise ValueError("quantizer executable is unavailable or unsafe")
    if _sha256(quantizer) != value["quantizer_sha256"]:
        raise ValueError("quantizer SHA-256 mismatch")
    return value


def _quantize(payload: dict) -> None:
    model = Path(payload["model_root"]).resolve(strict=True)
    adapter = Path(payload["adapter_root"]).resolve(strict=True)
    if adapter.is_symlink() or not adapter.is_dir():
        raise ValueError("adapter root is unavailable or unsafe")
    for name in ("adapter_config.json", "adapters.safetensors"):
        path = adapter / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError("MLX adapter is incomplete")
    output = Path(payload["output_directory"])
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError("quantization output must be a new directory with an existing parent")
    output.mkdir(mode=0o700)
    temporary = output / ".fuse"
    fused = temporary / "fused-model"
    temporary.mkdir(mode=0o700)
    fuse_stdout = output / "mlx-fuse.stdout.log"
    fuse_stderr = output / "mlx-fuse.stderr.log"
    quantize_stdout = output / "llama-quantize.stdout.log"
    quantize_stderr = output / "llama-quantize.stderr.log"
    started = time.monotonic()
    fuse_command = [
        sys.executable,
        "-B",
        "-I",
        "-m",
        "mlx_lm.fuse",
        "--model",
        str(model),
        "--adapter-path",
        str(adapter),
        "--save-path",
        str(fused),
        "--export-gguf",
        "--gguf-path",
        "model-f16.gguf",
    ]
    if payload["adapter_method"] == "qlora":
        fuse_command.append("--dequantize")
    fuse_code = _run(fuse_command, fuse_stdout, fuse_stderr, cwd=temporary)
    if fuse_code != 0:
        raise RuntimeError(f"mlx_lm.fuse exited with status {fuse_code}")
    candidates = [
        fused / "model-f16.gguf",
        temporary / "model-f16.gguf",
    ]
    existing = [path for path in candidates if path.is_file() and not path.is_symlink()]
    if len(existing) != 1 or existing[0].stat().st_size == 0:
        raise ValueError("MLX fuse did not produce one FP16 GGUF artifact")
    source = existing[0]
    destination = output / "selene-q4_k_m.gguf"
    quantize_command = [
        payload["quantizer"],
        str(source),
        str(destination),
        "Q4_K_M",
    ]
    quantize_code = _run(quantize_command, quantize_stdout, quantize_stderr, cwd=output)
    if quantize_code != 0:
        raise RuntimeError(f"llama-quantize exited with status {quantize_code}")
    if destination.is_symlink() or not destination.is_file() or destination.stat().st_size == 0:
        raise ValueError("llama-quantize did not produce a deployment artifact")
    shutil.rmtree(temporary)
    for path in (fuse_stdout, fuse_stderr, quantize_stdout, quantize_stderr):
        _ensure_nonempty_log(path)
    metadata = {
        "framework": payload["framework"],
        "framework_version": payload["framework_version"],
        "quantizer_version": payload["quantizer_version"],
        "quantization": payload["quantization"],
        "elapsed_seconds": time.monotonic() - started,
    }
    (output / "quantization-metrics.json").write_text(
        json.dumps(metadata, indent=2, allow_nan=False) + "\n"
    )
    _write_manifest(payload, output)


def _run(command: list[str], stdout_path: Path, stderr_path: Path, *, cwd: Path) -> int:
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        process = subprocess.run(command, stdout=stdout, stderr=stderr, check=False, cwd=cwd)
    return process.returncode


def _ensure_nonempty_log(path: Path) -> None:
    if path.stat().st_size == 0:
        path.write_text("(no output)\n")


def _write_manifest(payload: dict, output: Path) -> None:
    artifacts = []
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "deployment-manifest.json":
            continue
        artifacts.append(
            {
                "path": path.relative_to(output).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    if sum(item["path"].endswith(".gguf") for item in artifacts) != 1:
        raise ValueError("deployment output must contain exactly one GGUF artifact")
    manifest = {
        "schema_version": 1,
        "protocol": "selene-deployment-artifact-v1",
        "run_id": payload["run_id"],
        "run_sha256": payload["run_sha256"],
        "base_model_identity": payload["model_identity"],
        "adapter_manifest_sha256": payload["adapter_manifest_sha256"],
        "framework": payload["framework"],
        "framework_version": payload["framework_version"],
        "quantizer_sha256": payload["quantizer_sha256"],
        "quantizer_version": payload["quantizer_version"],
        "quantization": payload["quantization"],
        "terminal_status": "succeeded",
        "artifacts": artifacts,
    }
    (output / "deployment-manifest.json").write_text(
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
