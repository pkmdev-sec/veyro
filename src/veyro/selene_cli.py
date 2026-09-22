"""JSON CLI boundaries for structured grounding and Selene adapter research."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer
from pydantic import TypeAdapter, ValidationError

selene_app = typer.Typer(
    help=(
        "Experimental research: validate grounded evidence and run offline Selene "
        "training and shadow workflows."
    )
)


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except (OSError, RuntimeError, TimeoutError, TypeError, ValueError, ValidationError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from error


def _print(value) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    typer.echo(json.dumps(value, indent=2, allow_nan=False))


def _exclusive_json(path: Path, value) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
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


@selene_app.command("render-prompt")
def render_prompt(case: Path) -> None:
    """Render a dynamically fenced grounding prompt without model inference."""
    from veyro.grounding import GroundingCase, render_grounding_prompt

    with _errors():
        parsed = GroundingCase.model_validate_json(case.read_text())
        typer.echo(render_grounding_prompt(parsed))


@selene_app.command("validate-grounding")
def validate_grounding_command(case: Path, response: Path) -> None:
    """Apply deterministic fail-closed grounding rules to one model response."""
    from veyro.grounding import GroundingCase, GroundingResponse, validate_grounding

    with _errors():
        parsed_case = GroundingCase.model_validate_json(case.read_text())
        parsed_response = GroundingResponse.model_validate_json(response.read_text())
        _print(validate_grounding(parsed_case, parsed_response))


@selene_app.command("validate-dataset")
def validate_dataset(manifest: Path) -> None:
    from veyro.grounding_data import validate_dataset_manifest

    with _errors():
        dataset = validate_dataset_manifest(manifest)
        _print(
            {
                "protocol": dataset.manifest.protocol,
                "dataset_id": dataset.manifest.dataset_id,
                "manifest_sha256": dataset.manifest_sha256,
                "case_counts": dataset.case_counts,
                "status": "passed",
            }
        )


@selene_app.command("export-dataset")
def export_dataset(
    manifest: Path,
    role: str,
    output: Path,
    preference: bool = False,
) -> None:
    """Export one non-qualification role after all custody checks pass."""
    from veyro.grounding_data import (
        DatasetRole,
        export_training_records,
        validate_dataset_manifest,
    )

    with _errors():
        selected = DatasetRole(role)
        dataset = validate_dataset_manifest(manifest)
        records = export_training_records(dataset, selected, preference=preference)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                for record in records:
                    stream.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                output.unlink()
            except OSError:
                pass
            raise
        _print({"role": selected.value, "records": len(records), "output": str(output)})


@selene_app.command("preflight")
def preflight(config: Path) -> None:
    """Verify weights, licences, data custody, runtime, outputs, and disk without training."""
    from veyro.selene_training import TrainingRun, preflight_training

    with _errors():
        run = TrainingRun.model_validate_json(config.read_text())
        _print(preflight_training(run))


@selene_app.command("train")
def train(config: Path) -> None:
    """Run one immutable offline training attempt; failures are terminal receipts."""
    from veyro.selene_training import TrainingRun, run_training

    with _errors():
        run = TrainingRun.model_validate_json(config.read_text())
        result = run_training(run)
        _print(result)
    if result.status != "succeeded":
        raise typer.Exit(1)


@selene_app.command("calibrate")
def calibrate(
    contract: Path,
    predictions: Path,
    labels: Path,
    output: Path,
) -> None:
    """Fit only methods named by a frozen calibration contract."""
    from veyro.selene_calibration import (
        BinaryLabel,
        BinaryPrediction,
        CalibrationContract,
        fit_calibration,
    )

    with _errors():
        frozen = CalibrationContract.model_validate_json(contract.read_text())
        scores = TypeAdapter(list[BinaryPrediction]).validate_json(predictions.read_text())
        truth = TypeAdapter(list[BinaryLabel]).validate_json(labels.read_text())
        artifact = fit_calibration(frozen, scores, truth)
        _exclusive_json(output, artifact)
        _print(artifact)


@selene_app.command("compare-quantization")
def compare_quantization_command(
    contract: Path,
    bf16_predictions: Path,
    quantized_predictions: Path,
    labels: Path,
    output: Path,
) -> None:
    """Reject quantization when it adds false accepts or regresses either class."""
    from veyro.selene_calibration import (
        BinaryLabel,
        BinaryPrediction,
        QuantizationContract,
        compare_quantization,
    )

    with _errors():
        frozen = QuantizationContract.model_validate_json(contract.read_text())
        adapter = TypeAdapter(list[BinaryPrediction])
        bf16 = adapter.validate_json(bf16_predictions.read_text())
        quantized = adapter.validate_json(quantized_predictions.read_text())
        truth = TypeAdapter(list[BinaryLabel]).validate_json(labels.read_text())
        report = compare_quantization(frozen, bf16, quantized, truth)
        _exclusive_json(output, report)
        _print(report)
    if not report.passes:
        raise typer.Exit(1)


@selene_app.command("shadow")
def shadow(profile: Path, case: Path) -> None:
    """Run pinned offline grounding inference and emit a shadow-only receipt."""
    from veyro.grounding import GroundingCase
    from veyro.grounding_shadow import (
        GroundingShadowProfile,
        evaluate_grounding_shadow,
    )

    with _errors():
        selected = GroundingShadowProfile.model_validate_json(profile.read_text())
        parsed_case = GroundingCase.model_validate_json(case.read_text())
        _print(evaluate_grounding_shadow(selected, parsed_case))


@selene_app.command("preflight-quantization")
def preflight_quantization_command(config: Path) -> None:
    """Verify model, adapter, runtime, licence, output, and peak disk custody."""
    from veyro.selene_quantization import QuantizationRun, preflight_quantization

    with _errors():
        run = QuantizationRun.model_validate_json(config.read_text())
        _print(preflight_quantization(run))


@selene_app.command("quantize")
def quantize_command(config: Path) -> None:
    """Create one immutable offline deployment quantization attempt."""
    from veyro.selene_quantization import QuantizationRun, run_quantization

    with _errors():
        run = QuantizationRun.model_validate_json(config.read_text())
        result = run_quantization(run)
        _print(result)
    if result.status != "succeeded":
        raise typer.Exit(1)
