"""No live inference: API replies and local model-store files are fixtures."""

from __future__ import annotations

import hashlib
import io
import json
from urllib.error import HTTPError, URLError

import pytest
from pydantic import ValidationError

from veyro import local_models
from veyro.local_models import LocalModelError, LocalModels, ModelProfile, load_profiles


@pytest.fixture
def profile():
    return ModelProfile(
        id="small",
        ollama_model="qwen2.5:7b",
        expected_digest="a" * 64,
        parameter_size="7.6B",
        quantization="Q4_K_M",
        model_family="qwen2",
    )


def entry(profile, **overrides):
    return {
        "name": profile.ollama_model,
        "digest": profile.expected_digest,
        "size": 100,
        "details": {
            "family": profile.model_family,
            "parameter_size": profile.parameter_size,
            "quantization_level": profile.quantization,
        },
        **overrides,
    }


@pytest.fixture
def api(monkeypatch):
    calls = []
    replies = {"/api/tags": {"models": []}, "/api/ps": {"models": []}}

    def request(_self, endpoint, payload=None):
        calls.append((endpoint, payload))
        reply = replies[endpoint]
        return reply() if callable(reply) else reply

    monkeypatch.setattr(LocalModels, "_request", request)
    return replies, calls


def test_shipped_profiles_are_pinned():
    profiles = load_profiles()
    assert set(profiles) == {"small", "14b"}
    assert profiles["small"].ollama_model == "qwen3:4b-instruct-2507-q4_K_M"
    assert profiles["14b"].ollama_model == "qwen3:14b"
    assert profiles["small"].expected_digest == (
        "0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0"
    )
    assert profiles["14b"].expected_digest == (
        "bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8"
    )


def test_wheel_profile_lookup(tmp_path, monkeypatch, profile):
    monkeypatch.setattr(local_models, "__file__", str(tmp_path / "lib/veyro/local_models.py"))
    monkeypatch.setattr(local_models.sys, "prefix", str(tmp_path))
    destination = tmp_path / "share/veyro/model-profiles.json"
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps({"small": profile.model_dump()}))
    assert load_profiles() == {"small": profile}


@pytest.mark.parametrize("value", [[], {}, {"small": {}}, {"wrong": "invalid"}])
def test_invalid_profile_files(tmp_path, value):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(value))
    with pytest.raises(LocalModelError, match="Cannot load model profiles"):
        load_profiles(path)


def test_profile_key_and_id_must_match(tmp_path, profile):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"wrong": profile.model_dump()}))
    with pytest.raises(LocalModelError, match="keys must match"):
        load_profiles(path)


@pytest.mark.parametrize("model", ["qwen3", "../qwen3:14b", "qwen3:..", "x:y:z", "x//y:1"])
def test_model_names_cannot_escape_manifest_store(profile, model):
    with pytest.raises(ValidationError):
        ModelProfile.model_validate({**profile.model_dump(), "ollama_model": model})


def test_digest_required(profile):
    data = profile.model_dump()
    del data["expected_digest"]
    with pytest.raises(ValidationError):
        ModelProfile.model_validate(data)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:11434",
        "file:///tmp/a",
        "http://user@localhost:11434",
        "http://localhost:11434/prefix",
        "http://localhost:11434?query=1",
    ],
)
def test_runtime_must_be_loopback(url):
    with pytest.raises(ValueError, match="loopback"):
        LocalModels(url)


def test_installed_is_not_resident(profile, api):
    replies, calls = api
    replies["/api/tags"] = {"models": [entry(profile)]}
    client = LocalModels()
    assert client.inventory()[0].name == profile.ollama_model
    status = client.status(profile)
    assert status.installed and status.digest_matches and status.metadata_matches
    assert not status.resident and not status.ready
    assert status.model_dump()["ready"] is False
    assert all(payload is None for _, payload in calls)


def test_missing_model_is_not_installed(profile, api):
    status = LocalModels().status(profile)
    assert not status.installed and not status.resident and not status.digest_matches
    assert status.actual_digest is None


@pytest.mark.parametrize(
    "installed_digest,resident_digest,ready",
    [
        ("a" * 64, "a" * 64, True),
        ("b" * 64, "a" * 64, False),
        ("a" * 64, "b" * 64, False),
    ],
)
def test_installed_and_resident_identity_are_separate(
    profile,
    api,
    installed_digest,
    resident_digest,
    ready,
):
    replies, _ = api
    replies["/api/tags"] = {"models": [entry(profile, digest=installed_digest)]}
    replies["/api/ps"] = {"models": [entry(profile, digest=resident_digest)]}
    status = LocalModels().status(profile)
    assert status.installed and status.resident
    assert status.actual_digest == installed_digest
    assert status.resident_digest == resident_digest
    assert status.ready is ready


def test_warm_uses_promptless_keep_alive_and_checks_residency(profile, api):
    replies, calls = api
    replies["/api/tags"] = {"models": [entry(profile)]}

    def warm_reply():
        replies["/api/ps"] = {"models": [entry(profile)]}
        return {"done": True, "done_reason": "load"}

    replies["/api/generate"] = warm_reply
    assert LocalModels().warm(profile).ready
    writes = [(endpoint, payload) for endpoint, payload in calls if payload is not None]
    assert writes == [
        (
            "/api/generate",
            {
                "model": profile.ollama_model,
                "keep_alive": -1,
                "stream": False,
            },
        )
    ]
    assert calls[-1] == ("/api/ps", None)


@pytest.mark.parametrize("problem", ["missing", "digest", "metadata", "resident_digest"])
def test_warm_refuses_unverified_identity(profile, api, problem):
    replies, calls = api
    if problem != "missing":
        model = entry(profile)
        if problem == "digest":
            model["digest"] = "b" * 64
        if problem == "metadata":
            model["details"]["family"] = "other"
        replies["/api/tags"] = {"models": [model]}
    if problem == "resident_digest":
        replies["/api/ps"] = {"models": [entry(profile, digest="b" * 64)]}
    with pytest.raises(LocalModelError):
        LocalModels().warm(profile)
    assert all(payload is None for _, payload in calls)


def test_warm_refuses_to_risk_eviction_of_other_models(profile, api):
    replies, calls = api
    replies["/api/tags"] = {"models": [entry(profile)]}
    replies["/api/ps"] = {"models": [entry(profile, name="other:latest")]}
    with pytest.raises(LocalModelError, match="eviction"):
        LocalModels().warm(profile)
    assert all(payload is None for _, payload in calls)


def test_warm_refresh_of_resident_target_does_not_unload_other_models(profile, api):
    replies, calls = api
    replies["/api/tags"] = {"models": [entry(profile)]}
    replies["/api/ps"] = {"models": [entry(profile), entry(profile, name="other:latest")]}
    replies["/api/generate"] = {"done": True}
    assert LocalModels().warm(profile).ready
    assert [payload for _, payload in calls if payload] == [
        {"model": profile.ollama_model, "keep_alive": -1, "stream": False},
    ]


def test_warm_success_response_is_not_proof_of_residency(profile, api):
    replies, _ = api
    replies["/api/tags"] = {"models": [entry(profile)]}
    replies["/api/generate"] = {"done": True}
    with pytest.raises(LocalModelError, match="without verified"):
        LocalModels().warm(profile)


def test_inventory_rejects_malformed_response(api):
    replies, _ = api
    replies["/api/tags"] = {"models": [{"name": "model:latest"}]}
    with pytest.raises(LocalModelError, match="Invalid Ollama inventory"):
        LocalModels().inventory()


class FakeOpener:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.reply, Exception):
            raise self.reply
        return io.BytesIO(self.reply)


def test_http_post_is_json_and_not_streamed(profile):
    client = LocalModels()
    client._opener = FakeOpener(b'{"done": true}')
    payload = {"model": profile.ollama_model, "keep_alive": -1, "stream": False}
    assert client._request("/api/generate", payload) == {"done": True}
    request, timeout = client._opener.calls[0]
    assert request.full_url == "http://127.0.0.1:11434/api/generate"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == payload
    assert timeout == 300


@pytest.mark.parametrize(
    "reply,match",
    [
        (URLError("connection refused"), "unavailable"),
        (TimeoutError("timed out"), "unavailable"),
        (b"not json", "invalid response"),
        (b"[]", "JSON object"),
        (b'{"error":"model requires more system memory"}', "more system memory"),
        (
            HTTPError("http://localhost", 500, "error", {}, io.BytesIO(b"out of memory")),
            "out of memory",
        ),
    ],
)
def test_runtime_failures_are_explicit(reply, match):
    client = LocalModels()
    client._opener = FakeOpener(reply)
    with pytest.raises(LocalModelError, match=match):
        client._request("/api/tags")


def test_redirects_cannot_escape_local_endpoint():
    with pytest.raises(LocalModelError, match="redirected"):
        local_models._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com")


@pytest.fixture
def model_store(tmp_path, profile):
    content = b"GGUFfixture weights"
    blob_digest = hashlib.sha256(content).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "layers": [
            {
                "mediaType": "application/vnd.ollama.image.template",
                "digest": "sha256:" + "c" * 64,
                "size": 7,
            },
            {
                "mediaType": "application/vnd.ollama.image.model",
                "digest": "sha256:" + blob_digest,
                "size": len(content),
            },
        ],
    }
    manifest_path = tmp_path / "manifests/registry.ollama.ai/library/qwen2.5/7b"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest))
    blob = tmp_path / "blobs" / ("sha256-" + blob_digest)
    blob.parent.mkdir()
    blob.write_bytes(content)
    pinned = profile.model_copy(
        update={
            "expected_digest": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        }
    )
    return LocalModels(models_dir=tmp_path), pinned, manifest_path, blob


def test_gguf_resolves_model_layer_not_manifest_digest(model_store):
    client, profile, manifest, blob = model_store
    before = {p: p.read_bytes() for p in (manifest, blob)}
    assert client.gguf_path(profile) == blob
    assert profile.expected_digest not in blob.name
    assert before == {p: p.read_bytes() for p in (manifest, blob)}


@pytest.mark.parametrize(
    "problem,match",
    [
        ("manifest", "digest mismatch"),
        ("missing", "Cannot resolve"),
        ("truncated", "size"),
        ("header", "not a GGUF"),
    ],
)
def test_gguf_rejects_unverified_store(model_store, problem, match):
    client, profile, manifest, blob = model_store
    if problem == "manifest":
        manifest.write_text(manifest.read_text() + " ")
    elif problem == "missing":
        blob.unlink()
    elif problem == "truncated":
        blob.write_bytes(b"GGUF")
    else:
        blob.write_bytes(b"NOPE" + blob.read_bytes()[4:])
    with pytest.raises(LocalModelError, match=match):
        client.gguf_path(profile)


@pytest.mark.parametrize(
    "layers",
    [
        [],
        [
            {
                "mediaType": "application/vnd.ollama.image.model",
                "digest": "sha256:" + "a" * 64,
                "size": 4,
            },
        ]
        * 2,
    ],
)
def test_gguf_requires_one_model_layer(model_store, layers):
    client, profile, manifest, _ = model_store
    data = json.loads(manifest.read_text())
    data["layers"] = layers
    manifest.write_text(json.dumps(data))
    profile = profile.model_copy(
        update={
            "expected_digest": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        }
    )
    with pytest.raises(LocalModelError, match="exactly one"):
        client.gguf_path(profile)


def test_custom_model_store_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    assert LocalModels().models_dir == tmp_path
