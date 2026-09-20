# Prime Agent daemon bridge

For current procedures and recovery, use the [operator guide](supervision-operator-guide.md).
For the dated cross-provider checks, see [verification results](supervision-verification.md).

Veyro attaches beside the native Prime Agent TUI. Prime Agent continues to own the terminal, credentials, configuration, and session files. Veyro does not scrape terminal output.

## Pinned compatibility

The bridge accepts only this verified installation:

- Prime Agent: `0.9.5`
- Protocol: `prime-agent.daemon` version `7`
- Schema revision: `29`
- Schema ID: `protocol-7-schema-29-a5c9d20f8b13`

A different hello is rejected before attach. This is deliberate because the installed type declarations describe the local daemon API as an internal transport rather than a stable remote API.

## Capabilities

The bridge supports lifecycle, message-metadata, and tool-metadata observation; event replay; attach; follow-up; steering; turn interruption; and session stop. Prime Agent does not expose a provider-neutral approval reply through this schema, so that capability is declared unsupported.

All supported capabilities are declared `internal`. The control authorization gate therefore requires human approval for control dispatch. A daemon capability can never override a deterministic policy prohibition.

## Evidence and privacy

Normalized events contain event kind, tool name, error state, replay state, native event ID, and a SHA-256 digest of the native record. They do not retain prompts, assistant text, tool arguments, command text, tool output, session snapshots, or credentials.

The client accepts only a Unix socket owned by the current user with no group or other permissions. JSONL records are bounded to 4 MiB. Attach uses chunked and slim snapshots.

## Live canary

The SUP-008 canary connected to the installed default daemon, verified the exact hello above, listed sessions, attached read-only to an idle Prime Agent session, observed one normalized `session_started` event, and detached. It sent no prompt and executed no provider control.

Run focused checks with:

```bash
.venv/bin/pytest -q tests/test_prime_agent_bridge.py tests/test_bridge_contract.py
.venv/bin/ruff check src/veyro/bridges tests/test_prime_agent_bridge.py
```
