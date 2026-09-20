from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from veyro.observation import FactoryObservation
from veyro.veyro import JevVeyroModel, VeyroModelError
from veyro.veyro.jev import (
    ASSESSMENT_QUESTIONS,
    ASSESSMENT_QUESTIONS_VERSION,
    normalize_assessment,
    parse_jev_response,
)


def values(value: float = 0.5) -> dict[str, float]:
    return dict.fromkeys(ASSESSMENT_QUESTIONS, value)


def observation() -> FactoryObservation:
    return FactoryObservation(
        original_job="job",
        run_id="abc",
        factory_status="RUNNING",
        iteration=1,
        active_workers=[],
        worker_history=[],
        latest_worker_output="",
        worker_exit_status={},
        worker_elapsed_seconds={},
        git_status="",
        git_diff="",
        changed_files=[],
        test_results=[],
        verification_results=[],
        recent_events=[],
        previous_assessment=None,
        previous_intervention=None,
        attempts=0,
        failures=[],
        elapsed_factory_seconds=0,
    )


def test_pinned_localjev_baseline_matches_assessment_questions() -> None:
    baseline_path = Path(__file__).parents[1] / "config/baselines/localjev-qwen3-14b.json"
    baseline = json.loads(baseline_path.read_text())
    canonical_questions = json.dumps(
        ASSESSMENT_QUESTIONS,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()

    assert baseline["role"] == "authoritative"
    assert baseline["service"]["revision"] == "0a2d1b889ce1a056e13feddce8fd04532c116d78"
    assert baseline["upstream"]["model"] == "qwen3:14b"
    assert baseline["questions"]["version"] == ASSESSMENT_QUESTIONS_VERSION
    assert baseline["questions"]["sha256"] == hashlib.sha256(canonical_questions).hexdigest()


def test_jeff_shadow_manifest_is_pinned_disabled_and_hardened() -> None:
    manifest_path = (
        Path(__file__).parents[1] / "config/baselines/jeff-gliformer-large-v1-shadow.json"
    )
    manifest = json.loads(manifest_path.read_text())
    canonical_questions = json.dumps(
        ASSESSMENT_QUESTIONS,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()

    assert manifest["enabled"] is False
    assert manifest["role"] == "shadow"
    assert manifest["service"]["revision"] == "230d85d29e5df3454f7eb1aad374da01808191a2"
    assert manifest["model"]["revision"] == "d0a4e53d09cebe6bc963dd9be319d4279084bb2d"
    assert manifest["model"]["expected_sha256"] == (
        "f80b29199d66f878669f283703e4dba9fd726755dcc20aba1ed0d24fce4a23f1"
    )
    assert manifest["server"]["host"] == "127.0.0.1"
    assert manifest["server"]["authentication_required"] is True
    assert manifest["server"]["max_state_characters"] == 20_000
    assert manifest["questions"]["version"] == ASSESSMENT_QUESTIONS_VERSION
    assert manifest["questions"]["sha256"] == hashlib.sha256(canonical_questions).hexdigest()
    assert manifest["verification"]["live_canary"] == "blocked"


def test_valid_jev_assessment_parsing() -> None:
    response = SimpleNamespace(
        nouls={name: SimpleNamespace(noul=value) for name, value in values(0.75).items()}
    )
    assert parse_jev_response(response).tests_sufficient == 0.75


def test_dictionary_jev_assessment_parsing() -> None:
    response = {"answers": {name: {"noul": value} for name, value in values(0.4).items()}}
    assert parse_jev_response(response).worker_stuck == 0.4


def test_malformed_jev_response() -> None:
    with pytest.raises(VeyroModelError, match="omitted"):
        parse_jev_response({"answers": {}})


def test_assessment_normalization() -> None:
    raw = values()
    raw["worker_stuck"] = 1.001
    raw["work_off_track"] = -0.001
    result = normalize_assessment(raw)
    assert result.worker_stuck == 1.0
    assert result.work_off_track == 0.0


class Client:
    def __init__(self, result=None, error=None, delay=0.0) -> None:
        self.result = result
        self.error = error
        self.delay = delay
        self.calls = []

    async def system_one(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_jev_model_builds_parallel_noul_call() -> None:
    response = SimpleNamespace(
        nouls={name: SimpleNamespace(noul=value) for name, value in values(0.6).items()}
    )
    client = Client(result=response)
    result = await JevVeyroModel(
        provider_id="localjev-qwen",
        base_url="http://127.0.0.1:8080",
        model="localjev-0.2",
        checkpoint="qwen3:14b@sha256:abc",
        timeout_seconds=45,
        client=client,
    ).assess(observation())
    assert result.ready_to_finish == 0.6
    assert result.provenance is not None
    assert result.provenance.provider_id == "localjev-qwen"
    assert result.provenance.role == "authoritative"
    assert result.provenance.checkpoint == "qwen3:14b@sha256:abc"
    assert result.provenance.question_version == ASSESSMENT_QUESTIONS_VERSION
    assert result.provenance.inference.timeout_seconds == 45
    assert result.provenance.inference.latency_seconds >= 0
    assert set(client.calls[0]["questions"]) == set(ASSESSMENT_QUESTIONS)
    assert client.calls[0]["model"] == "localjev-0.2"


@pytest.mark.asyncio
async def test_jev_failure_is_translated() -> None:
    model = JevVeyroModel(client=Client(error=RuntimeError("offline")))
    with pytest.raises(VeyroModelError, match="RuntimeError"):
        await model.assess(observation())


@pytest.mark.asyncio
async def test_jev_timeout_is_translated() -> None:
    model = JevVeyroModel(client=Client(delay=1), timeout_seconds=0.01)
    with pytest.raises(VeyroModelError, match="timed out"):
        await model.assess(observation())


def test_jev_model_does_not_fall_back_to_process_global_credentials() -> None:
    with pytest.raises(ValueError, match="explicit API key"):
        JevVeyroModel()


def test_jev_model_builds_an_endpoint_scoped_client(monkeypatch) -> None:
    import typesafe_sdk

    captured = {}

    class SDKClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://wrong.example")
    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", SDKClient)
    model = JevVeyroModel(
        provider_id="localjev-qwen",
        base_url="http://127.0.0.1:8081",
        api_key="local-only",
        model="localjev-0.2",
        checkpoint="qwen3:14b@sha256:abc",
        timeout_seconds=45,
    )

    model._make_client()

    assert captured["base_url"] == "http://127.0.0.1:8081"
    assert captured["api_key"] == "local-only"
    assert captured["model"] == "localjev-0.2"
    assert captured["timeout"] == 45
    assert model.provider_id == "localjev-qwen"
    assert model.checkpoint == "qwen3:14b@sha256:abc"


@pytest.mark.asyncio
async def test_jev_model_preflights_provider_state_limit() -> None:
    client = Client()
    model = JevVeyroModel(
        client=client,
        provider_id="jeff",
        role="shadow",
        max_state_characters=10,
        state_format="kv",
    )
    oversized = observation().model_copy(update={"latest_worker_output": "x" * 100})

    with pytest.raises(VeyroModelError, match="state preflight"):
        await model.assess(oversized)

    assert client.calls == []
