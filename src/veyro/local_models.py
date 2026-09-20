"""Pinned local Ollama profiles, residency checks, and read-only GGUF resolution."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field, field_validator


class LocalModelError(RuntimeError):
    """A local model is unavailable, unverified, or unsafe to load."""


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    ollama_model: str = Field(min_length=1)
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameter_size: str = Field(min_length=1)
    quantization: str = Field(min_length=1)
    model_family: str = Field(min_length=1)

    @field_validator("ollama_model")
    @classmethod
    def explicit_model_tag(cls, value: str) -> str:
        # Registry and namespace components map directly to Ollama manifest directories.
        if not re.fullmatch(r"[a-zA-Z0-9._:/-]+", value):
            raise ValueError("model must be an explicit Ollama model:tag")
        components = value.split("/")
        if len(components) > 3 or any(part in {"", ".", ".."} for part in components):
            raise ValueError("invalid Ollama model path")
        name, separator, tag = components[-1].partition(":")
        if not separator or not name or not tag or ":" in tag or name in {".", ".."}:
            raise ValueError("model must include an explicit tag")
        if tag in {".", ".."}:
            raise ValueError("invalid Ollama model tag")
        return value


def load_profiles(path: Path | None = None) -> dict[str, ModelProfile]:
    """Read a source checkout or wheel-installed share/veyro profile file."""
    if path is None:
        source = Path(__file__).resolve().parents[2] / "config" / "model-profiles.json"
        installed = Path(sys.prefix) / "share" / "veyro" / "model-profiles.json"
        path = source if source.is_file() else installed
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not data:
            raise ValueError("profiles must be a nonempty object keyed by profile ID")
        profiles = {key: ModelProfile.model_validate(value) for key, value in data.items()}
        if any(key != profile.id for key, profile in profiles.items()):
            raise ValueError("profile keys must match profile IDs")
        return profiles
    except (OSError, ValueError) as error:
        raise LocalModelError(f"Cannot load model profiles from {path}: {error}") from error


class ModelDetails(BaseModel):
    family: str
    parameter_size: str
    quantization_level: str


class OllamaModel(BaseModel):
    name: str
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    details: ModelDetails
    size_vram: int | None = Field(default=None, ge=0)
    expires_at: str | None = None


class _Inventory(BaseModel):
    models: list[OllamaModel]


class ModelStatus(BaseModel):
    profile: ModelProfile
    installed: bool
    resident: bool
    digest_matches: bool
    metadata_matches: bool
    resident_digest_matches: bool
    actual_digest: str | None
    resident_digest: str | None

    @computed_field
    @property
    def ready(self) -> bool:
        return (
            self.installed
            and self.resident
            and self.digest_matches
            and self.metadata_matches
            and self.resident_digest_matches
        )


class _Layer(BaseModel):
    mediaType: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size: int = Field(gt=0)


class _Manifest(BaseModel):
    schemaVersion: int
    layers: list[_Layer]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LocalModelError("Ollama endpoint redirected; refusing to leave the local endpoint")


class LocalModels:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        *,
        models_dir: Path | None = None,
        timeout_seconds: float = 300,
    ) -> None:
        url = urlsplit(base_url)
        if (
            url.scheme not in {"http", "https"}
            or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("Ollama base_url must be a loopback HTTP(S) origin")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self.base_url = base_url.rstrip("/")
        self.models_dir = (
            models_dir or Path(os.environ.get("OLLAMA_MODELS", "~/.ollama/models"))
        ).expanduser()
        self.timeout_seconds = timeout_seconds
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _request(self, endpoint: str, payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(
            self.base_url + endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError("expected a JSON object")
            if result.get("error"):
                raise LocalModelError(f"Ollama {endpoint}: {result['error']}")
            return result
        except HTTPError as error:
            detail = error.read(4096).decode("utf-8", errors="replace")
            raise LocalModelError(f"Ollama {endpoint}: HTTP {error.code}: {detail}") from error
        except (URLError, OSError, ValueError) as error:
            raise LocalModelError(
                f"Ollama unavailable or invalid response at {endpoint}: {error}"
            ) from error

    def _models(self, endpoint: str) -> list[OllamaModel]:
        try:
            return _Inventory.model_validate(self._request(endpoint)).models
        except ValidationError as error:
            raise LocalModelError(f"Invalid Ollama inventory at {endpoint}: {error}") from error

    def inventory(self) -> list[OllamaModel]:
        """Installed models, not loaded models. Does not run inference."""
        return self._models("/api/tags")

    def status(self, profile: ModelProfile) -> ModelStatus:
        """Check installed and resident identities separately; never loads a model."""
        installed = next((m for m in self.inventory() if m.name == profile.ollama_model), None)
        resident = next(
            (m for m in self._models("/api/ps") if m.name == profile.ollama_model), None
        )
        return ModelStatus(
            profile=profile,
            installed=installed is not None,
            resident=resident is not None,
            digest_matches=installed is not None and installed.digest == profile.expected_digest,
            metadata_matches=installed is not None
            and (
                installed.details.family == profile.model_family
                and installed.details.parameter_size == profile.parameter_size
                and installed.details.quantization_level == profile.quantization
            ),
            resident_digest_matches=resident is not None
            and (resident.digest == profile.expected_digest),
            actual_digest=installed.digest if installed else None,
            resident_digest=resident.digest if resident else None,
        )

    def warm(self, profile: ModelProfile) -> ModelStatus:
        """Load without a prompt, retain indefinitely, and verify actual residency.

        Refuse a new load while another model is resident: Ollama may otherwise evict it.
        Other clients can still race this check; this API cannot reserve Ollama memory.
        """
        before = self.status(profile)
        if not before.installed:
            raise LocalModelError(
                f"{profile.ollama_model} is not installed; no automatic downloads"
            )
        if not before.digest_matches or not before.metadata_matches:
            raise LocalModelError(f"{profile.id}: installed identity does not match pinned profile")
        if before.resident and not before.resident_digest_matches:
            raise LocalModelError(f"{profile.id}: resident digest does not match pinned profile")
        others = [m.name for m in self._models("/api/ps") if m.name != profile.ollama_model]
        if not before.resident and others:
            raise LocalModelError(
                f"Refusing to risk eviction of resident models: {', '.join(others)}"
            )
        result = self._request(
            "/api/generate", {"model": profile.ollama_model, "keep_alive": -1, "stream": False}
        )
        if result.get("done") is not True:
            raise LocalModelError(f"{profile.id}: Ollama did not complete the warm request")
        after = self.status(profile)
        if not after.ready:
            raise LocalModelError(f"{profile.id}: warm returned without verified pinned residency")
        return after

    def gguf_path(self, profile: ModelProfile) -> Path:
        """Resolve the model layer, not the manifest digest. Never loads or changes weights.

        Verify manifest SHA-256, model-layer file size, and GGUF header. This is not a
        full content hash of the multi-GB weight blob.
        """
        components = profile.ollama_model.split("/")
        name, tag = components[-1].split(":")
        namespace = components[-2] if len(components) >= 2 else "library"
        registry = components[0] if len(components) == 3 else "registry.ollama.ai"
        manifest_path = self.models_dir / "manifests" / registry / namespace / name / tag
        try:
            raw = manifest_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != profile.expected_digest:
                raise LocalModelError(f"{profile.id}: local manifest digest mismatch")
            manifest = _Manifest.model_validate_json(raw)
            if manifest.schemaVersion != 2:
                raise LocalModelError(f"{profile.id}: unsupported Ollama manifest schema")
            layers = [
                layer
                for layer in manifest.layers
                if layer.mediaType == "application/vnd.ollama.image.model"
            ]
            if len(layers) != 1:
                raise LocalModelError(f"{profile.id}: expected exactly one GGUF model layer")
            layer = layers[0]
            blob = self.models_dir / "blobs" / layer.digest.replace(":", "-")
            if blob.stat().st_size != layer.size:
                raise LocalModelError(f"{profile.id}: GGUF blob size does not match manifest")
            with blob.open("rb") as stream:
                if stream.read(4) != b"GGUF":
                    raise LocalModelError(f"{profile.id}: model layer is not a GGUF file")
            return blob
        except (OSError, ValueError) as error:
            raise LocalModelError(f"Cannot resolve GGUF for {profile.id}: {error}") from error
