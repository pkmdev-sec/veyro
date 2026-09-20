from __future__ import annotations

import hashlib
import os
import ssl
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlsplit, urlunsplit
from urllib.request import Request, urlopen

CHUNK_BYTES = 8 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class PinnedArtifact:
    filename: str
    expected_bytes: int
    expected_sha256: str


@dataclass(frozen=True, slots=True)
class ArtifactImportResult:
    destination: Path
    bytes_written: int
    sha256: str
    installed: bool


class ArtifactImportError(RuntimeError):
    """The candidate artifact could not be safely installed."""


GLIFORMER_LARGE_V1_WEIGHTS = PinnedArtifact(
    filename="pytorch_model.bin",
    expected_bytes=2_302_735_855,
    expected_sha256="f80b29199d66f878669f283703e4dba9fd726755dcc20aba1ed0d24fce4a23f1",
)


def import_pinned_artifact(
    source: str | Path,
    destination: Path,
    artifact: PinnedArtifact,
    *,
    ca_bundle: Path | None = None,
) -> ArtifactImportResult:
    """Verify a pinned artifact and atomically install it without overwriting existing data."""

    destination = destination.expanduser().absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ArtifactImportError(f"destination is a symbolic link; refusing to use {destination}")
    if destination.exists():
        if not destination.is_file():
            raise ArtifactImportError(
                f"destination exists but is not a regular file; refusing to use {destination}"
            )
        try:
            size, digest = _verify_file(destination, artifact)
        except ArtifactImportError as error:
            raise ArtifactImportError(
                f"destination exists but is invalid; refusing to overwrite {destination}: {error}"
            ) from error
        return ArtifactImportResult(destination, size, digest, installed=False)

    temporary_path: Path | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{artifact.filename}.", suffix=".part", dir=destination.parent
        )
        temporary_path = Path(temporary)
        with (
            os.fdopen(descriptor, "wb") as output,
            _open_source(source, artifact, ca_bundle=ca_bundle) as (input_stream, content_type),
        ):
            if content_type and "text/html" in content_type.lower():
                raise ArtifactImportError("source returned HTML instead of model weights")
            size, digest = _copy_and_hash(input_stream, output, artifact)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary_path, destination)
        except FileExistsError as error:
            raise ArtifactImportError(
                f"destination appeared during import; refusing to overwrite {destination}"
            ) from error
        temporary_path.unlink()
        temporary_path = None
        _fsync_directory(destination.parent)
        return ArtifactImportResult(destination, size, digest, installed=True)
    except ArtifactImportError:
        raise
    except OSError as error:
        raise ArtifactImportError(
            f"artifact import failed: {type(error).__name__}: {error}"
        ) from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _copy_and_hash(
    input_stream: BinaryIO,
    output: BinaryIO,
    artifact: PinnedArtifact,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    first_chunk = True
    while chunk := input_stream.read(CHUNK_BYTES):
        if first_chunk:
            first_chunk = False
            if _looks_like_html(chunk):
                raise ArtifactImportError("source contains HTML instead of model weights")
        size += len(chunk)
        if size > artifact.expected_bytes:
            raise ArtifactImportError(
                f"artifact exceeds expected size {artifact.expected_bytes} bytes"
            )
        digest.update(chunk)
        output.write(chunk)

    actual_digest = digest.hexdigest()
    _validate_identity(size, actual_digest, artifact)
    return size, actual_digest


def _verify_file(path: Path, artifact: PinnedArtifact) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            first_chunk = True
            while chunk := handle.read(CHUNK_BYTES):
                if first_chunk:
                    first_chunk = False
                    if _looks_like_html(chunk):
                        raise ArtifactImportError("file contains HTML instead of model weights")
                size += len(chunk)
                digest.update(chunk)
    except OSError as error:
        raise ArtifactImportError(f"cannot read artifact: {error}") from error
    actual_digest = digest.hexdigest()
    _validate_identity(size, actual_digest, artifact)
    return size, actual_digest


def _validate_identity(size: int, digest: str, artifact: PinnedArtifact) -> None:
    if size != artifact.expected_bytes:
        raise ArtifactImportError(
            f"size mismatch: expected {artifact.expected_bytes} bytes, found {size}"
        )
    if digest != artifact.expected_sha256:
        raise ArtifactImportError(
            f"SHA-256 mismatch: expected {artifact.expected_sha256}, found {digest}"
        )


def _looks_like_html(chunk: bytes) -> bool:
    prefix = chunk[:512].lstrip().lower()
    return prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))


@contextmanager
def _open_source(
    source: str | Path,
    artifact: PinnedArtifact,
    *,
    ca_bundle: Path | None,
) -> Iterator[tuple[BinaryIO, str | None]]:
    source_text = str(source)
    parsed = urlsplit(source_text)
    if parsed.scheme:
        if parsed.scheme != "https":
            raise ArtifactImportError("artifact URLs must use HTTPS")
        if parsed.username or parsed.password:
            raise ArtifactImportError("artifact URLs must not contain embedded credentials")
        with _open_https_source(source_text, parsed, artifact, ca_bundle) as opened:
            yield opened
        return

    path = Path(source).expanduser()
    try:
        handle = path.open("rb")
    except FileNotFoundError as error:
        raise ArtifactImportError(f"artifact source does not exist: {path}") from error
    except OSError as error:
        raise ArtifactImportError(f"cannot read artifact source {path}: {error}") from error
    with handle:
        yield handle, None


@contextmanager
def _open_https_source(
    source: str,
    parsed_source: SplitResult,
    artifact: PinnedArtifact,
    ca_bundle: Path | None,
) -> Iterator[tuple[BinaryIO, str | None]]:
    if ca_bundle is not None:
        ca_bundle = ca_bundle.expanduser()
        if not ca_bundle.is_file():
            raise ArtifactImportError(f"CA bundle does not exist: {ca_bundle}")
    context = ssl.create_default_context(cafile=str(ca_bundle) if ca_bundle else None)
    request = Request(
        source,
        headers={
            "Accept": "application/octet-stream",
            "Accept-Encoding": "identity",
            "User-Agent": "veyro-model-import/1",
        },
    )
    safe_source = urlunsplit(
        (parsed_source.scheme, parsed_source.netloc, parsed_source.path, "", "")
    )
    try:
        with urlopen(request, context=context, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310 - HTTPS enforced above
            final_url = urlsplit(response.geturl())
            if final_url.scheme != "https":
                raise ArtifactImportError("artifact download redirected away from HTTPS")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ArtifactImportError("artifact server returned encoded content")
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) != artifact.expected_bytes:
                raise ArtifactImportError(
                    f"server size mismatch: expected {artifact.expected_bytes} bytes, "
                    f"found {content_length}"
                )
            yield response, response.headers.get("Content-Type")
    except ArtifactImportError:
        raise
    except HTTPError as error:
        raise ArtifactImportError(
            f"artifact server returned HTTP {error.code} for {safe_source}"
        ) from error
    except (URLError, ssl.SSLError, ValueError) as error:
        raise ArtifactImportError(
            f"HTTPS artifact download failed for {safe_source}: {type(error).__name__}"
        ) from error


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
