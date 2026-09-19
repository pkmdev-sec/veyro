# Supervision capabilities and privacy

This reference describes the pinned adapters and evidence boundaries. For tasks,
use the [operator guide](supervision-operator-guide.md).

## Compatibility

| Provider | Required version | Additional pin |
| --- | --- | --- |
| Prime Agent | `0.9.5` | Daemon protocol `7`, schema revision `29`, ID `protocol-7-schema-29-a5c9d20f8b13` |
| OpenCode | `1.18.30` | OpenAPI `1.0.0`; authenticated numeric-loopback HTTP |
| Codex | `0.154.0` | Native hooks and optional queue seam, not App Server |

Version mismatches fail closed. Do not edit a pin merely to accept an upgrade.
Recheck the provider schema, adapter conformance, and native canary first.

The only authoritative semantic assessor is loopback `localjev-qwen3-14b` with
checkpoint `qwen3:14b@sha256:bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8`.
LocalJev is not needed for read-only attachment or observe-only proposals.
`supervise` uses `http://127.0.0.1:8080` and requests `jev-latest`; it does not use
`FOREMAN_JEV_*` to choose a different server. Foreman validates the configured
provider/checkpoint labels, but provenance copies that configured checkpoint.
It does not authenticate a server-reported weight digest for each response.
[Check the deployment independently](supervision-verification.md#check-the-assessor-deployment)
before enabling controls. Readiness and installed-tag checks are not per-response
attestation. Assessment failure does not select another model. Jeff and other shadow models
cannot authorize controls. Do not weaken TLS, native trust, or managed policy to
make a connection or canary succeed.

## Adapter capabilities

This table records adapter declarations, not authorization or a promise that every
capability is exposed by the executable. `internal` and `experimental` controls
need approval. Stable interruption is disruptive and also needs approval.

| Capability | Prime Agent | OpenCode | Codex default | Codex queue opt-in |
| --- | --- | --- | --- | --- |
| `observe_lifecycle` | supported / internal | supported / stable | supported / stable | supported / stable |
| `observe_messages` | supported / internal | supported / stable | supported / stable | supported / stable |
| `observe_tools` | supported / internal | supported / stable | supported / stable | supported / stable |
| `observe_approvals` | unsupported | supported / stable | supported / stable | supported / stable |
| `replay_events` | supported / internal | unsupported | unsupported | unsupported |
| `queue_follow_up` | supported / internal | supported / experimental | unsupported | supported / experimental |
| `steer_active_turn` | supported / internal | unsupported | unsupported | unsupported |
| `interrupt_turn` | supported / internal | supported / stable | unsupported | unsupported |
| `stop_session` | supported / internal | unsupported | unsupported | unsupported |
| `reply_to_approval` | unsupported | supported / experimental | unsupported | unsupported |
| `attach_existing` | supported / internal | supported / stable | unsupported | unsupported |

- Prime's replay seam does not make `foreman attach` history complete. The public
  command requests snapshot metadata, not transcript replay; its output cursor is
  local to that connection. Prime approval observation and replies are unsupported.
- OpenCode SSE is live-only. `abort` interrupts; it does not stop or delete a
  session. Foreman never substitutes destructive session deletion for stop.
  Approval replies require an observed ID and map to `once` or `reject`, not `always`.
- Codex observations are hook boundaries, usually `unknown`, not successful tool
  outcomes or completion evidence. Local journal reads work through `foreman
  attach` despite unsupported native replay/attachment. Queue opt-in is an adapter
  seam for an orchestrator; neither the listener CLI nor `supervise` enables it.
  Live queue delivery has not been verified.

## Privacy and retention

- These boundaries apply to the supervision adapters, not legacy `foreman run`
  worker logs, which can contain task text and agent output.
- Foreman normalizes structured provider data; it never scrapes a user's terminal.
  Native transports can deliver content transiently before the adapter drops it.
  Codex's hook helper sanitizes input before publishing it across its socket.
- Normalized evidence omits prompts, transcripts, file contents, tool arguments
  and output, control text, credentials, and environment values. IDs, repository
  paths, tool kinds/names, timestamps, and digests can still be sensitive metadata.
  Hashing is not anonymization or encryption.
- `sessions`, `attach`, and `supervise` write metadata to stdout, not a new event
  journal. Protect any redirected output. The Codex listener and generic broker
  can persist private journals under `.foreman/supervision/`. The older native
  launcher sidecar uses `.foreman/native-sessions/`; it is not this control plane.
- Operator proposal files intentionally contain control text. Delivery claims
  contain only digests and have no automatic expiration. Keep them through any
  period in which a command might be repeated. Do not put private operator files,
  journals, connection credentials, or ledgers into Git or public bug reports.
- Native providers can retain prompts and queued input under their own policies.
  Codex queue text is also visible in native process argv. Foreman cannot redact
  native storage or hide that argv by omitting its own logs.
- Owner-only permissions separate OS users, not processes running as the same
  user. An agent that can edit operator files or write approval stdin can cross
  this trust boundary. Protect those channels with external access controls.
- Deterministic classification uses the declared operation, not proof of arbitrary
  prose safety. Semantic checks use retained metadata and cannot prove unobserved
  content safe or a task complete. Gates cover Foreman-issued controls, not every
  native tool action; native permissions remain necessary.

To pause supervision, close the observer/controller. This does not stop native
work or revoke an already delivered control. Retain delivery claims. Handle native
stop, credential rotation, and hook removal through the provider's normal workflow.

