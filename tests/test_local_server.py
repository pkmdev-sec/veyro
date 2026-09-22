"""Real loopback HTTP and lifecycle checks; never loads model weights."""

from __future__ import annotations

import concurrent.futures
import http.client
import json
import os
import threading
import time
from pathlib import Path

import pytest

from veyro import local_server
from veyro.local_models import VerifiedModel
from veyro.local_server import LocalServiceError, evaluate_local, service_status, stop_service

QUESTIONS = {
    "safe": {
        "text": "Is this safe?",
        "outcomes": [
            {"name": "yes", "description": "safe"},
            {"name": "no", "description": "unsafe"},
        ],
    }
}


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


class FakeEngine:
    instances = []

    def __init__(self, model_path, *, family, context_size):
        self.model_path, self.family, self.context_size = model_path, family, context_size
        self.closed = False
        self.cancelled = False
        self.warmups = 0
        self.calls = []
        self.instances.append(self)

    def evaluate(self, state, questions, *, timeout):
        if state == {"warmup": True}:
            self.warmups += 1
        else:
            self.calls.append((state, questions, timeout))
        if state == "timeout":
            raise TimeoutError("expired")
        if state == "invalid":
            raise ValueError("context capacity")
        return {
            "predictions": {questions[0].name: {"probabilities": [0.8, 0.2]}},
            "calibrated": False,
            "protocol": local_server.READOUT_PROTOCOL,
        }

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


@pytest.fixture
def fake_backend(monkeypatch):
    FakeEngine.instances = []
    monkeypatch.setattr(local_server, "ReadoutEngine", FakeEngine)
    monkeypatch.setattr(
        local_server.LocalModels,
        "verify_model",
        lambda self, profile: VerifiedModel(
            profile,
            Path("/unused/blobs/sha256-" + profile.blob_sha256),
            profile.manifest_sha256,
            profile.blob_sha256,
        ),
    )


@pytest.fixture
def running(tmp_path, fake_backend):
    threads = []
    errors = []

    def launch(profile="small"):
        def run():
            try:
                local_server.serve(profile, 0, tmp_path, 8192)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        threads.append((profile, thread))
        wait_for(lambda: service_status(profile, state_dir=tmp_path).get("status") == "ready")
        return json.loads((tmp_path / f"{profile}.json").read_text())

    yield tmp_path, launch
    for profile, thread in threads:
        if thread.is_alive():
            stop_service(profile, state_dir=tmp_path)
        thread.join(3)
        assert not thread.is_alive()
    assert not errors


def request(
    metadata, path="/v1/evaluate", *, payload=None, token=True, headers=None, method="POST"
):
    connection = http.client.HTTPConnection("127.0.0.1", metadata["port"], timeout=3)
    body = json.dumps(
        payload
        if payload is not None
        else {
            "state": {"result": "ok"},
            "questions": QUESTIONS,
        }
    )
    fields = {"Content-Type": "application/json"}
    if token:
        fields["Authorization"] = f"Bearer {metadata['token']}"
    fields.update(headers or {})
    try:
        connection.request(method, path, body=body, headers=fields)
        response = connection.getresponse()
        data = response.read()
        return response.status, json.loads(data) if data else None
    finally:
        connection.close()


def test_http_readout_identity_and_persistent_engine(running):
    directory, launch = running
    metadata = launch()
    for _ in range(2):
        result = evaluate_local("small", {"result": "ok"}, QUESTIONS, state_dir=directory)
        assert result["predictions"]["safe"]["probabilities"] == [0.8, 0.2]
        assert result["instance_id"] == metadata["instance_id"]
        assert result["profile"]["manifest_sha256"] == metadata["profile"][
            "manifest_sha256"
        ]
        assert result["model_manifest_sha256"] == metadata["profile"]["manifest_sha256"]
        assert result["model_blob_sha256"] == metadata["profile"]["blob_sha256"]
        assert result["protocol"] == local_server.READOUT_PROTOCOL
        assert result["calibrated"] is False
        assert "token" not in result
    assert len(FakeEngine.instances) == 1
    engine = FakeEngine.instances[0]
    assert engine.family == "qwen3"
    assert engine.warmups == 1
    assert len(engine.calls) == 2
    assert engine.calls[0][1][0].outcomes == (("yes", "safe"), ("no", "unsafe"))
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert ((directory / "small.json").stat().st_mode & 0o777) == 0o600
    code, health = request(metadata, "/health", token=False, method="GET")
    assert code == 200 and health["status"] == "ready" and "token" not in health
    assert health["warmup_ms"] is not None


def test_qwen3_profile_maps_to_qwen3(running):
    _, launch = running
    launch("14b")
    assert FakeEngine.instances[0].family == "qwen3"


@pytest.mark.parametrize("path", ["/v1/evaluate", "/shutdown"])
def test_authentication_required(running, path):
    _, launch = running
    metadata = launch()
    assert request(metadata, path, token=False)[0] == 401
    assert request(metadata, path, headers={"Authorization": "Bearer wrong"})[0] == 401
    assert not FakeEngine.instances[0].calls


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"state": {}, "questions": {}},
        {"state": {}, "questions": QUESTIONS, "timeout": 0},
        {"state": {}, "questions": QUESTIONS, "timeout": 121},
        {"state": {}, "questions": QUESTIONS, "timeout": True},
        {"state": {}, "questions": QUESTIONS, "timeout": float("nan")},
        {"state": float("inf"), "questions": QUESTIONS},
        {"state": {}, "questions": QUESTIONS, "model_path": "/tmp/evil.gguf"},
        {"state": {}, "questions": {str(i): QUESTIONS["safe"] for i in range(9)}},
        {"state": {}, "questions": {"q": {"text": " ", "outcomes": QUESTIONS["safe"]["outcomes"]}}},
        {
            "state": {},
            "questions": {
                "q": {
                    "text": "?",
                    "outcomes": [
                        {"name": "yes", "description": "a"},
                        {"name": "yes", "description": "b"},
                    ],
                }
            },
        },
        {
            "state": {},
            "questions": {
                "q": {
                    "text": "?",
                    "outcomes": [{"name": str(i), "description": "a"} for i in range(129)],
                }
            },
        },
    ],
)
def test_invalid_requests_are_rejected_before_engine(running, payload):
    _, launch = running
    metadata = launch()
    assert request(metadata, payload=payload)[0] == 400
    assert not FakeEngine.instances[0].calls


def test_content_type_and_request_size(running):
    _, launch = running
    metadata = launch()
    assert request(metadata, headers={"Content-Type": "text/plain"})[0] == 415
    assert request(metadata, headers={"Content-Length": "1048577"})[0] == 413
    assert request(metadata, headers={"Transfer-Encoding": "chunked"})[0] == 400
    assert request(metadata, "/missing")[0] == 404
    assert request(metadata, "/v1/evaluate", method="GET")[0] == 404


@pytest.mark.parametrize("state,code", [("timeout", 504), ("invalid", 400)])
def test_engine_errors_are_http_errors(running, state, code):
    _, launch = running
    metadata = launch()
    assert request(metadata, payload={"state": state, "questions": QUESTIONS})[0] == code


def test_shutdown_is_authenticated_and_idempotent(running):
    directory, launch = running
    metadata = launch()
    assert request(metadata, "/shutdown", payload={}, token=False)[0] == 401
    assert stop_service("small", state_dir=directory)["status"] == "stopped"
    assert stop_service("small", state_dir=directory)["status"] == "stopped"
    assert service_status("small", state_dir=directory)["status"] == "stopped"
    assert FakeEngine.instances[0].closed
    assert not (directory / "small.json").exists()
    new = launch()
    assert metadata["instance_id"] != new["instance_id"]


def test_bounded_inference_queue(running, monkeypatch):
    _, launch = running
    metadata = launch()
    entered = threading.Barrier(3)
    release = threading.Event()

    def evaluate(self, state, questions, *, timeout):
        entered.wait(timeout=3)
        assert release.wait(timeout=3)
        return {"predictions": {}}

    monkeypatch.setattr(FakeEngine, "evaluate", evaluate)
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(request, metadata) for _ in range(2)]
        try:
            entered.wait(timeout=3)
            assert request(metadata)[0] == 429
            assert request(metadata, "/health", method="GET")[0] == 200
        finally:
            release.set()
        assert [future.result()[0] for future in futures] == [200, 200]


def test_loading_is_observable_and_shutdown_waits_for_engine(tmp_path, fake_backend, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class LoadingEngine(FakeEngine):
        def __init__(self, *args, **kwargs):
            entered.set()
            assert release.wait(timeout=5)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(local_server, "ReadoutEngine", LoadingEngine)
    worker = threading.Thread(target=local_server.serve, args=("small", 0, tmp_path, 8192))
    worker.start()
    try:
        assert entered.wait(timeout=3)
        status = service_status("small", state_dir=tmp_path)
        assert status["status"] == "loading"
        metadata = json.loads((tmp_path / "small.json").read_text())
        assert request(metadata)[0] == 503
        assert request(metadata, "/shutdown", payload={})[1]["status"] == "stopping"
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert FakeEngine.instances[0].closed


def test_private_state_and_symlink_metadata_required(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(LocalServiceError, match="private"):
        service_status("small", state_dir=tmp_path)
    tmp_path.chmod(0o700)
    target = tmp_path / "other.json"
    target.write_text("{}")
    (tmp_path / "small.json").symlink_to(target)
    with pytest.raises(OSError):
        service_status("small", state_dir=tmp_path)
    assert target.read_text() == "{}"


def test_metadata_unlinked_during_open_is_observed_as_stopped(tmp_path, monkeypatch):
    path = tmp_path / "small.json"
    path.write_text("{}")
    path.chmod(0o600)
    real_open = os.open

    def open_then_unlink(candidate, flags, mode=0o777):
        descriptor = real_open(candidate, flags, mode)
        if Path(candidate) == path:
            path.unlink()
        return descriptor

    monkeypatch.setattr(local_server.os, "open", open_then_unlink)
    assert service_status("small", state_dir=tmp_path)["status"] == "stopped"


def test_mismatched_metadata_never_shuts_down_server(running):
    directory, launch = running
    metadata = launch()
    path = directory / "small.json"
    path.write_text(json.dumps({**metadata, "instance_id": "b" * 32}))
    try:
        with pytest.raises(LocalServiceError, match="identity mismatch"):
            stop_service("small", state_dir=directory)
        with pytest.raises(LocalServiceError, match="identity mismatch"):
            evaluate_local("small", {}, QUESTIONS, state_dir=directory)
        assert request(metadata, "/health", method="GET")[1]["status"] == "ready"
    finally:
        path.write_text(json.dumps(metadata))


@pytest.mark.parametrize(
    "change",
    [
        {"profile": {}},
        {"port": 11434},
        {"port": "8081"},
        {"token": "x"},
        {"protocol": "old"},
    ],
)
def test_corrupt_metadata_rejected(running, change):
    directory, launch = running
    metadata = launch()
    path = directory / "small.json"
    path.write_text(json.dumps({**metadata, **change}))
    try:
        with pytest.raises(LocalServiceError, match="invalid metadata"):
            stop_service("small", state_dir=directory)
    finally:
        path.write_text(json.dumps(metadata))


def test_worker_lock_prevents_duplicate_foreground_server(running):
    directory, launch = running
    metadata = launch()
    with pytest.raises(LocalServiceError, match="worker lock is busy"):
        local_server.serve("small", 0, directory, 8192)
    assert service_status("small", state_dir=directory)["instance_id"] == metadata["instance_id"]


def test_start_existing_instance_is_idempotent(running, monkeypatch):
    directory, launch = running
    metadata = launch()

    def forbidden(*args, **kwargs):
        pytest.fail("must not spawn another worker")

    monkeypatch.setattr(local_server.subprocess, "Popen", forbidden)
    status = local_server.start_service("small", port=metadata["port"], state_dir=directory)
    assert status["instance_id"] == metadata["instance_id"]
    with pytest.raises(LocalServiceError, match="different port/context"):
        local_server.start_service(
            "small", port=metadata["port"], state_dir=directory, context_size=4096
        )


def test_start_uses_isolated_interpreter_and_private_log(tmp_path, fake_backend, monkeypatch):
    launched = []

    class Process:
        def __init__(self, command, **kwargs):
            launched.append((command, kwargs))
            port = int(command[command.index("--port") + 1])
            self.thread = threading.Thread(
                target=local_server.serve, args=("small", port, tmp_path, 8192)
            )
            self.thread.start()

        def poll(self):
            return None

    monkeypatch.setattr(local_server.subprocess, "Popen", Process)
    # Reserve a free port before starting the simulated subprocess.
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        status = local_server.start_service("small", port=port, state_dir=tmp_path)
        assert status["status"] == "ready"
        command, options = launched[0]
        assert command[:5] == [
            local_server.sys.executable,
            "-I",
            "-m",
            "veyro.local_server",
            "serve",
        ]
        assert options["start_new_session"] and options["close_fds"]
        assert ((tmp_path / "small.log").stat().st_mode & 0o777) == 0o600
    finally:
        stop_service("small", state_dir=tmp_path)


def test_stale_metadata_never_signals_pid(running, monkeypatch):
    directory, launch = running
    metadata = launch()
    stop_service("small", state_dir=directory)
    path = directory / "small.json"
    path.write_text(json.dumps({**metadata, "pid": os.getpid()}))
    path.chmod(0o600)

    def forbidden(*args):
        pytest.fail("must not signal a PID from metadata")

    monkeypatch.setattr(os, "kill", forbidden)
    assert service_status("small", state_dir=directory)["status"] == "stale"
    assert stop_service("small", state_dir=directory)["status"] == "stale"
    replacement = launch()
    assert replacement["instance_id"] != metadata["instance_id"]


@pytest.mark.parametrize("port", [8080, 11434, -1, 65536, True])
def test_reserved_and_invalid_ports_never_spawn(tmp_path, port):
    with pytest.raises(ValueError, match="port"):
        local_server.start_service("small", port=port, state_dir=tmp_path)


def test_unknown_profile_cannot_escape_state_directory(tmp_path):
    with pytest.raises(LocalServiceError, match="Unknown model profile"):
        service_status("../escape", state_dir=tmp_path)


def test_worker_retries_transient_status_probe_lock(tmp_path, fake_backend):
    errors = []

    def run():
        try:
            local_server.serve("small", 0, tmp_path, 8192)
        except BaseException as error:
            errors.append(error)

    with local_server._lock(tmp_path, "small", "worker"):
        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(0.05)
    try:
        wait_for(lambda: service_status("small", state_dir=tmp_path)["status"] == "ready")
    finally:
        stop_service("small", state_dir=tmp_path)
        worker.join(3)
    assert not errors and not worker.is_alive()


def test_start_lock_rejects_concurrent_lifecycle(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("concurrent start must not spawn")

    monkeypatch.setattr(local_server.subprocess, "Popen", forbidden)
    with local_server._lock(tmp_path, "small", "start"):
        with pytest.raises(LocalServiceError, match="start lock is busy"):
            local_server.start_service("small", state_dir=tmp_path)


def test_failed_load_releases_listener_metadata_and_lock(tmp_path, fake_backend, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("test load failure")

    monkeypatch.setattr(local_server, "ReadoutEngine", fail)
    worker = threading.Thread(target=local_server.serve, args=("small", 0, tmp_path, 8192))
    worker.start()
    worker.join(3)
    assert not worker.is_alive()
    assert not (tmp_path / "small.json").exists()
    assert service_status("small", state_dir=tmp_path)["status"] == "stopped"


def test_shutdown_finishes_active_evaluation_before_closing(running, monkeypatch):
    directory, launch = running
    metadata = launch()
    entered, release = threading.Event(), threading.Event()

    def evaluate(self, state, questions, *, timeout):
        entered.set()
        assert release.wait(timeout=3)
        assert not self.closed
        return {"predictions": {}}

    monkeypatch.setattr(FakeEngine, "evaluate", evaluate)
    with concurrent.futures.ThreadPoolExecutor(1) as executor:
        future = executor.submit(request, metadata)
        try:
            assert entered.wait(timeout=3)
            assert request(metadata, "/shutdown", payload={})[0] == 200
            assert not FakeEngine.instances[0].closed
        finally:
            release.set()
        assert future.result()[0] == 200
    wait_for(lambda: service_status("small", state_dir=directory)["status"] == "stopped")
    assert FakeEngine.instances[0].closed


def test_connections_are_bounded_without_spawning_more_threads(running):
    import socket

    _, launch = running
    metadata = launch()
    clients = []
    try:
        # Headers remain incomplete, occupying the bounded connection workers.
        for _ in range(8):
            client = socket.create_connection(("127.0.0.1", metadata["port"]), timeout=2)
            client.sendall(b"GET /health HTTP/1.0\r\n")
            clients.append(client)
        assert request(metadata, "/health", method="GET")[0] == 503
    finally:
        for client in clients:
            client.close()


def test_oversized_client_payload_fails_before_sending(running):
    directory, launch = running
    launch()
    with pytest.raises(ValueError, match="1 MiB"):
        evaluate_local(
            "small", "x" * local_server.MAX_REQUEST_BYTES, QUESTIONS, state_dir=directory
        )
    assert not FakeEngine.instances[0].calls


def test_non_ascii_auth_is_rejected(running):
    _, launch = running
    metadata = launch()
    assert request(metadata, headers={"Authorization": "Bearer é"})[0] == 401


def test_public_metadata_permissions_are_rejected(running):
    directory, launch = running
    launch()
    path = directory / "small.json"
    path.chmod(0o644)
    try:
        with pytest.raises(LocalServiceError, match="Unsafe service file"):
            stop_service("small", state_dir=directory)
    finally:
        path.chmod(0o600)


def test_port_collision_does_not_replace_metadata_or_load_model(tmp_path, fake_backend):
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(OSError):
            local_server.serve("small", listener.getsockname()[1], tmp_path, 8192)
    assert not FakeEngine.instances
    assert not (tmp_path / "small.json").exists()
    assert not local_server._worker_running(tmp_path, "small")
