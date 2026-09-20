from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from veyro.agents import AgentId
from veyro.autonomy import AutonomyOptions, CodingModel, HarnessModels, prepare_autonomy


def local_harness_options(
    tmp_path: Path,
    *,
    coding_profile: str = "small",
    evaluation_profile: str = "14b",
) -> AutonomyOptions:
    rubric = tmp_path / "rubric.json"
    rubric.write_text(
        json.dumps(
            {
                "rubric_version": "v1",
                "criteria": {"implemented": "Implementation exists"},
                "evidence_files": ["app.py"],
            }
        )
    )
    return AutonomyOptions(
        ("true",),
        judge_file=rubric,
        models=HarnessModels(coding_profile, evaluation_profile),
        allow_uncalibrated_judge=True,
    )


@pytest.mark.parametrize("provider", [AgentId.PRIME_AGENT, AgentId.OPENCODE])
@pytest.mark.parametrize(
    ("profile", "model"),
    [("small", "qwen3:4b-instruct-2507-q4_K_M"), ("14b", "qwen3:14b")],
)
def test_local_coding_is_scoped(tmp_path, monkeypatch, provider, profile, model):
    original = {"agent": {"build": {"permission": {"bash": {"rm *": "deny"}}}}}
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps(original))
    prepared = prepare_autonomy(
        provider,
        tmp_path,
        "Implement",
        (),
        AutonomyOptions(("true",), models=CodingModel(profile)),
    )
    snapshot = json.loads((prepared.directory / "config.json").read_text())
    assert snapshot["model_roles"]["coding"]["profile"] == profile
    assert "evaluation" not in snapshot["model_roles"]
    assert "judge" not in snapshot
    assert json.loads(__import__("os").environ["OPENCODE_CONFIG_CONTENT"]) == original
    assert not (tmp_path / "opencode.json").exists()
    assert model in (prepared.directory / "adapter.mjs").read_text()
    if provider is AgentId.OPENCODE:
        config = json.loads(prepared.environment["OPENCODE_CONFIG_CONTENT"])
        assert config["provider"]["veyro-local"]["options"]["baseURL"] == (
            "http://127.0.0.1:11434/v1"
        )
        assert config["agent"]["build"]["permission"]["bash"] == {"rm *": "deny"}
        assert config["small_model"] == f"veyro-local/{model}"
        assert config["agent"]["title"]["disable"] is True
        assert f"veyro-local/{model}" in prepared.command
    else:
        assert "veyro-local" in prepared.command
        assert model in prepared.command


@pytest.mark.parametrize("flag", ["--model=x", "-m", "--provider", "--thinking=high"])
def test_conflicting_local_model_override_rejected(tmp_path, flag):
    with pytest.raises(ValueError, match="native model overrides"):
        prepare_autonomy(
            AgentId.PRIME_AGENT,
            tmp_path,
            "Implement",
            (flag,),
            AutonomyOptions(("true",), models=CodingModel("small")),
        )


def test_dual_model_harness_requires_an_evaluator(tmp_path):
    with pytest.raises(ValueError, match="require a semantic evaluator file"):
        prepare_autonomy(
            AgentId.PRIME_AGENT,
            tmp_path,
            "Implement",
            (),
            AutonomyOptions(("true",), models=HarnessModels()),
        )
    assert not (tmp_path / ".veyro").exists()


def test_unknown_dual_model_profile_fails_before_session_creation(tmp_path):
    options = local_harness_options(tmp_path, evaluation_profile="missing")
    with pytest.raises(ValueError, match="unknown local model profile: missing"):
        prepare_autonomy(AgentId.PRIME_AGENT, tmp_path, "Implement", (), options)
    assert not (tmp_path / ".veyro").exists()


def test_prime_registers_a_local_provider_without_global_configuration():
    adapter = Path(__file__).parents[1] / "src/veyro/integrations/prime-agent.mjs"
    script = f"""
      import create from {json.dumps(adapter.as_uri())};
      let registered;
      create({{codingModel: "qwen3:14b", codingBaseUrl: "http://127.0.0.1:11434/v1"}})({{
        registerProvider(name, config) {{ registered = {{name, config}}; }}, on() {{}}
      }});
      process.stdout.write(JSON.stringify(registered));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    registered = json.loads(result.stdout)
    assert registered["name"] == "veyro-local"
    assert registered["config"]["baseUrl"] == "http://127.0.0.1:11434/v1"
    assert registered["config"]["models"][0]["id"] == "qwen3:14b"


def test_local_judge_and_native_provider_have_distinct_ownership(tmp_path):
    judge = tmp_path / "judge.json"
    judge.write_text(
        json.dumps(
            {
                "rubric_version": "v1",
                "criteria": {"implemented": "Implementation exists"},
                "evidence_files": ["app.py"],
            }
        )
    )
    prepared = prepare_autonomy(
        AgentId.PRIME_AGENT,
        tmp_path,
        "Implement",
        (),
        AutonomyOptions(
            ("true",),
            judge_file=judge,
            models=HarnessModels("small", "14b"),
            allow_uncalibrated_judge=True,
        ),
    )
    assert "veyro-local" in prepared.command
    config = json.loads((prepared.directory / "config.json").read_text())
    assert config["judge"]["provider"]["kind"] == "local"
    assert config["judge"]["provider"]["profile"] == "14b"
    assert config["model_roles"]["coding"]["profile"] == "small"
    assert config["model_roles"]["evaluation"]["profile"] == "14b"


def test_local_native_request_options_and_routing(tmp_path):
    integrations = Path(__file__).parents[1] / "src/veyro/integrations"
    script = f"""
      import prime from {json.dumps((integrations / "prime-agent.mjs").as_uri())};
      import opencode from {json.dumps((integrations / "opencode.mjs").as_uri())};
      const binding = {{codingModel: "qwen3:14b", codingBaseUrl: "http://127.0.0.1:11434/v1",
        config: {json.dumps(str(tmp_path / "config.json"))} }};
      const handlers = {{}};
      prime(binding)({{registerProvider() {{}}, on(name, handler) {{handlers[name] = handler;}} }});
      const payload = {{temperature: 1, reasoning_effort: "low", max_tokens: 4096,
        messages: [{{role: "system", content: "Keep native policies."}}]}};
      const request = handlers.before_provider_request({{payload}}, {{model: {{
        provider: "veyro-local",
        id: binding.codingModel, baseUrl: binding.codingBaseUrl}} }});
      const hooks = await opencode(binding)({{client: {{}} }});
      const output = {{temperature: 1, options: {{reasoningEffort: "low"}} }};
      await hooks["chat.params"]({{model: {{providerID: "veyro-local", id: binding.codingModel,
        api: {{url: ""}} }} }}, output);
      const system = {{system: ["Keep native policies."]}};
      await hooks["experimental.chat.system.transform"](
        {{model: {{providerID: "veyro-local"}}}}, system);
      console.log(JSON.stringify({{request, payload, output, system}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    data = json.loads(result.stdout)
    assert data["request"] == {
        "temperature": 0,
        "max_tokens": 4096,
        "reasoning_effort": "none",
        "messages": [{"role": "system", "content": "Keep native policies.\n/no_think"}],
    }
    assert data["payload"]["reasoning_effort"] == "low"
    assert data["payload"]["messages"][0]["content"] == "Keep native policies."
    assert data["system"]["system"] == ["Keep native policies.", "/no_think"]
    assert data["output"] == {"temperature": 0, "options": {"reasoningEffort": "none"}}
    requests = [
        json.loads(line) for line in (tmp_path / "model-requests.jsonl").read_text().splitlines()
    ]
    assert len(requests) == 2
    assert all(row["endpoint"] == "http://127.0.0.1:11434/v1" for row in requests)
    assert all(row["model"] == "qwen3:14b" for row in requests)
