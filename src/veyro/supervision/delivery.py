from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from veyro.models import SessionIdentity


class DeliveryError(RuntimeError):
    """A control delivery could not acquire a durable, exclusive claim."""


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _check_directory(info: os.stat_result, *, private: bool) -> None:
    if private:
        safe = info.st_uid == os.getuid() and not info.st_mode & 0o077
    else:
        # System temporary directories protect owned entries with the sticky bit.
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


class DeliveryLedger:
    """An immutable no-retry fence, including for interrupted or failed deliveries."""

    def __init__(self, root: Path):
        try:
            self._root = root.absolute()
            if ".." in self._root.parts or len(self._root.parts) < 2:
                raise DeliveryError("delivery ledger directory is unsafe")
            with _open_root(self._root, create=True) as descriptors:
                self._root_identity = _identity(os.fstat(descriptors[-1]))
                # Persist newly created ancestors too, including concurrent mkdirs.
                for descriptor in reversed(descriptors):
                    os.fsync(descriptor)
                _check_links(self._root, descriptors)
        except (OSError, ValueError):
            raise DeliveryError("delivery ledger is unavailable") from None

    def claim(self, *, session: SessionIdentity, command_id: str, request_sha256: str) -> None:
        if (
            not session.provider_session_id
            or not session.provider_session_id.strip()
            or not 1 <= len(command_id) <= 200
            or not re.fullmatch(r"[0-9a-f]{64}", request_sha256)
        ):
            raise DeliveryError("invalid delivery claim")
        try:
            repository = Path(session.repository).resolve(strict=True)
            if not repository.is_dir():
                raise DeliveryError("invalid delivery repository")
            scope = [str(repository), session.provider_id, session.provider_session_id]
            scope_digest = hashlib.sha256(json.dumps(scope).encode()).hexdigest()
            key = hashlib.sha256(json.dumps([*scope, command_id]).encode()).hexdigest()
            record = (
                json.dumps(
                    {
                        "version": 1,
                        "scope_sha256": scope_digest,
                        "command_sha256": hashlib.sha256(command_id.encode()).hexdigest(),
                        "request_sha256": request_sha256,
                    },
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            with _open_root(self._root, create=False) as descriptors:
                root_fd = descriptors[-1]
                if _identity(os.fstat(root_fd)) != self._root_identity:
                    raise DeliveryError("delivery ledger directory was replaced")
                filename = key + ".json"
                try:
                    descriptor = os.open(
                        filename,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                        0o600,
                        dir_fd=root_fd,
                    )
                except FileExistsError:
                    # Never read or reuse an old claim, even an empty or corrupt one.
                    raise DeliveryError("control command already claimed") from None
                try:
                    info = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or info.st_mode & 0o077
                        or info.st_nlink != 1
                    ):
                        raise DeliveryError("delivery claim file is unsafe")
                    os.fchmod(descriptor, 0o600)
                    remaining = memoryview(record)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written == 0:
                            raise DeliveryError("delivery claim could not be persisted")
                        remaining = remaining[written:]
                    os.fsync(descriptor)
                    linked = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
                    if (
                        _identity(info) != _identity(linked)
                        or linked.st_nlink != 1
                        or linked.st_mode & 0o077
                        or linked.st_uid != os.getuid()
                    ):
                        raise DeliveryError("delivery claim file was replaced")
                    os.fsync(root_fd)
                    _check_links(self._root, descriptors)
                finally:
                    # Never unlink: partial persistence must still block a retry.
                    os.close(descriptor)
        except (OSError, ValueError, RuntimeError) as error:
            if isinstance(error, DeliveryError):
                raise
            raise DeliveryError("delivery claim could not be persisted") from None
