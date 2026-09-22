# Experimental pinned-local agent task

Veyro discovers five native harnesses, but the experimental `veyro agent` command is not a
general interactive launcher. It currently runs only an autonomous OpenCode task with an explicit
pinned local coding profile. Use `sessions` and `attach` for supported existing-session observation.

## Caller interface

```text
# Discover installed agents and the machine interface Veyro will use.
veyro agents
veyro agents --json

# Experimental: run OpenCode with a pinned local model.
veyro agent opencode --repo . --autonomous --coding-profile coder30 \
  --prompt "Fix the failing test" --check '.venv/bin/python -m pytest -q'

# Discover or observe previously recorded native sessions.
veyro sessions --agent opencode --repo .
```

The deployed `veyro agent` path requires `--autonomous` and a pinned local coding profile.
It rejects unpinned interactive launches and native model overrides. G4 remains open. The removed
top-level `run` command has no compatibility shim. Independently installed native CLIs remain
separate programs.

`veyro agents --json` is the language-neutral discovery interface. Shell scripts, Node programs,
Go programs, and other clients can consume it without importing Veyro's Python package.

The human table and JSON document report executable availability separately from pinned autonomy
version qualification. An available provider with an unqualified version is not eligible for the
experimental autonomous task path.

## Boundary

```text
operator or automation
        |
        +-- native executable ----------------------> native interactive harness
        |
        +-- veyro agent --------------------------> experimental pinned-local task
                |                                      (OpenCode autonomous mode)
                +-- Veyro evidence ---------------> lifecycle + workspace evidence
        |
        +-- veyro sessions / attach --------------> supported bounded observation
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

The internal legacy factory runtime defaults to Codex `exec`; App Server remains an explicit
experimental opt-in through `VEYRO_CODEX_BACKEND=app-server`. Persisted records can still carry
legacy `codex_thread_id` and `codex_turn_id` fields alongside neutral `agent_id`, `session_id`, and
`turn_id` fields. Removing those persisted fields requires a separate data migration.

For the version-pinned Codex hook listener, see [Codex hooks bridge](codex-hooks-bridge.md).

## Native autonomous tasks

`veyro agent opencode --autonomous --coding-profile PROFILE --prompt JOB --check COMMAND`
loads launch-scoped native hooks and a pinned loopback model. The shared checker drives
build, verification, and bounded repair without Veyro approval prompts. This experimental task
path is the only behavior of `veyro agent`; it is not an observation-only interactive launcher.
See [native autonomy](native-autonomy.md) for the architecture and live benchmarks.

## Existing-session supervision

The experimental pinned-local task, internal legacy factory runtime, and supported existing-session
control plane are separate interfaces. Starting a native task does not automatically connect its
provider bridge or enable controls.

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

Pi and Claude Code have no existing-session supervision bridge. Their native launch definitions
and internal worker mappings below remain available to library code.

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
