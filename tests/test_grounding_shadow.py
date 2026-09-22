from __future__ import annotations

import hashlib
import json
import shlex
import stat
import sys
from pathlib import Path

import pytest

from veyro.grounding import (
    CriterionClause,
    EvidenceItem,
    EvidenceRelationship,
    EvidenceType,
    GroundingCase,
    SourceProvenance,
)
from veyro.grounding_shadow import (
    GroundingShadowProfile,
    ShadowFile,
    evaluate_grounding_shadow,
)

MODEL_IDENTITY = "AtlaAI/Selene@adapter:test"


def sha(path_or_value: Path | bytes | str) -> str:
    if isinstance(path_or_value, Path):
        value = path_or_value.read_bytes()
    elif isinstance(path_or_value, str):
        value = path_or_value.encode()
    else:
        value = path_or_value
    return hashlib.sha256(value).hexdigest()


def grounding_case(*, evidence_type: EvidenceType = EvidenceType.IMPLEMENTATION) -> GroundingCase:
    content = (
        "verify_signature(receipt)"
        if evidence_type is EvidenceType.IMPLEMENTATION
        else "Completion will require a verified signature."
    )
    item = EvidenceItem(
        id="receipt-evidence",
        type=evidence_type,
        relationship=EvidenceRelationship.SUPPORTS,
        clause_ids=frozenset({"receipt-authentication"}),
        content=content,
        content_sha256=sha(content),
        source=SourceProvenance(
            repository="example/repository",
            revision="abc123",
            path="src/receipt.py" if evidence_type is EvidenceType.IMPLEMENTATION else "PLAN.md",
            start_line=1,
            end_line=1,
            file_sha256="a" * 64,
        ),
    )
    return GroundingCase(
        id="shadow-case",
        criterion="Completion receipts are authenticated before acceptance.",
        contract_sha256="b" * 64,
        candidate_sha256="c" * 64,
        clauses=[
            CriterionClause(
                id="receipt-authentication",
                text="The controller verifies a signature on every completion receipt.",
                required_evidence_types=frozenset({EvidenceType.IMPLEMENTATION}),
            )
        ],
        evidence=[item],
    )


def launcher(
    path: Path,
    *,
    result: str = "yes",
    logit_result: str | None = None,
    extra: bool = False,
) -> None:
    status = "established" if result == "yes" else "missing"
    citations = ["receipt-evidence"] if result == "yes" else []
    response = {
        "schema_version": 1,
        "protocol": "veyro-grounding-v1",
        "case_id": "shadow-case",
        "clauses": [
            {
                "clause_id": "receipt-authentication",
                "status": status,
                "evidence_ids": citations,
                "rationale": "The supplied evidence determines the clause.",
            }
        ],
        "result": result,
    }
    logit_result = logit_result or result
    envelope = {
        "schema_version": 1,
        "protocol": "selene-grounding-worker-v1",
        "model_identity": MODEL_IDENTITY,
        "content": json.dumps(response),
        "decision_logits": {"yes": 2.0, "no": -1.0}
        if logit_result == "yes"
        else {"yes": -1.0, "no": 2.0},
        "usage": {"prompt_tokens": 100, "completion_tokens": 30},
    }
    if extra:
        envelope["unexpected"] = True
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        f"print({json.dumps(json.dumps(envelope))})\n"
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def profile(
    tmp_path: Path,
    *,
    result: str = "yes",
    logit_result: str | None = None,
    extra: bool = False,
):
    base = tmp_path / "base"
    adapter = tmp_path / "adapter"
    base.mkdir()
    adapter.mkdir()
    base_file = base / "config.json"
    adapter_file = adapter / "adapter.safetensors"
    base_file.write_text("{}")
    adapter_file.write_bytes(b"adapter")
    worker = tmp_path / "worker.py"
    launcher(worker, result=result, logit_result=logit_result, extra=extra)
    packages = tmp_path / "packages.txt"
    packages.write_text("transformers==test\npeft==test\n")
    python = tmp_path / "venv-python"
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(str(Path(sys.executable).resolve()))} "$@"\n')
    python.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return GroundingShadowProfile(
        id="selene-shadow",
        model_identity=MODEL_IDENTITY,
        python=python,
        python_sha256=sha(python),
        launcher=worker,
        launcher_sha256=sha(worker),
        package_manifest=packages,
        package_manifest_sha256=sha(packages),
        base_model_root=base,
        adapter_root=adapter,
        files=[
            ShadowFile(
                root="base_model",
                path=base_file.name,
                size=base_file.stat().st_size,
                sha256=sha(base_file),
            ),
            ShadowFile(
                root="adapter",
                path=adapter_file.name,
                size=adapter_file.stat().st_size,
                sha256=sha(adapter_file),
            ),
        ],
        max_output_tokens=256,
        timeout_seconds=10,
    )


def test_shadow_records_grounding_without_becoming_authoritative(tmp_path, monkeypatch):
    selected = profile(tmp_path)
    import veyro.grounding_shadow as shadow

    monkeypatch.setattr(shadow, "_offline_command", lambda command: command)
    receipt = evaluate_grounding_shadow(selected, grounding_case())
    assert receipt.authority == "shadow_only"
    assert receipt.reported_result == "yes"
    assert receipt.would_authorize_completion is True
    assert receipt.grounding_decision.authorizes_completion is True
    assert receipt.assessment.model_identity == MODEL_IDENTITY
    assert receipt.raw_conditional_yes_probability == pytest.approx(0.9525741268)
    assert receipt.prompt_tokens == 100


def test_shadow_deterministically_rejects_plan_as_implementation(tmp_path, monkeypatch):
    selected = profile(tmp_path)
    import veyro.grounding_shadow as shadow

    monkeypatch.setattr(shadow, "_offline_command", lambda command: command)
    receipt = evaluate_grounding_shadow(selected, grounding_case(evidence_type=EvidenceType.PLAN))
    assert receipt.reported_result == "yes"
    assert receipt.would_authorize_completion is False
    assert {item.code for item in receipt.grounding_decision.violations} == {
        "non_authoritative_evidence",
        "required_evidence_missing",
    }


def test_shadow_records_grounded_no_without_authorization(tmp_path, monkeypatch):
    selected = profile(tmp_path, result="no")
    import veyro.grounding_shadow as shadow

    monkeypatch.setattr(shadow, "_offline_command", lambda command: command)
    receipt = evaluate_grounding_shadow(selected, grounding_case())
    assert receipt.reported_result == "no"
    assert receipt.grounding_decision.assessment_valid is True
    assert receipt.would_authorize_completion is False


def test_shadow_rejects_extra_worker_fields(tmp_path, monkeypatch):
    selected = profile(tmp_path, extra=True)
    import veyro.grounding_shadow as shadow

    monkeypatch.setattr(shadow, "_offline_command", lambda command: command)
    with pytest.raises(ValueError, match="unexpected"):
        evaluate_grounding_shadow(selected, grounding_case())


def test_shadow_rejects_generated_result_that_disagrees_with_exact_logits(tmp_path, monkeypatch):
    selected = profile(tmp_path, logit_result="no")
    import veyro.grounding_shadow as shadow

    monkeypatch.setattr(shadow, "_offline_command", lambda command: command)
    with pytest.raises(ValueError, match="forced-choice logits"):
        evaluate_grounding_shadow(selected, grounding_case())


def test_shadow_detects_artifact_mutation_before_inference(tmp_path, monkeypatch):
    selected = profile(tmp_path)
    (selected.adapter_root / "adapter.safetensors").write_bytes(b"changed")
    import veyro.grounding_shadow as shadow

    monkeypatch.setattr(
        shadow.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("mutated model must fail before inference"),
    )
    with pytest.raises(ValueError, match="size mismatch|SHA-256 mismatch"):
        evaluate_grounding_shadow(selected, grounding_case())


def test_shadow_rejects_writable_interpreter_target_before_inference(tmp_path, monkeypatch):
    selected = profile(tmp_path)
    unsafe = tmp_path / "writable-python"
    unsafe.write_text("#!/bin/sh\nexit 0\n")
    unsafe.chmod(0o777)
    selected.python.unlink()
    selected.python.symlink_to(unsafe)
    selected = selected.model_copy(update={"python_sha256": sha(unsafe)})
    import veyro.grounding_shadow as shadow

    monkeypatch.setattr(
        shadow.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("unsafe runtime must fail before inference"),
    )
    with pytest.raises(ValueError, match="unsafe shadow runtime file"):
        evaluate_grounding_shadow(selected, grounding_case())


def test_shadow_applies_only_a_model_bound_calibration_artifact(tmp_path, monkeypatch):
    from veyro.selene_calibration import (
        BinaryMetrics,
        CalibrationArtifact,
        CalibrationCandidate,
    )

    selected = profile(tmp_path)
    metrics = BinaryMetrics(
        cases=2,
        score_coverage=1,
        accuracy=1,
        balanced_accuracy=1,
        positive_accuracy=1,
        negative_accuracy=1,
        false_accepts=0,
        brier=0.1,
        log_loss=0.2,
        p95_ms=1,
        peak_memory_bytes=1,
    )
    artifact = CalibrationArtifact(
        contract_sha256="a" * 64,
        model_identity=MODEL_IDENTITY,
        predictions_sha256="b" * 64,
        labels_sha256="c" * 64,
        raw_metrics=metrics,
        selected_method="temperature",
        parameters={"temperature": 2.0},
        candidates=[
            CalibrationCandidate(
                method="identity",
                parameters={},
                metrics=metrics,
                class_predictions_preserved=True,
                eligible=True,
                rejection_reasons=[],
            ),
            CalibrationCandidate(
                method="temperature",
                parameters={"temperature": 2.0},
                metrics=metrics,
                class_predictions_preserved=True,
                eligible=True,
                rejection_reasons=[],
            ),
        ],
    )
    calibration = tmp_path / "calibration.json"
    calibration.write_text(artifact.model_dump_json(indent=2))
    selected = selected.model_copy(
        update={"calibration": calibration, "calibration_sha256": sha(calibration)}
    )
    import veyro.grounding_shadow as shadow

    monkeypatch.setattr(shadow, "_offline_command", lambda command: command)
    receipt = evaluate_grounding_shadow(selected, grounding_case())
    assert receipt.raw_conditional_yes_probability == pytest.approx(0.9525741268)
    assert receipt.calibrated_yes_probability == pytest.approx(0.8175744762)
    assert receipt.calibration_sha256 == sha(calibration)

    calibration.write_text(
        artifact.model_copy(update={"model_identity": "other-model"}).model_dump_json()
    )
    mismatched = selected.model_copy(update={"calibration_sha256": sha(calibration)})
    with pytest.raises(ValueError, match="calibration model identity"):
        evaluate_grounding_shadow(mismatched, grounding_case())


def test_shadow_hard_denies_retired_holdout_cases_before_inference(tmp_path, monkeypatch):
    selected = profile(tmp_path)
    retired = grounding_case().model_copy(update={"origin_study_id": "selene-holdout-v1"})
    import veyro.grounding_shadow as shadow

    monkeypatch.setattr(
        shadow.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("retired holdout must never reach inference"),
    )
    with pytest.raises(ValueError, match="cannot be rerun"):
        evaluate_grounding_shadow(selected, retired)
