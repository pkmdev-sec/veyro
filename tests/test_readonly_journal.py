from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from foreman.models import (
    BridgeSource,
    EventProvenance,
    EventSensitivity,
    SessionIdentity,
    SupervisionEvent,
    SupervisionEventType,
)
from foreman.supervision import journal as journal_module
from foreman.supervision.broker import MAX_JOURNAL_ENTRY_BYTES, MAX_MESSAGE_BYTES
from foreman.supervision.journal import JournalError, ReadOnlyJournal, discover_journal_ids


@pytest.fixture
def journal_files(tmp_path: Path) -> tuple[Path, Path, SessionIdentity]:
    repository = tmp_path.resolve()
    directory = repository / ".foreman" / "supervision" / "session-1"
    for path in (directory.parent.parent, directory.parent, directory):
        path.mkdir(mode=0o700)
    identity = SessionIdentity(
        foreman_session_id=directory.name,
        provider_id="codex",
        provider_session_id="11111111-1111-4111-8111-111111111111",
        repository=str(repository),
        provider_version="0.154.0",
        bridge_id="codex-hooks",
        bridge_version="1.0.0",
    )
    write_private(directory / "identity.json", identity.model_dump_json().encode())
    write_private(directory / "events.jsonl", b"")
    return repository, directory, identity


def write_private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def event_line(identity: SessionIdentity, sequence: int, **updates: object) -> bytes:
    event = SupervisionEvent(
        session=identity,
        sequence=sequence,
        event_type=SupervisionEventType.UNKNOWN,
        payload={"boundary_only": True},
        provenance=EventProvenance(source=BridgeSource.HOOK, native_event_type="Stop"),
    ).model_copy(update=updates)
    return event.model_dump_json().encode() + b"\n"


def snapshot(repository: Path) -> dict[str, tuple[int, int, bytes | None]]:
    return {
        str(path.relative_to(repository)): (
            path.stat().st_mode,
            path.stat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in repository.rglob("*")
    }


def test_readonly_discovery_and_paged_append(journal_files, monkeypatch):
    repository, directory, identity = journal_files
    events_path = directory / "events.jsonl"
    events_path.write_bytes(event_line(identity, 1) + event_line(identity, 2))
    for name in (
        "token",
        "capabilities.json",
        "controls.jsonl",
        "raw-events.jsonl",
        "settings.json",
    ):
        write_private(directory / name, b"must not read")
    before = snapshot(repository)
    original_open = os.open
    opened = []

    def checked_open(path, flags, *args, **kwargs):
        assert flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC) == 0
        assert str(path) not in {
            "token",
            "capabilities.json",
            "controls.jsonl",
            "raw-events.jsonl",
            "settings.json",
        }
        opened.append(str(path))
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", checked_open)
    assert discover_journal_ids(repository) == (["session-1"], False)
    reader = ReadOnlyJournal.open(repository, identity.foreman_session_id)
    try:
        assert reader.identity == identity
        assert reader.sequence == 0
        assert not reader.incomplete_tail
        assert [event.sequence for event in reader.read_page(limit=1)] == [1]
        assert [event.sequence for event in reader.read_page(limit=1)] == [2]
        assert reader.read_page() == []
        assert reader.sequence == 2
        assert snapshot(repository) == before
        with events_path.open("ab") as writer:
            writer.write(event_line(identity, 3))
        assert [event.sequence for event in reader.read_page()] == [3]
        assert reader.read_page() == []
        assert set(opened) == {
            str(repository),
            ".foreman",
            "supervision",
            "session-1",
            "identity.json",
            "events.jsonl",
        }
    finally:
        reader.close()
        reader.close()
    with pytest.raises(JournalError, match="closed"):
        reader.read_page()


def test_partial_tail_is_withheld_until_newline(journal_files):
    repository, directory, identity = journal_files
    path = directory / "events.jsonl"
    first = event_line(identity, 1)
    second = event_line(identity, 2)
    path.write_bytes(first + second[:20])
    reader = ReadOnlyJournal.open(repository, identity.foreman_session_id)
    try:
        assert [event.sequence for event in reader.read_page()] == [1]
        assert reader.sequence == 1
        assert reader.incomplete_tail
        assert reader.read_page() == []
        assert reader.incomplete_tail
        with path.open("ab") as writer:
            writer.write(second[20:-1])
        assert reader.read_page() == []
        assert reader.incomplete_tail
        with path.open("ab") as writer:
            writer.write(b"\n")
        assert [event.sequence for event in reader.read_page()] == [2]
        assert not reader.incomplete_tail
    finally:
        reader.close()


@pytest.mark.parametrize("bad_line", [b"\n", b"SECRET\n", b"{}\n", b"\xff\n"])
def test_malformed_complete_line_fails_closed(journal_files, bad_line):
    repository, directory, identity = journal_files
    (directory / "events.jsonl").write_bytes(event_line(identity, 1) + bad_line)
    reader = ReadOnlyJournal.open(repository, identity.foreman_session_id)
    assert len(reader.read_page(limit=1)) == 1
    with pytest.raises(JournalError) as caught:
        reader.read_page()
    assert "SECRET" not in str(caught.value)
    assert caught.value.__suppress_context__
    with pytest.raises(JournalError, match="closed"):
        reader.read_page()


@pytest.mark.parametrize("violation", ["gap", "duplicate", "foreign", "redacted", "content"])
def test_rejects_event_identity_sequence_and_sensitivity(journal_files, violation):
    repository, directory, identity = journal_files
    updates = {}
    sequence = 2
    if violation == "gap":
        sequence = 3
    elif violation == "duplicate":
        sequence = 1
    elif violation == "foreign":
        updates["session"] = identity.model_copy(update={"provider_session_id": "other"})
    else:
        updates["sensitivity"] = (
            EventSensitivity.REDACTED
            if violation == "redacted"
            else EventSensitivity.CONTENT_OPT_IN
        )
    (directory / "events.jsonl").write_bytes(
        event_line(identity, 1) + event_line(identity, sequence, **updates)
    )
    reader = ReadOnlyJournal.open(repository, identity.foreman_session_id)
    with pytest.raises(JournalError):
        reader.read_page()
    reader.close()


@pytest.mark.parametrize("filename", ["identity.json", "events.jsonl"])
def test_rejects_oversized_actual_file_reads(journal_files, filename):
    repository, directory, identity = journal_files
    maximum = MAX_MESSAGE_BYTES if filename == "identity.json" else MAX_JOURNAL_ENTRY_BYTES
    (directory / filename).write_bytes(b"x" * (maximum + 1) + b"\n")
    if filename == "identity.json":
        with pytest.raises(JournalError):
            ReadOnlyJournal.open(repository, identity.foreman_session_id)
    else:
        reader = ReadOnlyJournal.open(repository, identity.foreman_session_id)
        with pytest.raises(JournalError, match="size limit"):
            reader.read_page()
        reader.close()


@pytest.mark.parametrize(
    "part", [".foreman", "supervision", "session-1", "identity.json", "events.jsonl"]
)
@pytest.mark.parametrize("violation", ["symlink", "mode", "owner"])
def test_rejects_unsafe_owned_paths_without_repair(journal_files, monkeypatch, part, violation):
    repository, directory, identity = journal_files
    paths = {
        ".foreman": directory.parent.parent,
        "supervision": directory.parent,
        "session-1": directory,
        "identity.json": directory / "identity.json",
        "events.jsonl": directory / "events.jsonl",
    }
    path = paths[part]
    if violation == "symlink":
        moved = path.with_name(path.name + "-real")
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=moved.is_dir())
    elif violation == "mode":
        path.chmod(0o755 if path.is_dir() else 0o644)
    else:
        original_fstat = os.fstat
        target = path.stat().st_ino

        def foreign_owner(fd):
            info = original_fstat(fd)
            if info.st_ino == target:
                values = list(info)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return info

        monkeypatch.setattr(os, "fstat", foreign_owner)
    mode = path.lstat().st_mode
    with pytest.raises(JournalError):
        ReadOnlyJournal.open(repository, identity.foreman_session_id)
    assert path.lstat().st_mode == mode


@pytest.mark.parametrize("filename", ["identity.json", "events.jsonl"])
@pytest.mark.parametrize("kind", ["hardlink", "fifo", "directory"])
def test_rejects_nonregular_and_hardlinked_files(journal_files, filename, kind):
    repository, directory, identity = journal_files
    path = directory / filename
    if kind == "hardlink":
        os.link(path, directory / "second-link")
    else:
        path.unlink()
        if kind == "fifo":
            os.mkfifo(path, 0o600)
        else:
            path.mkdir(mode=0o700)
    with pytest.raises(JournalError):
        ReadOnlyJournal.open(repository, identity.foreman_session_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("foreman_session_id", "other"),
        ("repository", "/other"),
        ("repository", "relative"),
    ],
)
def test_requested_identity_is_exact(journal_files, field, value):
    repository, directory, identity = journal_files
    (directory / "identity.json").write_text(
        identity.model_copy(update={field: value}).model_dump_json()
    )
    with pytest.raises(JournalError, match="identity"):
        ReadOnlyJournal.open(repository, "session-1")


@pytest.mark.parametrize("selector", ["../session-1", "a/b", ".", "..", "", "a" * 201])
def test_rejects_invalid_selectors(journal_files, selector):
    repository, _, _ = journal_files
    with pytest.raises(JournalError, match="session id"):
        ReadOnlyJournal.open(repository, selector)


@pytest.mark.parametrize("change", ["replace", "truncate", "parent", "permissions", "symlink"])
def test_follow_fails_closed_on_file_changes(journal_files, change):
    repository, directory, identity = journal_files
    path = directory / "events.jsonl"
    path.write_bytes(event_line(identity, 1) + event_line(identity, 2))
    reader = ReadOnlyJournal.open(repository, identity.foreman_session_id)
    assert len(reader.read_page(limit=1)) == 1
    if change == "truncate":
        path.write_bytes(b"")
    elif change == "replace":
        replacement = directory / "replacement"
        write_private(replacement, event_line(identity, 2))
        replacement.replace(path)
    elif change == "parent":
        directory.rename(directory.with_name("old"))
        directory.mkdir(mode=0o700)
    elif change == "symlink":
        path.rename(directory / "old")
        path.symlink_to(directory / "old")
    else:
        path.chmod(0o644)
    with pytest.raises(JournalError):
        reader.read_page()
    with pytest.raises(JournalError, match="closed"):
        reader.read_page()


def test_all_descriptors_close_after_init_failure(journal_files, monkeypatch):
    repository, directory, identity = journal_files
    (directory / "identity.json").write_bytes(b"SECRET")
    real_open, real_close = os.open, os.close
    live = set()

    def tracked_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        live.add(fd)
        return fd

    def tracked_close(fd):
        live.remove(fd)
        return real_close(fd)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
    with pytest.raises(JournalError) as caught:
        ReadOnlyJournal.open(repository, identity.foreman_session_id)
    assert not live
    assert "SECRET" not in str(caught.value)


def test_repository_alias_is_canonicalized(journal_files, tmp_path):
    repository, _, identity = journal_files
    alias = tmp_path / "alias"
    alias.symlink_to(repository, target_is_directory=True)
    reader = ReadOnlyJournal.open(alias, identity.foreman_session_id)
    reader.close()
    assert discover_journal_ids(alias) == (["session-1"], False)


def test_discovery_missing_root_is_empty_but_unsafe_root_fails(tmp_path):
    assert discover_journal_ids(tmp_path) == ([], False)
    root = tmp_path / ".foreman"
    root.mkdir(mode=0o700)
    assert discover_journal_ids(tmp_path) == ([], False)
    root.chmod(0o755)
    with pytest.raises(JournalError):
        discover_journal_ids(tmp_path)


def test_discovery_is_sorted_bounded_and_leaves_identity_validation_to_caller(journal_files):
    repository, directory, _ = journal_files
    for name in ("z-session", "a-session", "bad name"):
        (directory.parent / name).mkdir(mode=0o700)
    (directory.parent / "unsafe-session").symlink_to(directory, target_is_directory=True)
    names, truncated = discover_journal_ids(repository)
    assert names == ["a-session", "session-1", "unsafe-session", "z-session"]
    assert not truncated
    names, truncated = discover_journal_ids(repository, limit=2)
    assert len(names) <= 2
    assert names == sorted(names)
    assert truncated


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5])
def test_bounded_limits(journal_files, limit):
    repository, _, identity = journal_files
    with pytest.raises(JournalError, match="limit"):
        discover_journal_ids(repository, limit=limit)
    reader = ReadOnlyJournal.open(repository, identity.foreman_session_id)
    try:
        with pytest.raises(JournalError, match="limit"):
            reader.read_page(limit=limit)
    finally:
        reader.close()


def test_metadata_open_replacement_is_rejected_and_descriptors_closed(journal_files, monkeypatch):
    repository, directory, identity = journal_files
    real_read = os.read
    replaced = False

    def replace_during_read(fd, count):
        nonlocal replaced
        data = real_read(fd, count)
        if not replaced:
            replacement = directory / "replacement"
            write_private(replacement, identity.model_dump_json().encode())
            replacement.replace(directory / "identity.json")
            replaced = True
        return data

    monkeypatch.setattr(journal_module.os, "read", replace_during_read)
    with pytest.raises(JournalError, match="replaced|private"):
        ReadOnlyJournal.open(repository, identity.foreman_session_id)


def append_from_process(path: str, line: bytes) -> None:
    with open(path, "ab") as writer:
        writer.write(line)


def test_follow_observes_a_separate_process_append(journal_files):
    repository, directory, identity = journal_files
    reader = ReadOnlyJournal.open(repository, identity.foreman_session_id)
    process = multiprocessing.get_context("spawn").Process(
        target=append_from_process,
        args=(str(directory / "events.jsonl"), event_line(identity, 1)),
    )
    try:
        assert reader.read_page() == []
        process.start()
        process.join(timeout=5)
        assert process.exitcode == 0
        assert [event.sequence for event in reader.read_page()] == [1]
        assert reader.read_page() == []
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        reader.close()


def test_reads_are_bounded_and_never_rewind(journal_files, monkeypatch):
    repository, directory, identity = journal_files
    data = b"".join(event_line(identity, sequence) for sequence in range(1, 101))
    (directory / "events.jsonl").write_bytes(data)
    real_read = os.read
    total = 0

    def bounded_read(fd, count):
        nonlocal total
        assert 0 < count <= 8192
        chunk = real_read(fd, count)
        total += len(chunk)
        return chunk

    def no_seek(*args):
        pytest.fail("reader must not rewind")

    reader = ReadOnlyJournal.open(repository, identity.foreman_session_id)
    monkeypatch.setattr(os, "read", bounded_read)
    monkeypatch.setattr(os, "lseek", no_seek)
    try:
        for sequence in range(1, 101):
            assert [event.sequence for event in reader.read_page(limit=1)] == [sequence]
        assert reader.read_page() == []
        assert total == len(data)
    finally:
        reader.close()
