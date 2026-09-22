from __future__ import annotations

import pytest

from veyro.config import FactoryConfig, JevProviderConfig


def test_experimental_app_server_is_opt_in() -> None:
    config = FactoryConfig()
    assert config.codex_backend == "exec"
    assert config.active_turn_steering_supported is False
    assert config.steering_enabled is True
    assert config.max_steers_per_worker == 1
    assert config.steering_grace_seconds == 30


def test_steering_environment_overrides(monkeypatch) -> None:
    monkeypatch.setenv("VEYRO_CODEX_BACKEND", "app-server")
    monkeypatch.setenv("VEYRO_STEERING_ENABLED", "false")
    monkeypatch.setenv("VEYRO_MAX_STEERS_PER_WORKER", "2")
    monkeypatch.setenv("VEYRO_STEERING_GRACE_SECONDS", "12.5")

    config = FactoryConfig.from_environment()
    assert config.codex_backend == "app-server"
    assert config.active_turn_steering_supported is True
    assert config.steering_enabled is False
    assert config.max_steers_per_worker == 2
    assert config.steering_grace_seconds == 12.5


def test_invalid_steering_boolean_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("VEYRO_STEERING_ENABLED", "sometimes")
    with pytest.raises(ValueError, match="invalid boolean"):
        FactoryConfig.from_environment()


def test_jev_provider_configuration_is_endpoint_scoped(monkeypatch) -> None:
    monkeypatch.setenv("VEYRO_JEV_PROVIDER_ID", "localjev-qwen")
    monkeypatch.setenv("VEYRO_JEV_BASE_URL", "http://127.0.0.1:8081")
    monkeypatch.setenv("VEYRO_JEV_API_KEY_ENV", "LOCALJEV_CLIENT_KEY")
    monkeypatch.setenv("VEYRO_JEV_MODEL", "localjev-0.2")
    monkeypatch.setenv("VEYRO_JEV_CHECKPOINT", "qwen3:14b@sha256:abc")
    monkeypatch.setenv("VEYRO_JEV_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("LOCALJEV_CLIENT_KEY", "must-not-enter-config")

    provider = FactoryConfig.from_environment().jev_provider

    assert provider.provider_id == "localjev-qwen"
    assert str(provider.base_url) == "http://127.0.0.1:8081/"
    assert provider.api_key_env == "LOCALJEV_CLIENT_KEY"
    assert provider.request_model == "localjev-0.2"
    assert provider.checkpoint == "qwen3:14b@sha256:abc"
    assert provider.timeout_seconds == 45
    assert "must-not-enter-config" not in provider.model_dump_json()


@pytest.mark.parametrize(
    ("field", "value"),
    [("base_url", "not-a-url"), ("api_key_env", "not an env name")],
)
def test_jev_provider_rejects_invalid_boundaries(field, value) -> None:
    with pytest.raises(ValueError):
        JevProviderConfig(**{field: value})


def test_jev_provider_rejects_remote_endpoint() -> None:
    with pytest.raises(ValueError, match="loopback"):
        JevProviderConfig(base_url="https://remote.example")


def clear_shadow_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "VEYRO_JEV_SHADOW_PROVIDER_ID",
        "VEYRO_JEV_SHADOW_BASE_URL",
        "VEYRO_JEV_SHADOW_API_KEY_ENV",
        "VEYRO_JEV_SHADOW_MODEL",
        "VEYRO_JEV_SHADOW_CHECKPOINT",
        "VEYRO_JEV_SHADOW_TIMEOUT_SECONDS",
        "VEYRO_JEV_SHADOW_MAX_STATE_CHARS",
        "VEYRO_JEV_SHADOW_STATE_FORMAT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_shadow_provider_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_shadow_environment(monkeypatch)

    assert FactoryConfig.from_environment().jev_shadow_provider is None


def test_shadow_provider_uses_an_independent_endpoint_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_shadow_environment(monkeypatch)
    monkeypatch.setenv("VEYRO_JEV_SHADOW_PROVIDER_ID", "jeff-gliformer")
    monkeypatch.setenv("VEYRO_JEV_SHADOW_BASE_URL", "http://127.0.0.1:8081")
    monkeypatch.setenv("VEYRO_JEV_SHADOW_API_KEY_ENV", "JEFF_API_KEY")
    monkeypatch.setenv("VEYRO_JEV_SHADOW_MODEL", "jev-latest")
    monkeypatch.setenv("VEYRO_JEV_SHADOW_CHECKPOINT", "gliformer@sha256:abc")
    monkeypatch.setenv("VEYRO_JEV_SHADOW_TIMEOUT_SECONDS", "4.5")
    monkeypatch.setenv("VEYRO_JEV_SHADOW_MAX_STATE_CHARS", "20000")
    monkeypatch.setenv("VEYRO_JEV_SHADOW_STATE_FORMAT", "kv")

    shadow = FactoryConfig.from_environment().jev_shadow_provider

    assert shadow is not None
    assert shadow.provider_id == "jeff-gliformer"
    assert str(shadow.base_url).rstrip("/") == "http://127.0.0.1:8081"
    assert shadow.api_key_env == "JEFF_API_KEY"
    assert shadow.checkpoint == "gliformer@sha256:abc"
    assert shadow.timeout_seconds == 4.5
    assert shadow.max_state_characters == 20_000
    assert shadow.state_format == "kv"


def test_partial_shadow_provider_configuration_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_shadow_environment(monkeypatch)
    monkeypatch.setenv("VEYRO_JEV_SHADOW_BASE_URL", "http://127.0.0.1:8081")

    with pytest.raises(ValueError, match="checkpoint, provider_id"):
        FactoryConfig.from_environment()
