from __future__ import annotations

import pytest
from pydantic import ValidationError

from veyro.models import (
    BoundaryAction,
    BoundaryDisposition,
    BoundaryOperation,
    SessionIdentity,
)
from veyro.supervision import BoundaryPolicy


def identity() -> SessionIdentity:
    return SessionIdentity(
        veyro_session_id="veyro-1",
        provider_id="prime-agent",
        provider_session_id="prime-1",
        repository="/tmp/project",
        bridge_id="prime-daemon-v4",
        bridge_version="1.0.0",
    )


def action(operation: BoundaryOperation) -> BoundaryAction:
    return BoundaryAction(
        action_id=f"action-{operation.value}",
        session=identity(),
        operation=operation,
        target_sha256="a" * 64,
    )


def test_every_boundary_operation_has_one_explicit_deterministic_classification() -> None:
    policy = BoundaryPolicy()
    expected = {
        BoundaryOperation.DECLINE_APPROVAL: BoundaryDisposition.LOW_RISK,
        BoundaryOperation.READ_REPOSITORY: BoundaryDisposition.LOW_RISK,
        BoundaryOperation.WRITE_REPOSITORY: BoundaryDisposition.LOW_RISK,
        BoundaryOperation.RUN_LOCAL_CHECK: BoundaryDisposition.LOW_RISK,
        BoundaryOperation.INSPECT_LOCAL_ENVIRONMENT: BoundaryDisposition.LOW_RISK,
        BoundaryOperation.EXECUTE_LOCAL_COMMAND: BoundaryDisposition.REVIEW_REQUIRED,
        BoundaryOperation.NETWORK_READ: BoundaryDisposition.REVIEW_REQUIRED,
        BoundaryOperation.NETWORK_WRITE: BoundaryDisposition.REVIEW_REQUIRED,
        BoundaryOperation.INSTALL_DEPENDENCY: BoundaryDisposition.REVIEW_REQUIRED,
        BoundaryOperation.WRITE_OUTSIDE_REPOSITORY: BoundaryDisposition.REVIEW_REQUIRED,
        BoundaryOperation.DELETE_LOCAL_DATA: BoundaryDisposition.REVIEW_REQUIRED,
        BoundaryOperation.DELETE_REMOTE_RESOURCE: BoundaryDisposition.REVIEW_REQUIRED,
        BoundaryOperation.ACCESS_CREDENTIAL: BoundaryDisposition.REVIEW_REQUIRED,
        BoundaryOperation.MODIFY_SECURITY_CONTROL: BoundaryDisposition.REVIEW_REQUIRED,
        BoundaryOperation.PRIVILEGED_SYSTEM_CHANGE: BoundaryDisposition.REVIEW_REQUIRED,
        BoundaryOperation.EXPOSE_CREDENTIAL: BoundaryDisposition.FORBIDDEN,
        BoundaryOperation.BYPASS_SECURITY_CONTROL: BoundaryDisposition.FORBIDDEN,
        BoundaryOperation.UNKNOWN: BoundaryDisposition.REVIEW_REQUIRED,
    }
    assert set(expected) == set(BoundaryOperation)

    actions = {operation: action(operation) for operation in BoundaryOperation}
    first = {operation: policy.classify(value) for operation, value in actions.items()}
    second = {operation: policy.classify(value) for operation, value in actions.items()}

    assert {operation: result.disposition for operation, result in first.items()} == expected
    assert first == second
    assert all(
        result.rule_id == f"boundary.{operation.value}" for operation, result in first.items()
    )


def test_boundary_evidence_uses_a_digest_instead_of_raw_targets() -> None:
    decision = BoundaryPolicy().classify(action(BoundaryOperation.NETWORK_WRITE))

    serialized = decision.model_dump_json()
    assert decision.action.target_sha256 == "a" * 64
    assert decision.policy_version == "1.0"
    assert "target" not in decision.action.model_fields_set
    assert "https://" not in serialized


def test_boundary_action_rejects_malformed_evidence_digests() -> None:
    with pytest.raises(ValidationError, match="target_sha256"):
        BoundaryAction(
            action_id="action-1",
            session=identity(),
            operation=BoundaryOperation.READ_REPOSITORY,
            target_sha256="not-a-digest",
        )
