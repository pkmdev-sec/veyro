from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Literal

from veyro.grounding import (
    ClauseAssessment,
    ClauseStatus,
    CriterionClause,
    EvidenceItem,
    EvidenceRelationship,
    EvidenceType,
    ExecutedCheckReceipt,
    GroundingAssessment,
    GroundingCase,
    validate_grounding,
)
from veyro.selene_calibration import BinaryLabel, BinaryPrediction, binary_metrics


def _prediction(
    case_id: str,
    digest: str,
    yes_logit: float,
    no_logit: float,
) -> BinaryPrediction:
    return BinaryPrediction(
        case_id=case_id,
        case_sha256=digest,
        model_identity="selene-test-model",
        yes_logit=yes_logit,
        no_logit=no_logit,
        latency_ms=1,
        peak_memory_bytes=1024,
    )


def _label(
    case_id: str,
    digest: str,
    expected: Literal["yes", "no"],
) -> BinaryLabel:
    return BinaryLabel(
        case_id=case_id,
        case_sha256=digest,
        expected=expected,
        author="independent-author",
        reviewer="independent-reviewer",
    )


def _execution_receipt_case(receipt_candidate_sha256: str) -> GroundingCase:
    contract_sha256 = "a" * 64
    candidate_sha256 = "b" * 64
    content = "The protected check completed successfully."
    receipt = ExecutedCheckReceipt(
        command=["python", "smoke.py"],
        exit_code=0,
        timed_out=False,
        protected=True,
        contract_sha256=contract_sha256,
        candidate_sha256=receipt_candidate_sha256,
        output_sha256="c" * 64,
        completed_at=datetime.now(UTC),
    )
    evidence = EvidenceItem(
        id="executed-check",
        type=EvidenceType.EXECUTED_CHECK,
        relationship=EvidenceRelationship.SUPPORTS,
        clause_ids=frozenset({"behavior"}),
        content=content,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        check=receipt,
    )
    return GroundingCase(
        id="receipt-case",
        criterion="The protected smoke check passes.",
        contract_sha256=contract_sha256,
        candidate_sha256=candidate_sha256,
        clauses=[
            CriterionClause(
                id="behavior",
                text="The protected smoke check passes.",
                required_evidence_types=frozenset({EvidenceType.EXECUTED_CHECK}),
            )
        ],
        evidence=[evidence],
    )


def _established_assessment() -> GroundingAssessment:
    return GroundingAssessment(
        case_id="receipt-case",
        clauses=[
            ClauseAssessment(
                clause_id="behavior",
                status=ClauseStatus.ESTABLISHED,
                evidence_ids=["executed-check"],
                rationale="The protected receipt reports a successful check.",
            )
        ],
        result="yes",
        model_identity="selene-test-model",
        generated_at=datetime.now(UTC),
        raw_response_sha256="d" * 64,
        conditional_yes_probability=0.8,
    )


def test_binary_metrics_count_every_false_accept_without_confidence_thresholds():
    case_ids = ("positive-a", "positive-b", "negative-a", "negative-b")
    digests = {case_id: character * 64 for case_id, character in zip(case_ids, "abcd", strict=True)}
    predictions = [
        _prediction("positive-a", digests["positive-a"], 2, 0),
        _prediction("positive-b", digests["positive-b"], 1, 0),
        _prediction("negative-a", digests["negative-a"], -2, 0),
        _prediction("negative-b", digests["negative-b"], 0.1, 0),
    ]
    labels = [
        _label("positive-a", digests["positive-a"], "yes"),
        _label("positive-b", digests["positive-b"], "yes"),
        _label("negative-a", digests["negative-a"], "no"),
        _label("negative-b", digests["negative-b"], "no"),
    ]

    metrics = binary_metrics(predictions, labels)

    assert metrics.score_coverage == 1
    assert metrics.false_accepts == 1
    assert metrics.positive_accuracy == 1
    assert metrics.negative_accuracy == 0.5
    assert metrics.balanced_accuracy == 0.75


def test_mutated_execution_receipt_cannot_authorize_completion():
    assessment = _established_assessment()

    valid = validate_grounding(_execution_receipt_case("b" * 64), assessment)
    assert valid.authorizes_completion is True

    mutated = validate_grounding(_execution_receipt_case("e" * 64), assessment)
    assert mutated.authorizes_completion is False
    assert {violation.code for violation in mutated.violations} == {"stale_executed_check"}
