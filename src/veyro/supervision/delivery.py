from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from veyro.models import (
    AuthorizationOutcome,
    AuthorizationReason,
    ControlEffectStatus,
    SessionIdentity,
    SupervisionEventType,
)

_DIGEST = re.compile(r"[0-9a-f]{64}")
_RECORD_NAME = re.compile(
    r"(?P<key>[0-9a-f]{64})\.(?P<kind>decision|claim|attempt|effect)\.json"
)


class DeliveryError(RuntimeError):
    """A control delivery could not persist or validate its no-retry evidence."""


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    delivery_sha256: str
    claim_sha256: str


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _record(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _check_directory(info: os.stat_result, *, private: bool) -> None:
    if private:
        safe = info.st_uid == os.getuid() and not info.st_mode & 0o077
    else:
        safe = info.st_uid in {0, os.getuid()} and (
            not info.st_mode & 0o022 or (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
        )
    if not stat.S_ISDIR(info.st_mode) or not safe:
        raise DeliveryError("delivery ledger directory is unsafe")


@contextmanager
def _open_root(root: Path, *, create: bool) -> Iterator[list[int]]:
    descriptors: list[int] = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptors.append(os.open(root.anchor, flags))
        _check_directory(os.fstat(descriptors[0]), private=False)
        for index, name in enumerate(root.parts[1:], start=1):
            parent = descriptors[-1]
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            descriptor = os.open(name, flags, dir_fd=parent)
            descriptors.append(descriptor)
            _check_directory(os.fstat(descriptor), private=index == len(root.parts) - 1)
        yield descriptors
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _check_links(root: Path, descriptors: list[int]) -> None:
    for index, name in enumerate(root.parts[1:], start=1):
        info = os.fstat(descriptors[index])
        _check_directory(info, private=index == len(descriptors) - 1)
        linked = os.stat(name, dir_fd=descriptors[index - 1], follow_symlinks=False)
        if _identity(info) != _identity(linked):
            raise DeliveryError("delivery ledger directory was replaced")


def _check_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise DeliveryError("delivery ledger record is unsafe")


def _read_file(root_fd: int, filename: str) -> bytes:
    descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    try:
        info = os.fstat(descriptor)
        _check_file(info)
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 4097 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > 4096:
                raise DeliveryError("delivery ledger record is invalid")
        linked = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
        _check_file(linked)
        if _identity(info) != _identity(linked):
            raise DeliveryError("delivery ledger record was replaced")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _parse_record(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DeliveryError("delivery ledger record is invalid") from None
    if not isinstance(value, dict) or _record(value) != data:
        raise DeliveryError("delivery ledger record is invalid")
    return value


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _decision_matches(outcome: object, reason: object) -> bool:
    if outcome == AuthorizationOutcome.AUTHORIZED.value:
        return reason == AuthorizationReason.AUTHORIZED.value
    if outcome == AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED.value:
        return reason in {
            AuthorizationReason.HUMAN_APPROVAL_REQUIRED.value,
            AuthorizationReason.HUMAN_APPROVAL_INVALID.value,
        }
    return outcome == AuthorizationOutcome.DENIED.value and reason not in {
        AuthorizationReason.AUTHORIZED.value,
        AuthorizationReason.HUMAN_APPROVAL_REQUIRED.value,
        AuthorizationReason.HUMAN_APPROVAL_INVALID.value,
    }




class DeliveryLedger:
    """Persist immutable authorization decisions, claims, attempts, and effects."""

    def __init__(self, root: Path):
        try:
            self._root = root.absolute()
            if ".." in self._root.parts or len(self._root.parts) < 2:
                raise DeliveryError("delivery ledger directory is unsafe")
            with _open_root(self._root, create=True) as descriptors:
                self._root_identity = _identity(os.fstat(descriptors[-1]))
                for descriptor in reversed(descriptors):
                    os.fsync(descriptor)
                _check_links(self._root, descriptors)
                self._validate_records(descriptors[-1])
        except DeliveryError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise DeliveryError("delivery ledger is unavailable") from None

    def record_authorization(
        self,
        *,
        session: SessionIdentity,
        authorization_sha256: str,
        outcome: AuthorizationOutcome,
        reason: AuthorizationReason,
    ) -> None:
        if not _is_digest(authorization_sha256) or not _decision_matches(
            outcome.value, reason.value
        ):
            raise DeliveryError("invalid authorization decision")
        _, scope_sha256 = self._scope(session)
        key = _sha256(json.dumps([scope_sha256, authorization_sha256]).encode())
        data = _record(
            {
                "version": 2,
                "kind": "authorization_decision",
                "decision_sha256": key,
                "scope_sha256": scope_sha256,
                "authorization_sha256": authorization_sha256,
                "outcome": outcome.value,
                "reason": reason.value,
                "recorded_at": datetime.now(UTC).isoformat(),
            }
        )
        filename = key + ".decision.json"
        with self._opened_root() as (descriptors, root_fd):
            self._validate_records(root_fd)
            try:
                self._create_record_at(
                    descriptors, root_fd, filename, data, "authorization decision"
                )
            except DeliveryError as error:
                if str(error) != "authorization decision already exists":
                    raise
                existing = _parse_record(_read_file(root_fd, filename))
                if (
                    existing.get("authorization_sha256") != authorization_sha256
                    or existing.get("scope_sha256") != scope_sha256
                    or existing.get("outcome") != outcome.value
                    or existing.get("reason") != reason.value
                ):
                    raise DeliveryError(
                        "authorization decision receipt is inconsistent"
                    ) from error
            self._validate_records(root_fd)

    def claim(
        self, *, session: SessionIdentity, command_id: str, request_sha256: str
    ) -> DeliveryClaim:
        key, scope_sha256 = self._delivery_key(session, command_id)
        if not _is_digest(request_sha256):
            raise DeliveryError("invalid delivery claim")
        value: dict[str, object] = {
            "version": 2,
            "kind": "claim",
            "delivery_sha256": key,
            "scope_sha256": scope_sha256,
            "command_sha256": _sha256(command_id.encode()),
            "request_sha256": request_sha256,
            "claimed_at": datetime.now(UTC).isoformat(),
        }
        data = _record(value)
        self._create_record(key + ".claim.json", data, "delivery claim")
        return DeliveryClaim(delivery_sha256=key, claim_sha256=_sha256(data))

    def commit_attempt(
        self,
        claim: DeliveryClaim,
        *,
        authorization_sha256: str,
        result_sha256: str | None,
        effect: ControlEffectStatus,
    ) -> None:
        if (
            not _is_digest(claim.delivery_sha256)
            or not _is_digest(claim.claim_sha256)
            or not _is_digest(authorization_sha256)
            or (result_sha256 is not None and not _is_digest(result_sha256))
            or effect is ControlEffectStatus.VERIFIED
        ):
            raise DeliveryError("invalid delivery attempt")
        with self._opened_root() as (descriptors, root_fd):
            claim_data = _read_file(root_fd, claim.delivery_sha256 + ".claim.json")
            if _sha256(claim_data) != claim.claim_sha256:
                raise DeliveryError("delivery claim changed before receipt")
            data = _record(
                {
                    "version": 2,
                    "kind": "attempt",
                    "delivery_sha256": claim.delivery_sha256,
                    "claim_sha256": claim.claim_sha256,
                    "authorization_sha256": authorization_sha256,
                    "result_sha256": result_sha256,
                    "effect": effect.value,
                    "completed_at": datetime.now(UTC).isoformat(),
                }
            )
            self._create_record_at(
                descriptors,
                root_fd,
                claim.delivery_sha256 + ".attempt.json",
                data,
                "delivery attempt",
            )
            if _read_file(root_fd, claim.delivery_sha256 + ".claim.json") != claim_data:
                raise DeliveryError("delivery claim changed while writing receipt")
            self._validate_records(root_fd)

    def commit_final_effect(
        self,
        *,
        session: SessionIdentity,
        command_id: str,
        effect: ControlEffectStatus,
        native_event: SupervisionEventType | None = None,
    ) -> None:
        verified = effect is ControlEffectStatus.VERIFIED
        terminal = native_event in {
            SupervisionEventType.SESSION_COMPLETED,
            SupervisionEventType.SESSION_FAILED,
        }
        if verified != terminal or effect not in {
            ControlEffectStatus.VERIFIED,
            ControlEffectStatus.UNKNOWN,
        }:
            raise DeliveryError("invalid final delivery effect")
        key, _ = self._delivery_key(session, command_id)
        with self._opened_root() as (descriptors, root_fd):
            attempt_data = _read_file(root_fd, key + ".attempt.json")
            attempt = _parse_record(attempt_data)
            if attempt.get("effect") != ControlEffectStatus.ACKNOWLEDGED_UNVERIFIED.value:
                raise DeliveryError("delivery attempt cannot be finalized")
            data = _record(
                {
                    "version": 2,
                    "kind": "final_effect",
                    "delivery_sha256": key,
                    "attempt_sha256": _sha256(attempt_data),
                    "effect": effect.value,
                    "native_event": native_event.value if native_event is not None else None,
                    "completed_at": datetime.now(UTC).isoformat(),
                }
            )
            self._create_record_at(
                descriptors,
                root_fd,
                key + ".effect.json",
                data,
                "final delivery effect",
            )
            if _read_file(root_fd, key + ".attempt.json") != attempt_data:
                raise DeliveryError("delivery attempt changed while writing final effect")
            self._validate_records(root_fd)

    @contextmanager
    def _opened_root(self) -> Iterator[tuple[list[int], int]]:
        try:
            with _open_root(self._root, create=False) as descriptors:
                root_fd = descriptors[-1]
                if _identity(os.fstat(root_fd)) != self._root_identity:
                    raise DeliveryError("delivery ledger directory was replaced")
                _check_links(self._root, descriptors)
                yield descriptors, root_fd
        except DeliveryError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise DeliveryError("delivery ledger operation failed") from None

    def _scope(self, session: SessionIdentity) -> tuple[list[str], str]:
        if not session.provider_session_id or not session.provider_session_id.strip():
            raise DeliveryError("invalid delivery scope")
        try:
            repository = Path(session.repository).resolve(strict=True)
        except (OSError, RuntimeError):
            raise DeliveryError("invalid delivery repository") from None
        if not repository.is_dir():
            raise DeliveryError("invalid delivery repository")
        scope = [str(repository), session.provider_id, session.provider_session_id]
        return scope, _sha256(json.dumps(scope).encode())

    def _delivery_key(self, session: SessionIdentity, command_id: str) -> tuple[str, str]:
        if not 1 <= len(command_id) <= 200:
            raise DeliveryError("invalid delivery claim")
        scope, scope_sha256 = self._scope(session)
        return _sha256(json.dumps([*scope, command_id]).encode()), scope_sha256

    def _create_record(self, filename: str, data: bytes, label: str) -> None:
        try:
            with self._opened_root() as (descriptors, root_fd):
                self._validate_records(root_fd)
                self._create_record_at(descriptors, root_fd, filename, data, label)
                self._validate_records(root_fd)
        except DeliveryError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise DeliveryError(f"{label} could not be persisted") from None

    def _create_record_at(
        self,
        descriptors: list[int],
        root_fd: int,
        filename: str,
        data: bytes,
        label: str,
    ) -> None:
        try:
            descriptor = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=root_fd,
            )
        except FileExistsError:
            message = (
                "control command already claimed"
                if filename.endswith(".claim.json")
                else f"{label} already exists"
            )
            raise DeliveryError(message) from None
        try:
            info = os.fstat(descriptor)
            _check_file(info)
            os.fchmod(descriptor, 0o600)
            remaining = memoryview(data)
            while remaining:
                written = os.write(descriptor, remaining)
                if written == 0:
                    raise DeliveryError(f"{label} could not be persisted")
                remaining = remaining[written:]
            os.fsync(descriptor)
            linked = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
            _check_file(linked)
            if _identity(info) != _identity(linked):
                raise DeliveryError(f"{label} file was replaced")
            os.fsync(root_fd)
            _check_links(self._root, descriptors)
        except DeliveryError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise DeliveryError(f"{label} could not be persisted") from None
        finally:
            os.close(descriptor)

    def _validate_records(self, root_fd: int) -> None:
        decisions: dict[str, bytes] = {}
        claims: dict[str, bytes] = {}
        attempts: dict[str, bytes] = {}
        effects: dict[str, bytes] = {}
        try:
            names = os.listdir(root_fd)
        except OSError:
            raise DeliveryError("delivery ledger is unavailable") from None
        records = {
            "decision": decisions,
            "claim": claims,
            "attempt": attempts,
            "effect": effects,
        }
        for filename in names:
            matched = _RECORD_NAME.fullmatch(filename)
            if matched is None:
                raise DeliveryError("delivery ledger contains an invalid record")
            data = _read_file(root_fd, filename)
            value = _parse_record(data)
            key = matched.group("key")
            kind = matched.group("kind")
            expected_kind = {
                "decision": "authorization_decision",
                "effect": "final_effect",
            }.get(kind, kind)
            identity_field = "decision_sha256" if kind == "decision" else "delivery_sha256"
            if value.get(identity_field) != key or value.get("kind") != expected_kind:
                raise DeliveryError("delivery ledger record is invalid")
            records[kind][key] = data

        for data in decisions.values():
            value = _parse_record(data)
            if set(value) != {
                "version",
                "kind",
                "decision_sha256",
                "scope_sha256",
                "authorization_sha256",
                "outcome",
                "reason",
                "recorded_at",
            } or not (
                value["version"] == 2
                and _is_digest(value["decision_sha256"])
                and _is_digest(value["scope_sha256"])
                and _is_digest(value["authorization_sha256"])
                and value["outcome"] in {item.value for item in AuthorizationOutcome}
                and value["decision_sha256"]
                == _sha256(
                    json.dumps(
                        [value["scope_sha256"], value["authorization_sha256"]]
                    ).encode()
                )
                and value["reason"] in {item.value for item in AuthorizationReason}
                and _decision_matches(value["outcome"], value["reason"])
                and _timestamp(value["recorded_at"])
            ):
                raise DeliveryError("authorization decision record is invalid")

        for data in claims.values():
            value = _parse_record(data)
            if set(value) != {
                "version",
                "kind",
                "delivery_sha256",
                "scope_sha256",
                "command_sha256",
                "request_sha256",
                "claimed_at",
            } or not (
                value["version"] == 2
                and all(
                    _is_digest(value[field])
                    for field in (
                        "delivery_sha256",
                        "scope_sha256",
                        "command_sha256",
                        "request_sha256",
                    )
                )
                and _timestamp(value["claimed_at"])
            ):
                raise DeliveryError("delivery claim record is invalid")

        for key, data in attempts.items():
            value = _parse_record(data)
            result_sha256 = value.get("result_sha256")
            claim_value = _parse_record(claims[key]) if key in claims else {}
            authorization_sha256 = value.get("authorization_sha256")
            decision_key = (
                _sha256(
                    json.dumps(
                        [claim_value.get("scope_sha256"), authorization_sha256]
                    ).encode()
                )
                if _is_digest(claim_value.get("scope_sha256"))
                and _is_digest(authorization_sha256)
                else ""
            )
            decision_value = (
                _parse_record(decisions[decision_key]) if decision_key in decisions else {}
            )
            if set(value) != {
                "version",
                "kind",
                "delivery_sha256",
                "claim_sha256",
                "authorization_sha256",
                "result_sha256",
                "effect",
                "completed_at",
            } or not (
                value["version"] == 2
                and key in claims
                and value["claim_sha256"] == _sha256(claims[key])
                and _is_digest(value["authorization_sha256"])
                and decision_value.get("scope_sha256") == claim_value.get("scope_sha256")
                and (
                    decision_value.get("outcome") == AuthorizationOutcome.AUTHORIZED.value
                )
                == (value["effect"] != ControlEffectStatus.NOT_APPLICABLE.value)
                and (result_sha256 is None or _is_digest(result_sha256))
                and value["effect"]
                in {
                    ControlEffectStatus.ACKNOWLEDGED_UNVERIFIED.value,
                    ControlEffectStatus.NOT_APPLICABLE.value,
                    ControlEffectStatus.FAILED.value,
                    ControlEffectStatus.UNKNOWN.value,
                }
                and ((result_sha256 is not None) == (
                    value["effect"]
                    in {
                        ControlEffectStatus.ACKNOWLEDGED_UNVERIFIED.value,
                        ControlEffectStatus.FAILED.value,
                    }
                ))
                and _timestamp(value["completed_at"])
            ):
                raise DeliveryError("delivery attempt record is invalid")

        for key, data in effects.items():
            value = _parse_record(data)
            if set(value) != {
                "version",
                "kind",
                "delivery_sha256",
                "attempt_sha256",
                "effect",
                "native_event",
                "completed_at",
            } or not (
                value["version"] == 2
                and key in attempts
                and value["attempt_sha256"] == _sha256(attempts[key])
                and _parse_record(attempts[key])["effect"]
                == ControlEffectStatus.ACKNOWLEDGED_UNVERIFIED.value
                and value["effect"]
                in {ControlEffectStatus.VERIFIED.value, ControlEffectStatus.UNKNOWN.value}
                and (
                    value["native_event"]
                    in {
                        SupervisionEventType.SESSION_COMPLETED.value,
                        SupervisionEventType.SESSION_FAILED.value,
                    }
                )
                == (value["effect"] == ControlEffectStatus.VERIFIED.value)
                and _timestamp(value["completed_at"])
            ):
                raise DeliveryError("final delivery effect record is invalid")

        if set(attempts) - set(claims) or set(effects) - set(attempts):
            raise DeliveryError("delivery ledger contains an orphaned record")
