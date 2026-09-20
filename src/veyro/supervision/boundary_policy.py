from __future__ import annotations

from veyro.models.boundary import (
    BoundaryAction,
    BoundaryDecision,
    BoundaryDisposition,
    BoundaryOperation,
)

_RULES: dict[BoundaryOperation, tuple[BoundaryDisposition, str]] = {
    BoundaryOperation.DECLINE_APPROVAL: (
        BoundaryDisposition.LOW_RISK,
        "declining a pending approval does not execute its requested operation",
    ),
    BoundaryOperation.READ_REPOSITORY: (
        BoundaryDisposition.LOW_RISK,
        "repository reads are local and non-destructive",
    ),
    BoundaryOperation.WRITE_REPOSITORY: (
        BoundaryDisposition.LOW_RISK,
        "repository writes are local and reviewable",
    ),
    BoundaryOperation.RUN_LOCAL_CHECK: (
        BoundaryDisposition.LOW_RISK,
        "declared local checks are non-destructive",
    ),
    BoundaryOperation.INSPECT_LOCAL_ENVIRONMENT: (
        BoundaryDisposition.LOW_RISK,
        "local environment inspection is non-destructive",
    ),
    BoundaryOperation.EXECUTE_LOCAL_COMMAND: (
        BoundaryDisposition.REVIEW_REQUIRED,
        "arbitrary local commands require review",
    ),
    BoundaryOperation.NETWORK_READ: (
        BoundaryDisposition.REVIEW_REQUIRED,
        "network access crosses the local trust boundary",
    ),
    BoundaryOperation.NETWORK_WRITE: (
        BoundaryDisposition.REVIEW_REQUIRED,
        "remote writes create external side effects",
    ),
    BoundaryOperation.INSTALL_DEPENDENCY: (
        BoundaryDisposition.REVIEW_REQUIRED,
        "dependency installation executes externally supplied code",
    ),
    BoundaryOperation.WRITE_OUTSIDE_REPOSITORY: (
        BoundaryDisposition.REVIEW_REQUIRED,
        "writes outside the repository affect unrelated local state",
    ),
    BoundaryOperation.DELETE_LOCAL_DATA: (
        BoundaryDisposition.REVIEW_REQUIRED,
        "local deletion may be irreversible",
    ),
    BoundaryOperation.DELETE_REMOTE_RESOURCE: (
        BoundaryDisposition.REVIEW_REQUIRED,
        "remote deletion is an irreversible external action",
    ),
    BoundaryOperation.ACCESS_CREDENTIAL: (
        BoundaryDisposition.REVIEW_REQUIRED,
        "credential access crosses a sensitive boundary",
    ),
    BoundaryOperation.MODIFY_SECURITY_CONTROL: (
        BoundaryDisposition.REVIEW_REQUIRED,
        "security-control changes require explicit review",
    ),
    BoundaryOperation.PRIVILEGED_SYSTEM_CHANGE: (
        BoundaryDisposition.REVIEW_REQUIRED,
        "privileged system changes affect the host trust boundary",
    ),
    BoundaryOperation.EXPOSE_CREDENTIAL: (
        BoundaryDisposition.FORBIDDEN,
        "credential disclosure is forbidden",
    ),
    BoundaryOperation.BYPASS_SECURITY_CONTROL: (
        BoundaryDisposition.FORBIDDEN,
        "bypassing a security control is forbidden",
    ),
    BoundaryOperation.UNKNOWN: (
        BoundaryDisposition.REVIEW_REQUIRED,
        "unknown actions cannot be treated as low risk",
    ),
}

if set(_RULES) != set(BoundaryOperation):
    raise RuntimeError("every boundary operation must have exactly one policy rule")


class BoundaryPolicy:
    """Pure deterministic classification before semantic assessment."""

    def __init__(self, *, review_operations: tuple[BoundaryOperation, ...] = ()) -> None:
        self.review_operations = frozenset(review_operations)

    def classify(self, action: BoundaryAction) -> BoundaryDecision:
        disposition, reason = _RULES[action.operation]
        if (
            action.operation in self.review_operations
            and disposition is BoundaryDisposition.LOW_RISK
        ):
            disposition = BoundaryDisposition.REVIEW_REQUIRED
            reason = "operator policy requires review for this operation"
        return BoundaryDecision(
            action=action,
            disposition=disposition,
            rule_id=f"boundary.{action.operation.value}",
            reason=reason,
        )
