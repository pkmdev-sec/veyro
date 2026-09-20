# OpenCode server bridge

For current procedures and recovery, use the [operator guide](supervision-operator-guide.md).
For the dated cross-provider checks, see [verification results](supervision-verification.md).

Veyro supports OpenCode `1.18.30` through its authenticated HTTP server. OpenCode still owns the native TUI, provider credentials, configuration, prompts, and session database. Veyro connects beside it through SSE and documented control endpoints.

Direct `opencode` use is unchanged. Use this bridge only when a session needs Veyro observation, evidence, policy, or approved controls.

## Start an authenticated loopback server

Generate a new high-entropy password for each server process. Put it in the child environment, not in argv, a URL, a log, or a repository file.

```bash
export OPENCODE_SERVER_USERNAME=veyro
export OPENCODE_SERVER_PASSWORD="$(openssl rand -base64 32)"
opencode serve --pure --hostname 127.0.0.1 --port 0
```

OpenCode first tries port `4096` for `--port 0`, then selects an available ephemeral port. Use the exact loopback URL printed by the process. Keep mDNS disabled and do not add CORS origins.

Veyro rejects:

- non-loopback addresses;
- HTTPS or non-HTTP schemes;
- credentials embedded in URLs;
- missing credentials;
- server or session versions other than `1.18.30`;
- sessions from another repository.

## Attach the native TUI

Pass authentication through the environment. A separate terminal does not inherit
the server terminal's exports; supply the same username and password through your
approved secret-handling workflow in each client process. Do not use `--password`,
because command arguments can be visible to other local processes.

```bash
opencode attach http://127.0.0.1:<port> \
  --dir /absolute/path/to/repository \
  --session ses_...
```

The native attached TUI and Veyro can use the same server and session. The TUI keeps full control of rendering and keyboard input. Veyro does not scrape terminal output.

## Capability contract

| Capability | State | Stability | OpenCode surface |
|---|---|---|---|
| Lifecycle observation | Supported | Stable | Authenticated `/event` SSE and session reads |
| Message observation | Supported | Stable | `message.updated`; content is discarded |
| Tool observation | Supported | Stable | `message.part.updated`; inputs and outputs are discarded |
| Approval observation | Supported | Stable | `permission.asked` and `permission.replied` |
| Queue follow-up | Supported | Experimental | `POST /session/:id/prompt_async` |
| Interrupt active turn | Supported | Stable | `POST /session/:id/abort` |
| Reply to approval | Supported | Experimental | `POST /permission/:id/reply` |
| Attach existing session | Supported | Stable | Session validation plus native `opencode attach` |
| Replay events | Unsupported | — | Legacy SSE has no cursor; the bridge does not mix in the v2 event schema |
| Steer active turn | Unsupported | — | An async prompt queues another user message; it does not steer the active model call |
| Stop session | Unsupported | — | Abort preserves a session; delete permanently erases it |

Approval replies are request-bound. Veyro replies only to an approval ID observed on the attached session. `APPROVE` maps to `once`; it never silently grants `always` permission.

## Evidence and privacy

The bridge stores provider-neutral metadata only:

- session and event IDs;
- event type and order;
- message role and message ID;
- tool name, tool call ID, and success state;
- approval ID and resolved decision;
- SHA-256 of the native event.

It does not put prompt text, message text, model names, tool arguments, tool output, permission resources, file contents, credentials, or environment values in normalized events. Native OpenCode storage remains subject to the operator's OpenCode configuration.

SSE is live-only. A disconnect can create an observation gap. The bridge reports a normalization failure and does not claim complete replay.

## Run the live canary

```bash
.venv/bin/python -m veyro.supervision.opencode_canary
```

The canary defaults to `~/.opencode/bin/opencode` and requires exactly `1.18.30`.
Use `--executable` with your verified native binary if it is installed elsewhere. It:

1. starts a temporary `--pure` server on loopback with random in-memory Basic credentials;
2. proves an unauthenticated health request returns `401`;
3. verifies authenticated health and the required OpenAPI routes;
4. observes the initial `server.connected` SSE event;
5. stops the process group and verifies the port closes.

The canary creates no session, selects no model, sends no prompt, invokes no tool, replies to no approval, and calls no abort or delete endpoint.
