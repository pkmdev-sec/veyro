from __future__ import annotations

import os
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from veyro.agents import AgentId


def _environment_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean environment value: {value!r}")


class JevProviderConfig(BaseModel):
    """Endpoint-scoped identity and connection settings for one Jev-compatible provider."""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    provider_id: str = Field(default="localjev-qwen3-14b", pattern=r"^[a-z0-9][a-z0-9._-]*$")
    base_url: HttpUrl = HttpUrl("http://127.0.0.1:8080")
    api_key_env: str = Field(default="TYPESAFE_API_KEY", pattern=r"^[A-Z_][A-Z0-9_]*$")
    request_model: str = Field(default="jev-latest", min_length=1)
    checkpoint: str = Field(
        default=(
            "qwen3:14b@sha256:bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8"
        ),
        min_length=1,
    )
    timeout_seconds: float = Field(default=10.0, gt=0.0)
    max_state_characters: int | None = Field(default=None, gt=0)
    state_format: Literal["kv", "json", "values"] = "kv"


class FactoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessment_min_interval_seconds: float = Field(default=5.0, ge=0.0)
    periodic_assessment_seconds: float = Field(default=30.0, gt=0.0)
    jev_provider: JevProviderConfig = Field(default_factory=JevProviderConfig)
    jev_shadow_provider: JevProviderConfig | None = None
    worker_timeout_seconds: float = Field(default=3_600.0, gt=0.0)
    overall_timeout_seconds: float = Field(default=7_200.0, gt=0.0)
    graceful_termination_seconds: float = Field(default=5.0, ge=0.0)
    max_concurrent_workers: int = Field(default=1, ge=1)
    max_workers: int = Field(default=3, ge=1)
    max_retries: int = Field(default=1, ge=0)
    max_iterations: int = Field(default=20, ge=1)
    agent_provider: AgentId = AgentId.CODEX
    codex_backend: Literal["app-server", "exec"] = "exec"
    steering_enabled: bool = True
    max_steers_per_worker: int = Field(default=1, ge=0)
    steering_grace_seconds: float = Field(default=30.0, ge=0.0)

    human_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    off_track_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    stuck_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    verification_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    finish_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    requirements_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    tests_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    implementation_for_verification_threshold: float = Field(default=0.75, ge=0.0, le=1.0)

    diff_limit: int = Field(default=20_000, ge=100)
    output_limit: int = Field(default=12_000, ge=100)
    field_limit: int = Field(default=50_000, ge=100)
    event_history_limit: int = Field(default=30, ge=1)
    worker_history_limit: int = Field(default=10, ge=1)

    @property
    def active_turn_steering_supported(self) -> bool:
        return self.agent_provider is AgentId.CODEX and self.codex_backend == "app-server"

    @classmethod
    def from_environment(cls) -> FactoryConfig:
        mapping: dict[str, tuple[str, Callable[[str], object]]] = {
            "VEYRO_ASSESSMENT_MIN_INTERVAL_SECONDS": (
                "assessment_min_interval_seconds",
                float,
            ),
            "VEYRO_PERIODIC_ASSESSMENT_SECONDS": ("periodic_assessment_seconds", float),
            "VEYRO_WORKER_TIMEOUT_SECONDS": ("worker_timeout_seconds", float),
            "VEYRO_OVERALL_TIMEOUT_SECONDS": ("overall_timeout_seconds", float),
            "VEYRO_MAX_WORKERS": ("max_workers", int),
            "VEYRO_MAX_RETRIES": ("max_retries", int),
            "VEYRO_MAX_ITERATIONS": ("max_iterations", int),
            "VEYRO_AGENT_PROVIDER": ("agent_provider", AgentId),
            "VEYRO_CODEX_BACKEND": ("codex_backend", str),
            "VEYRO_STEERING_ENABLED": (
                "steering_enabled",
                _environment_bool,
            ),
            "VEYRO_MAX_STEERS_PER_WORKER": ("max_steers_per_worker", int),
            "VEYRO_STEERING_GRACE_SECONDS": ("steering_grace_seconds", float),
        }
        values: dict[str, object] = {}
        for env_name, (field_name, converter) in mapping.items():
            value = os.getenv(env_name)
            if value is not None:
                values[field_name] = converter(value)

        provider_mapping: dict[str, tuple[str, Callable[[str], object]]] = {
            "VEYRO_JEV_PROVIDER_ID": ("provider_id", str),
            "VEYRO_JEV_BASE_URL": ("base_url", str),
            "VEYRO_JEV_API_KEY_ENV": ("api_key_env", str),
            "VEYRO_JEV_MODEL": ("request_model", str),
            "VEYRO_JEV_CHECKPOINT": ("checkpoint", str),
            "VEYRO_JEV_TIMEOUT_SECONDS": ("timeout_seconds", float),
            "VEYRO_JEV_MAX_STATE_CHARS": ("max_state_characters", int),
            "VEYRO_JEV_STATE_FORMAT": ("state_format", str),
        }
        provider_values: dict[str, object] = {}
        for env_name, (field_name, converter) in provider_mapping.items():
            value = os.getenv(env_name)
            if value is not None:
                provider_values[field_name] = converter(value)
        if provider_values:
            values["jev_provider"] = provider_values

        shadow_mapping: dict[str, tuple[str, Callable[[str], object]]] = {
            "VEYRO_JEV_SHADOW_PROVIDER_ID": ("provider_id", str),
            "VEYRO_JEV_SHADOW_BASE_URL": ("base_url", str),
            "VEYRO_JEV_SHADOW_API_KEY_ENV": ("api_key_env", str),
            "VEYRO_JEV_SHADOW_MODEL": ("request_model", str),
            "VEYRO_JEV_SHADOW_CHECKPOINT": ("checkpoint", str),
            "VEYRO_JEV_SHADOW_TIMEOUT_SECONDS": ("timeout_seconds", float),
            "VEYRO_JEV_SHADOW_MAX_STATE_CHARS": ("max_state_characters", int),
            "VEYRO_JEV_SHADOW_STATE_FORMAT": ("state_format", str),
        }
        shadow_values: dict[str, object] = {}
        for env_name, (field_name, converter) in shadow_mapping.items():
            value = os.getenv(env_name)
            if value is not None:
                shadow_values[field_name] = converter(value)
        if shadow_values:
            required = {"provider_id", "base_url", "checkpoint"}
            if missing := required - shadow_values.keys():
                names = ", ".join(sorted(missing))
                raise ValueError(f"shadow provider is missing required fields: {names}")
            values["jev_shadow_provider"] = shadow_values
        return cls(**values)
