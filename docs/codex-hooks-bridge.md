# Codex hooks bridge

For current procedures and recovery, use the [operator guide](supervision-operator-guide.md).
For the dated cross-provider checks, see [verification results](supervision-verification.md).

This bridge targets **Codex 0.154.0**. Pi and Claude Code have no existing-session
supervision bridge.
The bridge uses stable native command hooks for observation only.
It does not start or connect to App Server. Direct Codex usage remains unchanged.

## Start an observe-only listener

From this repository, use its installed Python environment:

```sh
.venv/bin/python -m veyro.bridges.codex_hooks serve \
  --repository /absolute/path/to/project \
  --codex-home "$HOME/.codex" \
  --executable /opt/homebrew/bin/codex
```

`/opt/homebrew/bin/codex` is a macOS Homebrew example. Use the verified native
launcher path for your installation, not a credential/proxy wrapper.
The listener checks `codex-cli 0.154.0`. It prints a JSON hook configuration with
an absolute Python executable in isolated mode (`-I`) and a private connection-file path. No token is
printed. Keep the listener alive while using Codex in that exact repository.

Review the generated command. Merge the desired event definitions into Codex's
`hooks.json` using Codex's native configuration and trust workflow (`/hooks` or
startup review). Do not overwrite existing hooks or duplicate TOML and JSON
handlers. Veyro does not modify native configuration, trust hashes, credentials,
plugins, transcripts, or native session storage. Do not bypass hook trust or
managed policy. Listener restart creates a new connection path, so review and
approve the updated hook command before using it. Remove those definitions when
retiring the listener; a missing listener intentionally produces no model feedback.

The command runs synchronously with a two-second native hook timeout. Publication
has a 0.5-second socket timeout; the listener bounds client reads to one second.
The helper always emits no stdout/stderr and returns zero on an observation
failure. It neither blocks a tool nor injects context. This is best-effort
observation, **not a security enforcement mechanism**. Missing observations cannot
be interpreted as permission to continue.

## Evidence and identity

Input is bounded to 4 MiB and reduced before crossing the socket. Only these
fields leave the hook process:

- UUID session ID, hook name, and repository path (for identity checks).
- SHA-256 of the original event and of turn, tool-use, and subagent IDs.
- Coarse tool kind: shell, file, or other.

Prompts, assistant text, tool arguments/results, model names, permission settings,
transcript paths, environment values, credentials, and arbitrary tool names are
not retained. The listener uses a current-user-owned private directory, 0600
connection file/socket, and an authentication token. It accepts metadata, never
control requests. Input frames are bounded to 16 KiB, active clients and sessions
to 64. A per-session OS lock prevents simultaneous journal writers.

The mapping of repository + CODEX_HOME + native thread UUID to Veyro session ID
is deterministic. Each thread has a private broker journal under
`.veyro/supervision/`. Resuming the same thread continues that local journal.
Receipt sequence is contiguous local observation order, **not provider execution
order**. Broker replay does not imply native replay. Multiple sessions remain
separate; subagent scope is retained rather than counted as root activity.

## Honest capability limits

| Capability | Status |
| --- | --- |
| Lifecycle, prompt, tool, permission boundaries | Supported, metadata only |
| Native event replay / attach-existing discovery | Unsupported in this bridge |
| Follow-up queue | Unsupported; hook supervision is observation-only |
| Active-turn steering / remote interrupt / session stop | Unsupported |
| Approval reply through the sidecar | Unsupported |
| Synchronous hook policy decisions | Not enabled |

Most hook boundaries normalize as `unknown` with `boundary_only=true` and exact
native event provenance. A root `UserPromptSubmit` becomes `user_prompt_submitted`.
This deliberately avoids inventing outcomes: another hook may deny a tool, a
`Stop` hook may cause continuation, and `SessionEnd` may precede later resume.
`PostToolUse` alone does not give this adapter a verified success boolean.
Therefore reduced state does not claim completed tools, turns, approval decisions,
or task completion. These events can support advisory inspection, not automatic
completion or unrestricted intervention.


## Experimental App Server is separate

The internal legacy factory worker defaults to `exec`. Use
`VEYRO_CODEX_BACKEND=app-server` only to opt into its experimental controls in library code.
The hook bridge never silently switches to RPC if a hook or control is unavailable. See
[steering.md](steering.md) for that separate internal worker path.

## Verification

```sh
.venv/bin/python -m pytest tests/test_codex_hooks.py tests/test_bridge_contract.py -q
.venv/bin/python -m veyro.supervision.codex_canary
```

Focused tests exercise a real helper process, authenticated socket publication,
private broker persistence, metadata filtering, session isolation, restart,
conformance, capability rejection, approval gating, and durable queue deduplication.
Queue tests use fake native acknowledgments and do not submit real model input.
A no-prompt live canary must prove actual `SessionEnd` delivery through native hook
approval. A blocked canary is not completion evidence. Live status is recorded
below.

## Version-pinned sources

Codex source commit: `6b9826e3aa83b1a5947db50f4332cb9c65f1b340`
(tag `rust-v0.154.0`).

- [Hook schemas](https://github.com/openai/codex/tree/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/hooks/schema/generated)
- [Hook discovery and trust](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/hooks/src/engine/discovery.rs)
- [Queue CLI](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/tui/src/session_queue_commands.rs)

### Live verification

The pinned Codex 0.154.0 binary delivered `SessionEnd` with a valid UUID and
`reason=other`, then exited with status zero. Native approval was used for the
private directory and generated hook. No model prompt or queue submission was sent; the OS sandbox denied network access. Owned process groups
and the disposable tree were removed.

The canary honors installed managed requirements without writing them. It denies
real-home access and uses system Python for its standalone metadata-only hook.
The PTY driver waits after trust-screen rendering because Codex deliberately drains
buffered input at those boundaries. It submits `/quit` separately from Enter so
native paste-burst handling does not turn the command into buffered text. This
terminal automation is canary-only, never an observation or control bridge.

This proves native hook loading, native approval, stdin delivery, UUID parsing,
and no-prompt cleanup. The separate real helper-to-broker test proves metadata
publication. Queue acknowledgment tests are synthetic; live queue execution,
tool events, and policy enforcement were not exercised by this no-prompt canary.
