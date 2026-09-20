from __future__ import annotations

import os
import stat
from pathlib import Path

from pydantic import ValidationError

from veyro.models import EventSensitivity, SessionIdentity, SupervisionEvent
from veyro.supervision.broker import _SESSION_ID, MAX_JOURNAL_ENTRY_BYTES, MAX_MESSAGE_BYTES


class JournalError(RuntimeError):
    """Raised when a local journal cannot be observed safely."""


def _validate_private(info: os.stat_result, *, directory: bool) -> None:
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not valid_type
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or (not directory and info.st_nlink != 1)
    ):
        raise JournalError("journal path is not private and owned")


def _check_link(parent: int, name: str, descriptor: int, *, directory: bool) -> os.stat_result:
    info = os.fstat(descriptor)
    linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _validate_private(info, directory=directory)
    _validate_private(linked, directory=directory)
    if (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino):
        raise JournalError("journal path was replaced")
    return info


def _open_private(parent: int, name: str, *, directory: bool) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    descriptor = os.open(name, flags, dir_fd=parent)
    try:
        _check_link(parent, name, descriptor, directory=directory)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_root(repository: Path, descriptors: list[int]) -> None:
    descriptors.append(os.open(repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
    for name in (".veyro", "supervision"):
        descriptors.append(_open_private(descriptors[-1], name, directory=True))


def _read_metadata(descriptor: int) -> bytes:
    data = bytearray()
    while len(data) <= MAX_MESSAGE_BYTES:
        chunk = os.read(descriptor, min(8192, MAX_MESSAGE_BYTES + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    raise JournalError("journal metadata exceeds size limit")


class ReadOnlyJournal:
    """Incrementally observe a private journal without opening its control files."""

    def __init__(self, identity: SessionIdentity, descriptors: list[int]) -> None:
        self.identity = identity
        self.sequence = 0
        self.incomplete_tail = False
        self._descriptors = descriptors
        self._buffer = b""
        self._offset = 0
        self._size = os.fstat(descriptors[-1]).st_size

    @classmethod
    def open(cls, repository: Path, veyro_session_id: str) -> ReadOnlyJournal:
        if not _SESSION_ID.fullmatch(veyro_session_id):
            raise JournalError("invalid Veyro session id")
        descriptors: list[int] = []
        try:
            repository = repository.resolve(strict=True)
            _open_root(repository, descriptors)
            descriptors.append(_open_private(descriptors[-1], veyro_session_id, directory=True))
            session_fd = descriptors[-1]
            identity_fd = _open_private(session_fd, "identity.json", directory=False)
            try:
                identity = SessionIdentity.model_validate_json(_read_metadata(identity_fd))
                _check_link(session_fd, "identity.json", identity_fd, directory=False)
            finally:
                os.close(identity_fd)
            if identity.veyro_session_id != veyro_session_id or identity.repository != str(
                repository
            ):
                raise JournalError("journal identity does not match the requested session")
            descriptors.append(_open_private(session_fd, "events.jsonl", directory=False))
            journal = cls(identity, descriptors)
            journal._check_unchanged()
        except BaseException as error:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            if isinstance(error, (OSError, ValueError, ValidationError)):
                raise JournalError("cannot open private journal") from None
            raise
        return journal

    def _check_unchanged(self) -> None:
        names = (".veyro", "supervision", self.identity.veyro_session_id, "events.jsonl")
        for index, name in enumerate(names):
            info = _check_link(
                self._descriptors[index],
                name,
                self._descriptors[index + 1],
                directory=index < 3,
            )
        if info.st_size < self._size or info.st_size < self._offset:
            raise JournalError("event journal was truncated")
        self._size = info.st_size

    def read_page(self, *, limit: int = 64) -> list[SupervisionEvent]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise JournalError("journal page limit must be between 1 and 1000")
        if not self._descriptors:
            raise JournalError("journal is closed")
        events: list[SupervisionEvent] = []
        self.incomplete_tail = False
        try:
            self._check_unchanged()
            while len(events) < limit:
                newline = self._buffer.find(b"\n")
                if newline >= 0:
                    line = self._buffer[:newline]
                    event = SupervisionEvent.model_validate_json(line)
                    if event.session != self.identity or event.sequence != self.sequence + 1:
                        raise JournalError("event journal identity or sequence is malformed")
                    if event.sensitivity is not EventSensitivity.METADATA:
                        raise JournalError("event journal contains nonmetadata content")
                    self._buffer = self._buffer[newline + 1 :]
                    self.sequence = event.sequence
                    events.append(event)
                    continue
                if len(self._buffer) > MAX_JOURNAL_ENTRY_BYTES:
                    raise JournalError("event journal entry exceeds size limit")
                chunk = os.read(
                    self._descriptors[-1],
                    min(8192, MAX_JOURNAL_ENTRY_BYTES + 1 - len(self._buffer)),
                )
                if not chunk:
                    self.incomplete_tail = bool(self._buffer)
                    break
                self._offset += len(chunk)
                self._buffer += chunk
            self._check_unchanged()
        except (OSError, ValueError, ValidationError, JournalError) as error:
            self.close()
            if isinstance(error, JournalError):
                raise
            raise JournalError("event journal is unreadable or malformed") from None
        return events

    def close(self) -> None:
        """Release only this reader's descriptors, never the writer's resources."""
        for descriptor in reversed(self._descriptors):
            os.close(descriptor)
        self._descriptors = []


def discover_journal_ids(repository: Path, *, limit: int = 1000) -> tuple[list[str], bool]:
    """Scan a bounded number of direct entries; callers validate each candidate."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise JournalError("journal discovery limit must be between 1 and 1000")
    descriptors: list[int] = []
    try:
        repository = repository.resolve(strict=True)
        try:
            _open_root(repository, descriptors)
        except FileNotFoundError:
            if descriptors:
                return [], False
            raise
        names: list[str] = []
        truncated = False
        with os.scandir(descriptors[-1]) as entries:
            for index, entry in enumerate(entries):
                if index == limit:
                    truncated = True
                    break
                if _SESSION_ID.fullmatch(entry.name):
                    names.append(entry.name)
        for index, name in enumerate((".veyro", "supervision")):
            _check_link(descriptors[index], name, descriptors[index + 1], directory=True)
        return sorted(names), truncated
    except (OSError, ValueError):
        raise JournalError("cannot discover private journals") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
