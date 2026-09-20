# Veyro documentation

Veyro combines localjev and Qwen3-14B with version-pinned native-agent adapters.
Start with a synthetic assessment, then connect to a session you own.

## Release scope

This release supports read-only observation and explicitly approved controls. No
adapter qualifies for automatic delivery. Autonomous tasks, local GGUF readout, and
evaluator/calibration additions are excluded. See [scope and checks](release-scope.md).

## Start here

- [Deploy localjev with Qwen3-14B](localjev.md): endpoint, model digest, compatibility, and trust checks.
- [Run a local assessment](../examples/README.md): a real model call with no agent connection or controls.
- [Operate existing sessions](supervision-operator-guide.md): discovery, read-only observation, exact approval, and recovery.
- [Upgrade an installation](upgrading.md): package, configuration, journals, and no-retry ledgers.

## Model variants

- [Qwen3 4B Instruct and Qwen3 14B](qwen-models.md): sizes, memory tradeoffs, use cases, and release status.

## Reference

- [Capabilities, version pins, and privacy](supervision-reference.md).
- [Control policies and approval protocol](supervision-rollout.md).
- [Read-only attachment and history limits](existing-session-attachment.md).
- [Prime daemon bridge](prime-agent-daemon-bridge.md).
- [OpenCode HTTP/SSE bridge](opencode-server-bridge.md).
- [Codex hook listener](codex-hooks-bridge.md).

## Architecture and evidence

- [Semantic assessment and deterministic control](theory.md).
- [How localjev maps Qwen3-14B to typed judgments](why-jev.md).
- [Verification commands and recorded native checks](supervision-verification.md).
- [What the evidence establishes](what-veyro-proves.md).
- [Disposable Prime control check](prime-agent-supervision-canary.md).

## Separate interfaces

- [Native agent launcher](agent-router.md): keep the native terminal and add lifecycle observation.
- [Factory runtime](runtime.md): worker execution and its separate content-retention contract.
- [Codex App Server steering](steering.md): experimental, explicit opt-in.
- [Optional jeff shadow validation](jeff-shadow-canary.md): disabled by default, never a substitute for localjev.

Return to the [project README](../README.md) for installation and a system overview.
