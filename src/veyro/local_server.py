"""Persistent, instance-authenticated label readout on loopback only."""

from __future__ import annotations

import argparse
import fcntl
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from veyro.local_models import LocalModels, ModelProfile, load_profiles
from veyro.readout import READOUT_PROTOCOL, ReadoutEngine, ReadoutQuestion

DEFAULT_PORTS = {"small": 8081, "coder14": 8083, "coder30": 8084, "14b": 8082}
MAX_REQUEST_BYTES = 1024 * 1024
MAX_TIMEOUT = 120
START_WAIT = 10
Name = Annotated[str, StringConstraints(min_length=1, max_length=128, strip_whitespace=True)]
Text = Annotated[str, StringConstraints(min_length=1, max_length=8192, strip_whitespace=True)]


class LocalServiceError(RuntimeError):
    """The local service is unavailable or its identity cannot be verified."""


class _LockBusy(LocalServiceError):
    pass


class _Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Name
    description: Text


class _Question(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: Text
    outcomes: list[_Outcome] = Field(min_length=2, max_length=128)

    @model_validator(mode="after")
    def unique_outcomes(self):
        if len({o.name for o in self.outcomes}) != len(self.outcomes):
            raise ValueError("outcome names must be unique")
        return self


class _Evaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    state: object
    questions: dict[Name, _Question] = Field(min_length=1, max_length=8)
    timeout: float = Field(default=60, gt=0, le=MAX_TIMEOUT, allow_inf_nan=False)

    def readout_questions(self) -> tuple[ReadoutQuestion, ...]:
        return tuple(
            ReadoutQuestion(name, q.text, tuple((o.name, o.description) for o in q.outcomes))
            for name, q in self.questions.items()
        )


def _profile(profile_id: str) -> ModelProfile:
    profiles = load_profiles()
    if profile_id not in profiles:
        raise LocalServiceError(f"Unknown model profile: {profile_id}")
    return profiles[profile_id]


def _directory(state_dir: Path | None) -> Path:
    directory = Path(state_dir or "~/.local/state/veyro/readout").expanduser().absolute()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LocalServiceError("Service state directory must be private (0700) and owned by you")
    return directory


def _private_open(path: Path, flags: int):
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        os.close(fd)
        raise LocalServiceError(f"Unsafe service file: {path.name}")
    return fd


@contextmanager
def _lock(directory: Path, profile_id: str, kind: str, *, wait=0):
    fd = _private_open(directory / f"{profile_id}.{kind}.lock", os.O_CREAT | os.O_RDWR)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _LockBusy(f"{profile_id}: {kind} lock is busy; retry later") from None
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)


def _worker_running(directory: Path, profile_id: str) -> bool:
    try:
        with _lock(directory, profile_id, "worker"):
            return False
    except _LockBusy:
        return True


def _write_metadata(path: Path, data: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _metadata(directory: Path, profile: ModelProfile) -> dict | None:
    path = directory / f"{profile.id}.json"
    try:
        fd = _private_open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    except LocalServiceError:
        if not path.exists():
            return None
        raise
    try:
        with os.fdopen(fd) as stream:
            raw = stream.read(16385)
        data = json.loads(raw)
        if (
            len(raw) > 16384
            or not isinstance(data, dict)
            or data.get("profile") != profile.model_dump()
            or data.get("protocol") != READOUT_PROTOCOL
            or type(data.get("port")) is not int
            or not 1 <= data["port"] <= 65535
            or data["port"] in {8080, 11434}
            or type(data.get("context_size")) is not int
            or not 512 <= data["context_size"] <= 32768
            or not isinstance(data.get("token"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", data["token"])
            or not isinstance(data.get("instance_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", data["instance_id"])
            or not isinstance(data.get("model_manifest_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", data["model_manifest_sha256"])
            or not isinstance(data.get("model_blob_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", data["model_blob_sha256"])
        ):
            raise ValueError("invalid or mismatched metadata")
        return data
    except (ValueError, TypeError) as error:
        raise LocalServiceError(f"Refusing invalid metadata for {profile.id}: {error}") from error


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LocalServiceError("Local service redirected; refusing request")


def _request(metadata: dict, endpoint: str, payload: dict | None = None, *, timeout=2) -> dict:
    body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    if body is not None and len(body) > MAX_REQUEST_BYTES:
        raise ValueError("request exceeds 1 MiB limit")
    headers = {"Content-Type": "application/json"}
    if endpoint != "/health":
        headers["Authorization"] = f"Bearer {metadata['token']}"
    request = Request(f"http://127.0.0.1:{metadata['port']}{endpoint}", data=body, headers=headers)
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.headers.get_content_type() != "application/json":
                raise LocalServiceError("Local service returned a non-JSON response")
            raw = response.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise LocalServiceError("Local service response exceeds size limit")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise LocalServiceError("Invalid local service response")
        return result
    except HTTPError as error:
        detail = error.read(4096).decode(errors="replace")
        raise LocalServiceError(f"Local service HTTP {error.code}: {detail}") from error
    except ValueError as error:
        raise LocalServiceError("Invalid local service JSON response") from error


def _verify_identity(metadata: dict, response: dict) -> dict:
    for key in (
        "instance_id",
        "profile",
        "model_manifest_sha256",
        "model_blob_sha256",
        "protocol",
        "context_size",
        "port",
    ):
        if response.get(key) != metadata[key]:
            raise LocalServiceError(f"Local service identity mismatch: {key}")
    return response


def service_status(profile_id: str, *, state_dir: Path | None = None) -> dict:
    profile, directory = _profile(profile_id), _directory(state_dir)
    metadata = _metadata(directory, profile)
    if metadata is None:
        status = "unavailable" if _worker_running(directory, profile_id) else "stopped"
        return {"status": status, "profile": profile.model_dump()}
    try:
        return _verify_identity(metadata, _request(metadata, "/health"))
    except (URLError, OSError):
        status = "unavailable" if _worker_running(directory, profile_id) else "stale"
        return {**{k: v for k, v in metadata.items() if k != "token"}, "status": status}


def start_service(
    profile_id: str,
    *,
    port: int | None = None,
    state_dir: Path | None = None,
    context_size: int = 8192,
) -> dict:
    profile, directory = _profile(profile_id), _directory(state_dir)
    port = DEFAULT_PORTS.get(profile_id) if port is None else port
    if type(port) is not int or not 1 <= port <= 65535 or port in {8080, 11434}:
        raise ValueError("port must be 1..65535, excluding 8080 and 11434")
    if type(context_size) is not int or not 512 <= context_size <= 32768:
        raise ValueError("context_size must be 512..32768")
    with _lock(directory, profile_id, "start"):
        status = service_status(profile_id, state_dir=directory)
        if status["status"] in {"loading", "ready"}:
            if status["port"] != port or status["context_size"] != context_size:
                raise LocalServiceError(
                    "Existing instance has different port/context configuration"
                )
            return status
        if _worker_running(directory, profile_id):
            raise LocalServiceError("Existing worker is unavailable or stopping; retry later")
        # Only the worker replaces stale metadata, after taking its lifetime lock and binding.
        log_fd = _private_open(
            directory / f"{profile.id}.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY
        )
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "veyro.local_server",
                    "serve",
                    profile_id,
                    "--port",
                    str(port),
                    "--state-dir",
                    str(directory),
                    "--context-size",
                    str(context_size),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            os.close(log_fd)
        deadline = time.monotonic() + START_WAIT
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise LocalServiceError(
                    f"Worker exited; inspect {directory / (profile_id + '.log')}"
                )
            status = service_status(profile_id, state_dir=directory)
            if status["status"] == "ready":
                return status
            time.sleep(0.05)
        if status["status"] == "loading":
            return status
        raise LocalServiceError(f"Worker did not become reachable; inspect {profile_id}.log")


def stop_service(profile_id: str, *, state_dir: Path | None = None) -> dict:
    profile, directory = _profile(profile_id), _directory(state_dir)
    with _lock(directory, profile_id, "start"):
        status = service_status(profile_id, state_dir=directory)
        if status["status"] in {"stopped", "stale", "unavailable"}:
            return status
        metadata = _metadata(directory, profile)
        response = _verify_identity(metadata, _request(metadata, "/shutdown", {}))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _worker_running(directory, profile_id):
            time.sleep(0.05)
        if not _worker_running(directory, profile_id):
            return {**response, "status": "stopped"}
        return response


def evaluate_local(
    profile_id: str,
    state: object,
    questions: dict,
    *,
    timeout: float = 60,
    state_dir: Path | None = None,
) -> dict:
    payload = {"state": state, "questions": questions, "timeout": timeout}
    _Evaluation.model_validate(payload)
    metadata = _metadata(_directory(state_dir), _profile(profile_id))
    if metadata is None:
        raise LocalServiceError(f"{profile_id}: service is not running")
    try:
        health = _verify_identity(metadata, _request(metadata, "/health"))
        if health.get("status") != "ready":
            raise LocalServiceError(f"{profile_id}: service is {health.get('status')}")
        return _verify_identity(
            metadata, _request(metadata, "/v1/evaluate", payload, timeout=timeout + 5)
        )
    except (URLError, OSError) as error:
        raise LocalServiceError(f"{profile_id}: service unavailable: {error}") from error


class _Server(ThreadingHTTPServer):
    # No unbounded thread creation, including unauthenticated or slow clients.
    daemon_threads = False
    request_queue_size = 8

    def __init__(self, port: int, metadata: dict):
        self.metadata = metadata
        self.engine = None
        self.status = "loading"
        self.warmup_ms = None
        self.connections = threading.BoundedSemaphore(8)
        self.evaluations = threading.BoundedSemaphore(2)
        self.stopping = threading.Event()
        super().__init__(("127.0.0.1", port), _Handler)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            try:
                request.settimeout(0.1)
                body = b'{"error":"connection limit reached"}'
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def health(self) -> dict:
        return {
            **{k: v for k, v in self.metadata.items() if k != "token"},
            "status": self.status,
            "warmup_ms": self.warmup_ms,
        }

    def stop(self):
        self.status = "stopping"
        self.stopping.set()
        if self.engine is not None:
            self.engine.cancel()
        self.shutdown()


class _Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format, *args):
        # Never log evidence, credentials or request paths supplied by clients.
        pass

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, allow_nan=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass
        self.close_connection = True

    def do_GET(self):
        if self.path == "/health":
            self._send(200, self.server.health())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in {"/v1/evaluate", "/shutdown"}:
            self._send(404, {"error": "not found"})
            return
        token = self.headers.get("Authorization", "")
        if not hmac.compare_digest(
            token.encode(), f"Bearer {self.server.metadata['token']}".encode()
        ):
            self._send(401, {"error": "instance bearer token required"})
            return
        if self.headers.get_content_type() != "application/json":
            self._send(415, {"error": "Content-Type must be application/json"})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                raise ValueError("one Content-Length and no Transfer-Encoding required")
            length = int(lengths[0])
            if not 0 < length <= MAX_REQUEST_BYTES:
                self._send(413, {"error": "request must contain 1..1048576 bytes"})
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete request body")
            payload = json.loads(raw)
            # json.loads accepts NaN/Infinity by default; JSON evidence must not.
            json.dumps(payload, allow_nan=False)
            if self.path == "/shutdown":
                if payload != {}:
                    raise ValueError("shutdown expects an empty object")
                self.server.status = "stopping"
                self.server.stopping.set()
                self._send(200, self.server.health())
                threading.Thread(target=self.server.stop, daemon=True).start()
                return
            evaluation = _Evaluation.model_validate(payload)
        except (ValueError, RecursionError, OSError):
            self._send(400, {"error": "invalid JSON request or evaluation schema"})
            return
        if self.server.status != "ready":
            self._send(503, {"error": f"service is {self.server.status}"})
            return
        if not self.server.evaluations.acquire(blocking=False):
            self._send(429, {"error": "readout queue is full"})
            return
        try:
            result = self.server.engine.evaluate(
                evaluation.state, evaluation.readout_questions(), timeout=evaluation.timeout
            )
            self._send(200, {**result, **self.server.health()})
        except TimeoutError:
            self._send(504, {"error": "readout deadline exceeded"})
        except ValueError as error:
            self._send(400, {"error": str(error)})
        except Exception:
            self._send(500, {"error": "readout failed; see worker log"})
            traceback.print_exc()
        finally:
            self.server.evaluations.release()


def serve(
    profile_id: str,
    port: int,
    state_dir: Path | None,
    context_size: int = 8192,
) -> None:
    profile, directory = _profile(profile_id), _directory(state_dir)
    if type(port) is not int or not 0 <= port <= 65535 or port in {8080, 11434}:
        raise ValueError("invalid readout port")
    if type(context_size) is not int or not 512 <= context_size <= 32768:
        raise ValueError("context_size must be 512..32768")
    family = {"qwen2": "qwen2", "qwen3": "qwen3"}.get(profile.model_family)
    if family is None:
        raise LocalServiceError("Unsupported readout model family")
    with _lock(directory, profile_id, "worker", wait=0.25):
        _metadata(directory, profile)  # Reject corrupt metadata before replacing it.
        verified = LocalModels().verify_model(profile)
        model_path = verified.blob_path
        metadata = {
            "profile": profile.model_dump(),
            "model_manifest_sha256": verified.manifest_sha256,
            "model_blob_sha256": verified.blob_sha256,
            "protocol": READOUT_PROTOCOL,
            "context_size": context_size,
            "instance_id": secrets.token_hex(16),
            "token": secrets.token_hex(32),
            "pid": os.getpid(),
            "port": port,
        }
        server = _Server(port, metadata)
        metadata["port"] = server.server_address[1]
        path = directory / f"{profile_id}.json"
        _write_metadata(path, metadata)

        def load():
            try:
                server.engine = ReadoutEngine(model_path, family=family, context_size=context_size)
                if not server.stopping.is_set():
                    began = time.monotonic()
                    server.engine.evaluate(
                        {"warmup": True},
                        tuple(
                            ReadoutQuestion(
                                f"warmup_{i}",
                                "Is one equal to one?",
                                (("no", "No"), ("yes", "Yes")),
                            )
                            for i in range(2)
                        ),
                        timeout=60,
                    )
                    server.warmup_ms = (time.monotonic() - began) * 1000
                    if not server.stopping.is_set():
                        server.status = "ready"
            except Exception:
                server.status = "failed"
                traceback.print_exc()
                server.stop()

        loader = threading.Thread(target=load, name=f"readout-load-{profile_id}")
        loader.start()
        try:
            server.serve_forever(poll_interval=0.05)
        finally:
            server.stopping.set()
            server.server_close()
            loader.join()
            if server.engine is not None:
                server.engine.close()
            current = _metadata(directory, profile)
            if current is not None and current["instance_id"] == metadata["instance_id"]:
                path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("profile_id")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--context-size", type=int, default=8192)
    args = parser.parse_args()
    serve(args.profile_id, args.port, args.state_dir, args.context_size)


if __name__ == "__main__":
    main()
