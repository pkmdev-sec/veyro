"""CLI boundaries for owned local workers and reusable evaluator artifacts."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from pydantic import TypeAdapter

local_app = typer.Typer(
    help="Experimental: inspect and run pinned local Qwen workers without global reconfiguration."
)
evaluator_app = typer.Typer(
    help="Experimental: preview, run, correct, and calibrate typed local evaluators."
)


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except (ValueError, RuntimeError, OSError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from error


def _print(value) -> None:
    typer.echo(json.dumps(value, indent=2, allow_nan=False))


@local_app.command("models")
def models() -> None:
    """Show installed identity and actual Ollama residency without loading weights."""
    from veyro.local_models import LocalModels, load_profiles

    with _errors():
        client = LocalModels()
        _print(
            {
                name: client.status(profile).model_dump(mode="json")
                for name, profile in load_profiles().items()
            }
        )


@local_app.command("warm")
def warm(profile: str) -> None:
    """Warm a pinned coding model; refuse to evict another resident Ollama model."""
    from veyro.local_models import LocalModels, load_profiles

    with _errors():
        profiles = load_profiles()
        if profile not in profiles:
            raise ValueError(f"unknown local model profile: {profile}")
        _print(LocalModels().warm(profiles[profile]).model_dump(mode="json"))


@local_app.command("start")
def start(
    profile: str,
    port: Annotated[int | None, typer.Option(min=1024, max=65535)] = None,
    state_dir: Path | None = None,
    context_size: Annotated[int, typer.Option(min=512, max=32768)] = 8192,
) -> None:
    """Start an owned loopback readout worker with persistent weights and KV state."""
    from veyro.local_server import start_service

    with _errors():
        _print(start_service(profile, port=port, state_dir=state_dir, context_size=context_size))


@local_app.command("status")
def status(profile: str, state_dir: Path | None = None) -> None:
    from veyro.local_server import service_status

    with _errors():
        _print(service_status(profile, state_dir=state_dir))


@local_app.command("stop")
def stop(profile: str, state_dir: Path | None = None) -> None:
    """Stop only the authenticated Veyro worker, never a PID from an untrusted file."""
    from veyro.local_server import stop_service

    with _errors():
        _print(stop_service(profile, state_dir=state_dir))


@evaluator_app.command("preview")
def preview(definition: Path, case: Path, corrections: Path | None = None) -> None:
    from veyro.evaluators import (
        EvaluationCase,
        EvaluatorDefinition,
        build_evaluation_request,
        load_corrections,
    )

    with _errors():
        schema = EvaluatorDefinition.load(definition)
        live = EvaluationCase.model_validate_json(case.read_text())
        examples = load_corrections(corrections, schema) if corrections else []
        _print(build_evaluation_request(schema, live, corrections=examples))


@evaluator_app.command("run")
def evaluate(
    definition: Path,
    case: Path,
    profile: str = "small",
    corrections: Path | None = None,
    calibration: Annotated[list[str] | None, typer.Option(help="Repeat QUESTION=FILE")] = None,
    state_dir: Path | None = None,
    timeout: Annotated[float, typer.Option(min=0.01, max=120)] = 60,
) -> None:
    from veyro.evaluators import (
        DistributionCalibrator,
        EvaluationCase,
        EvaluatorDefinition,
        load_corrections,
    )
    from veyro.local_evaluation import run_evaluator

    with _errors():
        schema = EvaluatorDefinition.load(definition)
        live = EvaluationCase.model_validate_json(case.read_text())
        examples = load_corrections(corrections, schema) if corrections else []
        fitted = {}
        for entry in calibration or []:
            name, separator, filename = entry.partition("=")
            if not separator or not name or not filename or name in fitted:
                raise ValueError("calibrations need unique QUESTION=FILE entries")
            fitted[name] = DistributionCalibrator.load(Path(filename))
        _print(
            run_evaluator(
                schema,
                live,
                profile,
                corrections=examples,
                calibrations=fitted,
                state_dir=state_dir,
                timeout=timeout,
            )
        )


@evaluator_app.command("correct")
def correct(definition: Path, record: Path, store: Path) -> None:
    """Add or replace one reviewed example. Existing records are validated, not discarded."""
    import fcntl

    from veyro.evaluators import (
        CorrectionRecord,
        EvaluatorDefinition,
        load_corrections,
        save_corrections,
    )

    with _errors():
        schema = EvaluatorDefinition.load(definition)
        correction = CorrectionRecord.model_validate_json(record.read_text())
        correction.validate_for(schema)
        with store.with_name(store.name + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            records = load_corrections(store, schema) if store.exists() else []
            records = [item for item in records if item.case.id != correction.case.id]
            save_corrections(store, [*records, correction], schema)
        _print({"store": str(store), "case_id": correction.case.id, "records": len(records) + 1})


@evaluator_app.command("calibrate")
def calibrate(
    definition: Path,
    question: str,
    training: Path,
    holdout_ids: Path,
    output: Path,
    profile: str = "small",
    corrections: Path | None = None,
) -> None:
    """Fit post-hoc temperature on training predictions; reserve holdout IDs, not labels."""
    from veyro.evaluators import (
        CalibrationExample,
        DistributionCalibrator,
        EvaluatorDefinition,
        few_shot_preview,
        load_corrections,
        outcome_labels,
    )
    from veyro.local_evaluation import (
        calibration_schema,
        load_evaluation_profiles,
        model_identity,
    )

    with _errors():
        schema = EvaluatorDefinition.load(definition)
        profiles = load_evaluation_profiles()
        if question not in schema.questions or profile not in profiles:
            raise ValueError("unknown question or local profile")
        examples = TypeAdapter(list[CalibrationExample]).validate_json(training.read_text())
        expected_outcomes = set(outcome_labels(schema.questions[question]))
        if any(set(example.probabilities) != expected_outcomes for example in examples):
            raise ValueError("training outcomes must match the selected question")
        reserved = TypeAdapter(list[str]).validate_json(holdout_ids.read_text(), strict=True)
        reviewed = load_corrections(corrections, schema) if corrections else []
        few_shot_preview(schema, reviewed, holdout_ids=[*reserved, *(e.id for e in examples)])
        artifact = DistributionCalibrator.fit(
            examples,
            holdout_ids=reserved,
            model=model_identity(profiles[profile]),
            profile=profile,
            schema_sha256=calibration_schema(schema, question, reviewed),
        )
        artifact.save(output)
        _print(artifact.model_dump(mode="json"))


@evaluator_app.command("fit-decision-head")
def fit_decision_head(
    definition: Path,
    training: Path,
    holdout_ids: Path,
    output: Path,
    feature: Annotated[
        list[str] | None, typer.Option(help="Repeat selected evaluator feature")
    ] = None,
    profile: str = "small",
    regularization: Annotated[float, typer.Option(min=1e-9)] = 1.0,
) -> None:
    """Fit an auditable binary logistic head; this does not train model weights."""
    from veyro.evaluators import BinaryDecisionHead, DecisionHeadExample, EvaluatorDefinition
    from veyro.local_evaluation import (
        decision_head_schema,
        load_evaluation_profiles,
        model_identity,
    )

    with _errors():
        schema = EvaluatorDefinition.load(definition)
        profiles = load_evaluation_profiles()
        if profile not in profiles:
            raise ValueError("unknown local profile")
        features = feature or []
        examples = TypeAdapter(list[DecisionHeadExample]).validate_json(training.read_text())
        reserved = TypeAdapter(list[str]).validate_json(holdout_ids.read_text(), strict=True)
        artifact = BinaryDecisionHead.fit(
            examples,
            feature_names=features,
            holdout_ids=reserved,
            model=model_identity(profiles[profile]),
            profile=profile,
            schema_sha256=decision_head_schema(schema, features),
            regularization=regularization,
        )
        artifact.save(output)
        _print(artifact.model_dump(mode="json"))


@evaluator_app.command("validate-decision-head")
def validate_decision_head(head: Path, holdout: Path) -> None:
    from veyro.evaluators import BinaryDecisionHead, DecisionHeadExample

    with _errors():
        artifact = BinaryDecisionHead.load(head)
        examples = TypeAdapter(list[DecisionHeadExample]).validate_json(holdout.read_text())
        _print(artifact.evaluate_holdout(examples).model_dump(mode="json"))


@evaluator_app.command("validate-calibration")
def validate_calibration(calibration: Path, holdout: Path) -> None:
    from veyro.evaluators import CalibrationExample, DistributionCalibrator

    with _errors():
        artifact = DistributionCalibrator.load(calibration)
        examples = TypeAdapter(list[CalibrationExample]).validate_json(holdout.read_text())
        _print(artifact.evaluate_holdout(examples))
