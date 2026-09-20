from __future__ import annotations

import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from veyro.agents import AgentId, agent_definition


@dataclass(frozen=True)
class CodingModel:
    coding_profile: str = "small"


@dataclass(frozen=True)
class HarnessModels(CodingModel):
    evaluation_profile: str = "14b"


@dataclass(frozen=True)
class AutonomyOptions:
    checks: tuple[str, ...]
    max_continuations: int = 6
    check_timeout: float = 120
    timeout: float = 1800
    judge_file: Path | None = None
    models: CodingModel | HarnessModels | None = None
    allow_uncalibrated_judge: bool = False

    def __post_init__(self) -> None:
        if not self.checks or any(not command.strip() for command in self.checks):
            raise ValueError("autonomy requires at least one nonempty --check command")
        if self.max_continuations < 1:
            raise ValueError("max_continuations must be positive")
        if any(not math.isfinite(n) or n <= 0 for n in (self.check_timeout, self.timeout)):
            raise ValueError("autonomy timeouts must be finite and positive")


@dataclass(frozen=True)
class AutonomousLaunch:
    command: list[str]
    environment: dict[str, str]
    directory: Path


def prepare_autonomy(
    provider: AgentId,
    repository: Path,
    prompt: str,
    native_args: tuple[str, ...],
    options: AutonomyOptions,
) -> AutonomousLaunch:
    if provider not in (AgentId.PRIME_AGENT, AgentId.OPENCODE):
        raise ValueError(f"no native autonomy adapter for {provider}")
    if not prompt.strip():
        raise ValueError("autonomy requires a nonempty --prompt")
    conflicting = {
        "--continue",
        "-c",
        "--resume",
        "-r",
        "--session",
        "-s",
        "--fork",
        "--pure",
        "--autonomous",
        "--goal",
        "--agent",
    }
    if any(arg.split("=", 1)[0] in conflicting for arg in native_args):
        raise ValueError("autonomy requires a new session without another autonomous controller")
    coding_model = None
    model_roles = None
    if options.models:
        from veyro.local_evaluation import model_identity
        from veyro.local_models import load_profiles

        profiles = load_profiles()
        requested = {options.models.coding_profile}
        if isinstance(options.models, HarnessModels):
            requested.add(options.models.evaluation_profile)
        unknown = requested - profiles.keys()
        if unknown:
            raise ValueError(f"unknown local model profile: {', '.join(sorted(unknown))}")
        if isinstance(options.models, HarnessModels) and options.judge_file is None:
            raise ValueError("local harness models require a semantic evaluator file")
        if not isinstance(options.models, HarnessModels) and options.judge_file is not None:
            raise ValueError("local evaluation requires an explicit evaluation profile")
        coding_profile = profiles[options.models.coding_profile]
        coding_model = coding_profile.ollama_model
        model_roles = {
            "coding": {
                "profile": coding_profile.id,
                "model": model_identity(coding_profile),
            },
        }
        if isinstance(options.models, HarnessModels):
            evaluation_profile = profiles[options.models.evaluation_profile]
            model_roles["evaluation"] = {
                "profile": evaluation_profile.id,
                "model": model_identity(evaluation_profile),
            }
    if options.allow_uncalibrated_judge and not (
        isinstance(options.models, HarnessModels) and options.judge_file
    ):
        raise ValueError(
            "experimental evaluator opt-in requires local harness models and an evaluator file"
        )
    if coding_model and any(
        arg.split("=", 1)[0] in {"--model", "-m", "--provider", "--thinking"} for arg in native_args
    ):
        raise ValueError("local coding profile cannot be combined with native model overrides")
    judge = None
    if options.judge_file is not None:
        from pydantic import ValidationError

        from veyro.native_judge import JudgeConfig

        try:
            data = json.loads(options.judge_file.read_text())
            if isinstance(options.models, HarnessModels):
                if not isinstance(data, dict):
                    raise ValueError("judge configuration must be an object")
                judge_provider = data.get("provider", {})
                if not isinstance(judge_provider, dict):
                    raise ValueError("judge provider must be an object")
                data["provider"] = {
                    **(judge_provider if judge_provider.get("kind") == "local" else {}),
                    "kind": "local",
                    "profile": options.models.evaluation_profile,
                    "allow_uncalibrated": options.allow_uncalibrated_judge
                    or judge_provider.get("allow_uncalibrated", False),
                }
            judge_config = JudgeConfig.model_validate(data)
            from veyro.native_judge import LocalJudgeProvider

            if isinstance(judge_config.provider, LocalJudgeProvider):
                judge_config.provider.calibrations = {
                    name: (options.judge_file.parent / path).resolve()
                    for name, path in judge_config.provider.calibrations.items()
                }
                if judge_config.provider.state_dir is not None:
                    judge_config.provider.state_dir = (
                        options.judge_file.parent / judge_config.provider.state_dir
                    ).resolve()
            judge = judge_config.model_dump(mode="json")
        except ValidationError:
            raise ValueError(
                "invalid semantic evaluator configuration; check rubric/provider fields"
            ) from None
    repository = repository.resolve()
    directory = repository / ".veyro" / "autonomy" / uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    plan_path = directory / "plan.md"
    config = {
        "schema_version": 1,
        "repository": str(repository),
        "plan_path": str(plan_path),
        "checks": options.checks,
        "max_continuations": options.max_continuations,
        "check_timeout": options.check_timeout,
        "deadline": time.time() + options.timeout,
    }
    if judge is not None:
        config["judge"] = judge
        config["task"] = prompt
    if model_roles is not None:
        config["model_roles"] = model_roles
    (directory / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    (directory / "state.json").write_text(
        json.dumps(
            {
                "phase": "planning",
                "continuations": 0,
            }
        )
        + "\n"
    )
    instructions = (
        "Veyro autonomous task. First inspect the repository and write a concrete plan to "
        f"{plan_path}. Include requirements, implementation steps, and verification. "
        "Do not implement during this first planning turn. End the turn after saving the plan. "
        "Veyro will automatically start the build and request repairs until checks pass. "
        "Do not ask for routine approval. Make reasonable reversible choices, report blockers, "
        "and respect native permission denials. Do not edit Veyro state/config or weaken checks.\n"
        f"Verification commands: {json.dumps(options.checks)}\nOriginal task:\n{prompt}"
    )
    if options.models:
        instructions += (
            "\nUse tools to make actual file edits; printing text does not create a file. "
            "If writing multiline text with Python, use outer triple-single-quoted strings "
            "and inner triple-double-quoted docstrings. "
            "After a tool error, correct the error and retry before claiming the file was saved."
        )
    if judge is not None:
        instructions += (
            "\nAfter executable checks pass, Veyro will evaluate these additional semantic "
            "criteria against only the listed evidence files. Ensure the evidence exists and "
            "satisfies each criterion. Do not modify the rubric.\n"
            + json.dumps({"criteria": judge["criteria"], "evidence_files": judge["evidence_files"]})
        )
    if options.models:
        instructions += "\n/no_think"
    integration = Path(__file__).parent / "integrations" / f"{provider.value}.mjs"
    entry = directory / "adapter.mjs"
    binding = {
        "python": sys.executable,
        "checker": str(Path(__file__).with_name("autonomy_check.py")),
        "config": str(directory / "config.json"),
        "deadline": config["deadline"],
    }
    if coding_model:
        binding["codingModel"] = coding_model
        binding["codingBaseUrl"] = "http://127.0.0.1:11434/v1"
    entry.write_text(
        f"import create from {json.dumps(integration.as_uri())};\n"
        f"export default create({json.dumps(binding)});\n"
    )
    definition = agent_definition(provider)
    environment = {}
    if provider is AgentId.PRIME_AGENT:
        local_args = (
            ("--provider", "veyro-local", "--model", coding_model, "--thinking", "off")
            if coding_model
            else ()
        )
        args = ("--extension", str(entry), *local_args, *native_args)
        command = definition.interactive_command(instructions, args)
    else:
        override = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT", "{}"))
        plugins = override.get("plugin", [])
        override["plugin"] = [*plugins, entry.as_uri()]
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(override)
        agents = override.setdefault("agent", {})
        build = agents.setdefault("build", {})
        permissions = build.setdefault("permission", {})
        if not isinstance(permissions, dict):
            raise ValueError("build agent permission must be an object for autonomous launch")
        permissions.update({"question": "deny", "plan_enter": "deny", "plan_exit": "deny"})
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(override)
        local_args = ()
        if coding_model:
            agents.setdefault("title", {})["disable"] = True
            build["temperature"] = 0
            override.setdefault("provider", {})["veyro-local"] = {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Veyro local Qwen",
                "options": {"baseURL": binding["codingBaseUrl"], "apiKey": "ollama"},
                "models": {
                    coding_model: {
                        "name": coding_model,
                        "reasoning": True,
                        "options": {"reasoningEffort": "none"},
                        "limit": {"context": 32768, "output": 4096},
                    }
                },
            }
            override["small_model"] = f"veyro-local/{coding_model}"
            local_args = ("--model", f"veyro-local/{coding_model}")
            environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(override)
            environment["OPENCODE_DISABLE_MODELS_FETCH"] = "true"
            environment["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
        command = definition.interactive_command(
            instructions,
            ("--auto", *local_args, *native_args, "--agent", "build"),
        )
    return AutonomousLaunch(command, environment, directory)
