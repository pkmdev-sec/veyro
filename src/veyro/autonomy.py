from __future__ import annotations

import json
import math
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from veyro.agents import AgentId, agent_definition

_MACOS_LOOPBACK_SANDBOX = (
    "(version 1) (allow default) (deny network-outbound) "
    '(allow network-outbound (remote ip "localhost:*"))'
)


def _confine_local_network(
    command: list[str], deny_read_paths: tuple[Path, ...] = ()
) -> list[str]:
    if sys.platform == "darwin":
        sandbox = shutil.which("sandbox-exec")
        if sandbox is None:
            raise ValueError("local-only model execution requires sandbox-exec on macOS")
        profile = _MACOS_LOOPBACK_SANDBOX + " ".join(
            f" (deny file-read* (subpath {json.dumps(str(path.resolve()))}))"
            for path in deny_read_paths
        )
        return [sandbox, "-p", profile, *command]
    return command


@dataclass(frozen=True)
class CodingModel:
    coding_profile: str = "small"


@dataclass(frozen=True)
class HarnessModels(CodingModel):
    evaluation_profile: str = "laya"


@dataclass(frozen=True)
class AutonomyOptions:
    checks: tuple[str, ...]
    max_continuations: int = 6
    check_timeout: float = 120
    model_turn_timeout: float = 120
    timeout: float = 1800
    judge_file: Path | None = None
    models: CodingModel | HarnessModels | None = None
    allow_uncalibrated_judge: bool = False
    deny_read_paths: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if not self.checks or any(not command.strip() for command in self.checks):
            raise ValueError("autonomy requires at least one nonempty --check command")
        if self.max_continuations < 0:
            raise ValueError("max_continuations must be nonnegative")
        if any(
            not math.isfinite(n) or n <= 0
            for n in (self.check_timeout, self.model_turn_timeout, self.timeout)
        ):
            raise ValueError("autonomy timeouts must be finite and positive")
        if any(not path.exists() for path in self.deny_read_paths):
            raise ValueError("deny_read_paths must exist")


@dataclass(frozen=True, slots=True)
class VerifiedCodingModel:
    profile_id: str
    requested_model: str
    manifest_sha256: str
    blob_sha256: str

    @property
    def provenance(self) -> str:
        return (
            f"{self.requested_model}"
            f"@manifest-sha256:{self.manifest_sha256}"
            f"@blob-sha256:{self.blob_sha256}"
        )


@dataclass(frozen=True)
class AutonomousLaunch:
    command: list[str]
    environment: dict[str, str]
    directory: Path
    coding_model: VerifiedCodingModel | None = None


@dataclass(frozen=True)
class AutonomyPlan:
    command: list[str]
    environment: dict[str, str]
    directory: Path
    files: dict[str, str]
    coding_model: VerifiedCodingModel | None

    def commit(self) -> AutonomousLaunch:
        parent = self.directory.parent
        root = parent.parent
        root_existed = root.exists()
        parent_existed = parent.exists()
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        staging = parent / f".{self.directory.name}.{uuid4().hex}.tmp"
        try:
            os.mkdir(staging, 0o700)
            for name, content in self.files.items():
                path = staging / name
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.replace(staging, self.directory)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            if not parent_existed:
                try:
                    parent.rmdir()
                except OSError:
                    pass
            if not root_existed:
                try:
                    root.rmdir()
                except OSError:
                    pass
            raise
        return AutonomousLaunch(
            command=self.command,
            environment=self.environment,
            directory=self.directory,
            coding_model=self.coding_model,
        )


def _json(value: object) -> str:
    return json.dumps(value, indent=2, allow_nan=False) + "\n"


def build_autonomy_plan(
    provider: AgentId,
    repository: Path,
    prompt: str,
    native_args: tuple[str, ...],
    options: AutonomyOptions,
) -> AutonomyPlan:
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
    evaluation_model = None
    native_ollama_tools = False
    model_roles = None
    if options.models:
        from veyro.local_evaluation import load_evaluation_profiles, model_identity
        from veyro.local_models import LocalModels, load_profiles

        coding_profiles = load_profiles()
        if options.models.coding_profile not in coding_profiles:
            raise ValueError(f"unknown local model profile: {options.models.coding_profile}")
        if isinstance(options.models, HarnessModels) and options.judge_file is None:
            raise ValueError("local harness models require a semantic evaluator file")
        if not isinstance(options.models, HarnessModels) and options.judge_file is not None:
            raise ValueError("local evaluation requires an explicit evaluation profile")
        coding_profile = coding_profiles[options.models.coding_profile]
        if provider is not AgentId.OPENCODE:
            raise ValueError("strict local-only coding currently supports only the OpenCode driver")
        if any(
            arg.split("=", 1)[0] in {"--model", "-m", "--provider", "--thinking"}
            for arg in native_args
        ):
            raise ValueError("local coding profile cannot be combined with native model overrides")
        verified = LocalModels().require_resident(coding_profile)
        coding_model = VerifiedCodingModel(
            profile_id=coding_profile.id,
            requested_model=coding_profile.ollama_model,
            manifest_sha256=verified.manifest_sha256,
            blob_sha256=verified.blob_sha256,
        )
        native_ollama_tools = coding_profile.model_family == "qwen3moe"
        model_roles = {
            "coding": {
                "profile": coding_profile.id,
                "model": coding_model.provenance,
            },
        }
        if isinstance(options.models, HarnessModels):
            evaluation_profiles = load_evaluation_profiles()
            if options.models.evaluation_profile not in evaluation_profiles:
                raise ValueError(
                    f"unknown local model profile: {options.models.evaluation_profile}"
                )
            evaluation_profile = evaluation_profiles[options.models.evaluation_profile]
            model_roles["evaluation"] = {
                "profile": evaluation_profile.id,
                "model": model_identity(evaluation_profile),
            }
            evaluation_model = model_roles["evaluation"]["model"]

    if options.allow_uncalibrated_judge and not (
        isinstance(options.models, HarnessModels) and options.judge_file
    ):
        raise ValueError(
            "experimental evaluator opt-in requires local harness models and an evaluator file"
        )

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
                    "expected_model": evaluation_model,
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
    config = {
        "schema_version": 1,
        "repository": str(repository),
        "checks": options.checks,
        "max_continuations": options.max_continuations,
        "check_timeout": options.check_timeout,
        "deadline": time.time() + options.timeout,
        "deny_read_paths": [str(path.resolve()) for path in options.deny_read_paths],
    }
    if judge is not None:
        config["judge"] = judge
        config["task"] = prompt
    if model_roles is not None:
        config["model_roles"] = model_roles

    instructions = (
        "Veyro autonomous task. Inspect the repository, implement the task end to end, and run "
        "the verification commands now. Veyro will run fresh authoritative checks after each "
        "turn and send a bounded repair packet when needed. Do not ask for routine approval. "
        "Make reasonable reversible choices, report blockers, and respect native permission "
        "denials. Do not edit Veyro state/config or weaken checks.\n"
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
    if not integration.is_file():
        raise ValueError(f"missing native autonomy integration: {integration}")
    entry = directory / "adapter.mjs"
    binding = {
        "python": sys.executable,
        "checker": str(Path(__file__).with_name("autonomy_check.py")),
        "config": str(directory / "config.json"),
        "deadline": config["deadline"],
        "maxMessageBytes": 24 * 1024,
        "maxModelSteps": 8,
    }
    if coding_model:
        binding["codingModel"] = coding_model.requested_model
        binding["codingBaseUrl"] = "http://127.0.0.1:11434/v1"
    adapter_source = (
        f"import create from {json.dumps(integration.as_uri())};\n"
        f"export default create({json.dumps(binding)});\n"
    )

    definition = agent_definition(provider)
    environment = {}
    if provider is AgentId.PRIME_AGENT:
        args = ("--extension", str(entry), *native_args)
        command = definition.interactive_command(instructions, args)
    else:
        override = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT", "{}"))
        if not isinstance(override, dict):
            raise ValueError("OpenCode configuration must be an object")
        plugins = override.get("plugin", [])
        if not isinstance(plugins, list):
            raise ValueError("OpenCode plugin configuration must be an array")
        override["plugin"] = [*plugins, entry.as_uri()]
        agents = override.setdefault("agent", {})
        if not isinstance(agents, dict):
            raise ValueError("OpenCode agent configuration must be an object")
        build = agents.setdefault("build", {})
        if not isinstance(build, dict):
            raise ValueError("OpenCode build agent configuration must be an object")
        permissions = build.setdefault("permission", {})
        if not isinstance(permissions, dict):
            raise ValueError("build agent permission must be an object for autonomous launch")
        permissions.update({"question": "deny", "plan_enter": "deny", "plan_exit": "deny"})
        local_args = ()
        if coding_model:
            title = agents.setdefault("title", {})
            if not isinstance(title, dict):
                raise ValueError("OpenCode title agent configuration must be an object")
            title["disable"] = True
            build["temperature"] = 0
            build["steps"] = binding["maxModelSteps"]
            build["tools"] = {
                "skill": False,
                "task": False,
                "todowrite": False,
                "webfetch": False,
                "websearch": False,
            }
            timeout_ms = int(options.model_turn_timeout * 1000)
            providers = override.setdefault("provider", {})
            if not isinstance(providers, dict):
                raise ValueError("OpenCode provider configuration must be an object")
            providers["veyro-local"] = {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Veyro local Qwen",
                "options": {
                    "baseURL": binding["codingBaseUrl"],
                    "apiKey": "ollama",
                    "timeout": timeout_ms,
                    "headerTimeout": timeout_ms,
                    "chunkTimeout": timeout_ms,
                },
                "models": {
                    coding_model.requested_model: {
                        "name": coding_model.requested_model,
                        "reasoning": True,
                        "options": {"reasoningEffort": "none"},
                        "limit": {"context": 16384, "output": 1024},
                    }
                },
            }
            override["small_model"] = f"veyro-local/{coding_model.requested_model}"
            override["tool_output"] = {"max_lines": 200, "max_bytes": 6000}
            override["compaction"] = {"auto": True, "prune": True, "tail_turns": 2}
            local_args = ("--model", f"veyro-local/{coding_model.requested_model}")
            environment["OPENCODE_DISABLE_MODELS_FETCH"] = "true"
            environment["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
            if native_ollama_tools:
                environment["VEYRO_OLLAMA_NATIVE_PROXY"] = "1"
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(override, allow_nan=False)
        native_command = [
            definition.executable,
            "run",
            "--format",
            "json",
            "--dir",
            str(repository),
            "--auto",
            *local_args,
            *native_args,
            "--agent",
            "build",
            instructions,
        ]
        command = [
            sys.executable,
            "-m",
            "veyro.opencode_autonomy",
            str(directory / "state.json"),
            *native_command,
        ]
    if coding_model:
        command = _confine_local_network(command, options.deny_read_paths)

    return AutonomyPlan(
        command=command,
        environment=environment,
        directory=directory,
        files={
            "config.json": _json(config),
            "state.json": _json({"phase": "building", "continuations": 0}),
            "adapter.mjs": adapter_source,
        },
        coding_model=coding_model,
    )


def prepare_autonomy(
    provider: AgentId,
    repository: Path,
    prompt: str,
    native_args: tuple[str, ...],
    options: AutonomyOptions,
) -> AutonomousLaunch:
    return build_autonomy_plan(provider, repository, prompt, native_args, options).commit()
