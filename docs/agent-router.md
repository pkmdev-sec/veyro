# Agent router

Veyro supports five coding-agent harnesses through one executable interface without replacing any
provider's native user experience.

## Caller interface

```text
# Discover installed agents and the machine interface Veyro will use.
veyro agents
veyro agents --json

# Enter a native interactive harness. Arguments after `--` pass through unchanged.
veyro agent codex --repo . --prompt "Fix the failing test"
veyro agent claude --repo . -- --model sonnet
veyro agent prime-agent --repo .
veyro agent pi --repo .
veyro agent opencode --repo .

# Run one agent under Veyro's supervisor.
veyro run --agent claude --repo . --job "Fix the failing test"
```

`veyro agent` keeps Veyro alive as a sidecar parent and starts the native executable as the
foreground terminal process. The child inherits the real terminal directly; Veyro does not parse,
redraw, or proxy human-oriented terminal output. The provider still owns its terminal UI, commands,
configuration, credentials, session storage, and updates. The commands `codex`, `claude`,
`prime-agent`, `pi`, and `opencode` remain valid and do not depend on Veyro.

`veyro agents --json` is the language-neutral discovery interface. Shell scripts, Node programs,
Go programs, and other clients can consume it without importing Veyro's Python package.

## Boundary

```text
operator or automation
        |
        +-- native executable ----------------------> native interactive harness
        |
        +-- veyro agent --------------------------> managed native interactive harness
                |                                      (real foreground terminal)
                +-- Veyro sidecar ----------------> lifecycle + workspace evidence
        |
        +-- veyro run
                |
                v
        AgentDefinition + NativeCliWorker
                |
                +-- Codex exec JSON (default); App Server JSON-RPC (opt-in)
                +-- Claude stream-json
                +-- Prime Agent JSON mode
                +-- Pi JSON mode
                +-- OpenCode JSON events
```

A managed native session writes versioned evidence under
`.veyro/native-sessions/<session-id>/`. `session.json` records process identity and terminal status.
`events.jsonl` records ordered lifecycle and workspace-state events. Prompts, native arguments, terminal
output, environment values, and file contents are not persisted. The native child receives only the
session ID and local evidence paths through `VEYRO_SESSION_*` variables.

Direct invocation such as `claude` remains valid but is not automatically observed. Veyro is present
when the session is started with `veyro agent ...`; observing independently started sessions still
requires a provider hook, extension, or attach API. This boundary is explicit rather than claiming that
process discovery is equivalent to integration.

The registry describes an agent. It does not implement the agent's harness. A definition owns only:

- executable discovery and version probing;
- the documented machine-readable invocation used by a supervised worker;
- the native interactive invocation; and
- conservative adapter capability claims with source evidence.

`NativeCliWorker` owns bounded subprocess execution, event streaming, output limits, timeout, and
process-group cancellation. Codex keeps its deeper App Server adapter because that interface supports
active-turn steering. Other adapters report steering as unavailable. Veyro must stop or retry those
workers instead of pretending a steering request succeeded.

## Compatibility

Codex remains the default agent. Existing `VEYRO_CODEX_BACKEND`, Codex App Server behavior, and
persisted `codex_thread_id` and `codex_turn_id` fields remain supported. New records also carry neutral
`agent_id`, `session_id`, and `turn_id` fields. The legacy fields can be removed only through a separate
persisted-data migration.

For the version-pinned Codex hook listener, see [Codex hooks bridge](codex-hooks-bridge.md).
The factory defaults to `exec`; App Server controls require `VEYRO_CODEX_BACKEND=app-server`.

## Native autonomous tasks

`veyro agent prime-agent --autonomous --prompt JOB --check COMMAND` and the same
command with `opencode` load launch-scoped native hooks. The shared checker drives
plan, build, verification, and bounded repair without Veyro approval prompts.
This is different from the observation-only launcher described above.
See [native autonomy](native-autonomy.md) for the architecture and live benchmarks.

## Existing-session supervision

The launcher sidecar, legacy `veyro run` workers, and existing-session control
plane are separate interfaces. Starting a native agent does not automatically
connect its provider bridge or enable controls.

Use `veyro sessions` for metadata discovery, `veyro attach` for read-only
observation, and `veyro supervise` for one policy-gated proposal. See the
[operator guide](supervision-operator-guide.md),
[capability matrix](supervision-reference.md#adapter-capabilities), and
[version-pinned checks](supervision-verification.md).

| Provider | Implemented supervision bridge | Limits |
| --- | --- | --- |
| Codex `0.154.0` | Native command hooks and private metadata journal | Queue is an experimental opt-in adapter seam; no public control socket or native attach/replay |
| Prime Agent `0.9.5` | Internal daemon protocol 7, schema 29 | Controls require approval; no approval observation/reply |
| OpenCode `1.18.30` | Authenticated loopback HTTP/SSE | No replay, active-turn steering, or non-destructive stop |

Pi and Claude Code have no existing-session supervision bridge. Their native
launch and factory worker mappings below remain available.

The control-plane contracts are versioned. Adapter capabilities do not grant
authorization. Public JSON/NDJSON commands do not require callers to import
Python. Normalized supervision evidence retains metadata and digests, not raw
provider records; native content can enter the adapter transiently. This privacy
contract does not describe legacy factory worker logs, which can contain task
text and agent output.

## Native interfaces currently mapped

| Agent | Native command | Supervised interface | Current Veyro capabilities |
| --- | --- | --- | --- |
| Codex | `codex` | `exec --json` (default), App Server JSON-RPC (explicit opt-in) | start, stream, cancel, wait, result, steer on App Server |
| Claude Code | `claude` | `--print --output-format stream-json` | start, stream, cancel, wait, result |
| Prime Agent | `prime-agent` | `--print --mode json` | start, stream, cancel, wait, result |
| Pi | `pi` | `--print --mode json` | start, stream, cancel, wait, result |
| OpenCode | `opencode` | `run --format json` | start, stream, cancel, wait, result |

The registry probes installed versions at runtime. These mappings are based on each installed CLI's
own `--help` output, not scraped terminal rendering.
