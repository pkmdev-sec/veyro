from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import stat
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from veyro.models import (
    AuthorizationOutcome,
    AuthorizationReason,
    ControlEffectStatus,
    SessionIdentity,
    SupervisionEventType,
)
from veyro.supervision.delivery import DeliveryClaim, DeliveryError, DeliveryLedger

DIGEST = hashlib.sha256(b"control text SECRET credential").hexdigest()


def identity(repository: Path, **updates: object) -> SessionIdentity:
    return SessionIdentity(
        veyro_session_id="veyro-secret",
        provider_id="provider-secret",
        provider_session_id="native-secret",
        repository=str(repository),
        bridge_id="bridge-secret",
        bridge_version="1",
    ).model_copy(update=updates)


@pytest.fixture
def scope(tmp_path: Path) -> tuple[Path, SessionIdentity]:
    repository = tmp_path.resolve()
    return repository / "private" / "delivery", identity(repository)


def claim(
    ledger: DeliveryLedger, session: SessionIdentity, **updates: str
) -> DeliveryClaim:
    args = {"command_id": "command-secret", "request_sha256": DIGEST, **updates}
    return ledger.claim(session=session, **args)


def receipt(ledger: DeliveryLedger, session: SessionIdentity) -> DeliveryClaim:
    ledger.record_authorization(
        session=session,
        authorization_sha256="a" * 64,
        outcome=AuthorizationOutcome.AUTHORIZED,
        reason=AuthorizationReason.AUTHORIZED,
    )
    delivery_claim = claim(ledger, session)
    ledger.commit_attempt(
        delivery_claim,
        authorization_sha256="a" * 64,
        result_sha256="b" * 64,
        effect=ControlEffectStatus.ACKNOWLEDGED_UNVERIFIED,
    )
    return delivery_claim

def test_authorization_decision_is_sanitized_idempotent_and_validated(scope):
    root, session = scope
    ledger = DeliveryLedger(root)
    for _ in range(2):
        ledger.record_authorization(
            session=session,
            authorization_sha256=DIGEST,
            outcome=AuthorizationOutcome.HUMAN_APPROVAL_REQUIRED,
            reason=AuthorizationReason.HUMAN_APPROVAL_REQUIRED,
        )
    (path,) = root.iterdir()
    assert path.name.endswith(".decision.json")
    data = path.read_bytes()
    assert b"secret" not in data and b"SECRET" not in data
    assert json.loads(data)["outcome"] == "human_approval_required"
    DeliveryLedger(root)

    value = json.loads(data)
    value["outcome"] = "authorized"
    path.write_text(json.dumps(value))
    with pytest.raises(DeliveryError):
        DeliveryLedger(root)


def _contend(root: Path, session: SessionIdentity) -> bool:
    try:
        claim(DeliveryLedger(root), session)
    except DeliveryError:
        return False
    return True


def _crash_after_claim(root: Path, session: SessionIdentity) -> None:
    claim(DeliveryLedger(root), session)
    os._exit(23)


def test_private_bounded_digest_only_record(scope):
    root, session = scope
    ledger = DeliveryLedger(root)
    delivery_claim = claim(ledger, session)
    assert delivery_claim.delivery_sha256 and delivery_claim.claim_sha256
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(root.parent.stat().st_mode) == 0o700
    (record_path,) = root.iterdir()
    info = record_path.stat()
    assert stat.S_ISREG(info.st_mode)
    assert info.st_nlink == 1
    assert info.st_uid == os.getuid()
    assert stat.S_IMODE(info.st_mode) == 0o600
    data = record_path.read_bytes()
    assert len(data) < 512
    assert b"secret" not in data and b"SECRET" not in data
    assert str(root.parent).encode() not in data
    assert b"control text" not in data and b"credential" not in data
    record = json.loads(data)
    assert set(record) == {
        "version",
        "kind",
        "delivery_sha256",
        "scope_sha256",
        "command_sha256",
        "request_sha256",
        "claimed_at",
    }
    assert record["request_sha256"] == DIGEST
    assert record_path.name == f"{delivery_claim.delivery_sha256}.claim.json"
    before = record_path.read_bytes()
    with pytest.raises(DeliveryError, match="already claimed"):
        claim(ledger, session)
    assert record_path.read_bytes() == before


def test_attempt_receipt_is_exactly_once_sanitized_and_reopenable(scope):
    root, session = scope
    ledger = DeliveryLedger(root)
    delivery_claim = receipt(ledger, session)
    records = {path.name: path.read_bytes() for path in root.iterdir()}
    assert f"{delivery_claim.delivery_sha256}.claim.json" in records
    assert f"{delivery_claim.delivery_sha256}.attempt.json" in records
    assert len([name for name in records if name.endswith(".decision.json")]) == 1
    attempt = records[f"{delivery_claim.delivery_sha256}.attempt.json"]
    assert b"secret" not in attempt and b"SECRET" not in attempt
    assert b"control text" not in attempt and b"credential" not in attempt
    assert json.loads(attempt)["effect"] == "acknowledged_unverified"
    with pytest.raises(DeliveryError, match="already exists"):
        ledger.commit_attempt(
            delivery_claim,
            authorization_sha256="a" * 64,
            result_sha256="b" * 64,
            effect=ControlEffectStatus.ACKNOWLEDGED_UNVERIFIED,
        )
    DeliveryLedger(root)


@pytest.mark.parametrize("corruption", ["missing", "non_authorized"])
def test_attempt_requires_matching_authorized_decision_receipt(scope, corruption):
    root, session = scope
    receipt(DeliveryLedger(root), session)
    (decision,) = root.glob("*.decision.json")
    if corruption == "missing":
        decision.unlink()
    else:
        value = json.loads(decision.read_bytes())
        value["outcome"] = AuthorizationOutcome.DENIED.value
        value["reason"] = AuthorizationReason.BOUNDARY_FORBIDDEN.value
        decision.write_bytes(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
    with pytest.raises(DeliveryError, match="attempt record is invalid"):
        DeliveryLedger(root)


def test_not_applicable_attempt_requires_non_authorized_decision(scope):
    root, session = scope
    ledger = DeliveryLedger(root)
    authorization_sha256 = "c" * 64
    ledger.record_authorization(
        session=session,
        authorization_sha256=authorization_sha256,
        outcome=AuthorizationOutcome.DENIED,
        reason=AuthorizationReason.BOUNDARY_FORBIDDEN,
    )
    delivery_claim = claim(ledger, session)
    ledger.commit_attempt(
        delivery_claim,
        authorization_sha256=authorization_sha256,
        result_sha256=None,
        effect=ControlEffectStatus.NOT_APPLICABLE,
    )
    (decision,) = root.glob("*.decision.json")
    value = json.loads(decision.read_bytes())
    value["outcome"] = AuthorizationOutcome.AUTHORIZED.value
    value["reason"] = AuthorizationReason.AUTHORIZED.value
    decision.write_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    with pytest.raises(DeliveryError, match="attempt record is invalid"):
        DeliveryLedger(root)


def test_final_effect_receipt_is_immutable_and_bound_to_attempt(scope):
    root, session = scope
    ledger = DeliveryLedger(root)
    delivery_claim = receipt(ledger, session)
    ledger.commit_final_effect(
        session=session,
        command_id="command-secret",
        effect=ControlEffectStatus.VERIFIED,
        native_event=SupervisionEventType.SESSION_FAILED,
    )
    effect_path = root / f"{delivery_claim.delivery_sha256}.effect.json"
    final_data = effect_path.read_bytes()
    assert b"secret" not in final_data and b"SECRET" not in final_data
    assert json.loads(final_data)["effect"] == "verified"
    assert json.loads(final_data)["native_event"] == "session_failed"
    with pytest.raises(DeliveryError, match="already exists"):
        ledger.commit_final_effect(
            session=session,
            command_id="command-secret",
            effect=ControlEffectStatus.UNKNOWN,
        )
    DeliveryLedger(root)

@pytest.mark.parametrize("corruption", ["content", "public", "symlink", "hardlink"])
def test_reopen_fails_closed_for_corrupt_or_unsafe_receipt(scope, corruption):
    root, session = scope
    delivery_claim = receipt(DeliveryLedger(root), session)
    path = root / f"{delivery_claim.delivery_sha256}.attempt.json"
    target = root.parent / "receipt-target"
    target.write_text("SECRET")
    if corruption == "content":
        path.write_text("{}")
    elif corruption == "public":
        path.chmod(0o644)
    else:
        path.unlink()
        if corruption == "symlink":
            path.symlink_to(target)
        else:
            path.hardlink_to(target)
    with pytest.raises(DeliveryError):
        DeliveryLedger(root)
    assert target.read_text() == "SECRET"



def test_restart_reconnect_and_changed_digest_never_retry(scope):
    root, session = scope
    claim(DeliveryLedger(root), session)
    reconnected = session.model_copy(
        update={
            "veyro_session_id": "fresh-veyro",
            "started_at": datetime.now(UTC) + timedelta(days=1),
            "provider_version": "new",
            "bridge_id": "different-bridge",
            "bridge_version": "new",
        }
    )
    with pytest.raises(DeliveryError, match="already claimed"):
        claim(DeliveryLedger(root), reconnected, request_sha256="f" * 64)
    assert len(list(root.iterdir())) == 1


def test_repository_aliases_share_scope(scope):
    root, session = scope
    alias = root.parent.parent / "alias"
    alias.symlink_to(Path(session.repository), target_is_directory=True)
    claim(DeliveryLedger(root), session)
    with pytest.raises(DeliveryError, match="already claimed"):
        claim(DeliveryLedger(root), session.model_copy(update={"repository": str(alias)}))


@pytest.mark.parametrize(
    "dimension", ["provider_id", "provider_session_id", "repository", "command"]
)
def test_different_scopes_can_claim_independently(scope, dimension):
    root, session = scope
    ledger = DeliveryLedger(root)
    claim(ledger, session)
    if dimension == "command":
        claim(ledger, session, command_id="other")
    else:
        value = "other"
        if dimension == "repository":
            repository = root.parent / "other-repository"
            repository.mkdir()
            value = str(repository)
        claim(ledger, session.model_copy(update={dimension: value}))
    assert len(list(root.iterdir())) == 2


def test_thread_contention_has_exactly_one_winner(scope):
    root, session = scope
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _contend(root, session), range(32)))
    assert sum(results) == 1
    assert len(list(root.iterdir())) == 1


def test_process_contention_has_exactly_one_winner(scope):
    root, session = scope
    with ProcessPoolExecutor(
        max_workers=4, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        futures = [pool.submit(_contend, root, session) for _ in range(16)]
        results = [future.result(timeout=30) for future in futures]
    assert sum(results) == 1
    assert len(list(root.iterdir())) == 1


def test_process_crash_keeps_unknown_claim_and_blocks_retry(scope):
    root, session = scope
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_after_claim, args=(root, session)
    )
    process.start()
    process.join(timeout=30)
    try:
        assert process.exitcode == 23
    finally:
        if process.is_alive():
            process.kill()
            process.join()
    (claim_path,) = root.iterdir()
    assert claim_path.name.endswith(".claim.json")
    with pytest.raises(DeliveryError, match="already claimed"):
        claim(DeliveryLedger(root), session)


@pytest.mark.parametrize("native_id", [None, "", "   "])
def test_native_session_identity_required(scope, native_id):
    root, session = scope
    ledger = DeliveryLedger(root)
    with pytest.raises(DeliveryError):
        claim(ledger, session.model_copy(update={"provider_session_id": native_id}))
    assert not list(root.iterdir())


@pytest.mark.parametrize(
    "args",
    [
        {"command_id": ""},
        {"command_id": "x" * 201},
        {"request_sha256": "SECRET"},
        {"request_sha256": "f" * 65},
    ],
)
def test_invalid_claim_has_safe_error_and_no_record(scope, args):
    root, session = scope
    ledger = DeliveryLedger(root)
    with pytest.raises(DeliveryError) as caught:
        claim(ledger, session, **args)
    assert str(caught.value) == "invalid delivery claim"
    assert not list(root.iterdir())


@pytest.mark.parametrize(
    "unsafe", ["public", "file", "symlink", "ancestor-symlink", "ancestor-write"]
)
def test_unsafe_root_is_rejected(scope, unsafe):
    root, _ = scope
    root.parent.mkdir(mode=0o700)
    if unsafe == "public":
        root.mkdir(mode=0o755)
    elif unsafe == "file":
        root.write_text("SECRET")
    elif unsafe == "symlink":
        root.symlink_to(root.parent, target_is_directory=True)
    elif unsafe == "ancestor-symlink":
        alias = root.parent.parent / "alias"
        alias.symlink_to(root.parent, target_is_directory=True)
        root = alias / root.name
    else:
        root.parent.chmod(0o777)
    with pytest.raises(DeliveryError) as caught:
        DeliveryLedger(root)
    assert "SECRET" not in str(caught.value)


def test_foreign_owner_is_rejected(scope, monkeypatch):
    root, _ = scope
    DeliveryLedger(root)
    uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: uid + 1000)
    with pytest.raises(DeliveryError):
        DeliveryLedger(root)


@pytest.mark.parametrize("replacement", ["removed", "directory", "symlink", "public"])
def test_root_is_revalidated_before_each_claim(scope, replacement):
    root, session = scope
    ledger = DeliveryLedger(root)
    if replacement == "public":
        root.chmod(0o755)
    else:
        root.rename(root.with_name("old"))
        if replacement == "directory":
            root.mkdir(mode=0o700)
        elif replacement == "symlink":
            root.symlink_to(root.with_name("old"), target_is_directory=True)
    with pytest.raises(DeliveryError):
        claim(ledger, session)
    if replacement != "public":
        assert not list(root.with_name("old").iterdir())


@pytest.mark.parametrize(
    "existing", ["empty", "malformed", "public", "symlink", "hardlink", "fifo", "directory"]
)
def test_any_existing_claim_is_a_no_retry_fence(scope, existing):
    root, session = scope
    ledger = DeliveryLedger(root)
    claim(ledger, session)
    (path,) = root.iterdir()
    path.unlink()
    target = root.parent / "target"
    target.write_text("SECRET")
    if existing == "symlink":
        path.symlink_to(target)
    elif existing == "hardlink":
        path.hardlink_to(target)
    elif existing == "fifo":
        os.mkfifo(path, mode=0o600)
    elif existing == "directory":
        path.mkdir(mode=0o700)
    else:
        path.write_bytes(b"" if existing == "empty" else b"SECRET")
        path.chmod(0o644 if existing == "public" else 0o600)
    with pytest.raises(DeliveryError):
        DeliveryLedger(root)
    assert target.read_text() == "SECRET"


@pytest.mark.parametrize("failure", ["file-fsync", "directory-fsync", "write", "zero-write"])
def test_persistence_failure_never_succeeds_or_retries(scope, monkeypatch, failure):
    root, session = scope
    ledger = DeliveryLedger(root)
    real_fsync = os.fsync

    def fail_fsync(fd):
        is_file = stat.S_ISREG(os.fstat(fd).st_mode)
        if (failure == "file-fsync" and is_file) or (failure == "directory-fsync" and not is_file):
            raise OSError("SECRET storage detail")
        return real_fsync(fd)

    def fail_write(fd, data):
        if failure == "zero-write":
            return 0
        raise OSError("SECRET storage detail")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_fsync)
        if failure in {"write", "zero-write"}:
            patch.setattr(os, "write", fail_write)
        with pytest.raises(DeliveryError, match="could not be persisted") as caught:
            claim(ledger, session)
        assert "SECRET" not in str(caught.value)
    assert len(list(root.iterdir())) == 1
    if failure in {"file-fsync", "directory-fsync"}:
        reopened = DeliveryLedger(root)
        with pytest.raises(DeliveryError, match="already claimed"):
            claim(reopened, session)
    else:
        with pytest.raises(DeliveryError):
            DeliveryLedger(root)


def test_exclusive_nofollow_creation_and_fsync_order(scope, monkeypatch):
    root, session = scope
    ledger = DeliveryLedger(root)
    real_open, real_fsync, real_write = os.open, os.fsync, os.write
    events = []

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        if flags & os.O_CREAT:
            assert flags & os.O_EXCL and flags & os.O_NOFOLLOW
            assert mode == 0o600 and dir_fd is not None
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def tracked_fsync(fd):
        events.append("file" if stat.S_ISREG(os.fstat(fd).st_mode) else "directory")
        return real_fsync(fd)

    def short_write(fd, data):
        return real_write(fd, data[:7])

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "fsync", tracked_fsync)
    monkeypatch.setattr(os, "write", short_write)
    claim(ledger, session)
    assert events == ["file", "directory"]
    (path,) = root.iterdir()
    assert json.loads(path.read_bytes())["request_sha256"] == DIGEST


def test_constructor_fsync_failure_prevents_use(scope, monkeypatch):
    root, _ = scope

    def fail_fsync(fd):
        raise OSError("SECRET filesystem")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(DeliveryError, match="ledger is unavailable") as caught:
        DeliveryLedger(root)
    assert "SECRET" not in str(caught.value)


def test_maximum_inputs_still_produce_bounded_record(scope):
    root, session = scope
    session = session.model_copy(
        update={"provider_id": "p" * 100, "provider_session_id": "s" * 500}
    )
    claim(DeliveryLedger(root), session, command_id="c" * 200)
    (path,) = root.iterdir()
    assert path.stat().st_size < 512


def test_restrictive_umask_still_creates_owner_readable_claim(scope):
    root, session = scope
    ledger = DeliveryLedger(root)
    previous = os.umask(0o777)
    try:
        claim(ledger, session)
    finally:
        os.umask(previous)
    (path,) = root.iterdir()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_bytes())["request_sha256"] == DIGEST
