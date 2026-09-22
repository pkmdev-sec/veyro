# Supervision capabilities and privacy

This reference describes the pinned adapters and evidence boundaries. For tasks,
use the [operator guide](supervision-operator-guide.md).

## Compatibility

| Provider | Required version | Additional pin |
| --- | --- | --- |
| Prime Agent | `0.9.5` | Daemon protocol `7`, schema revision `29`, ID `protocol-7-schema-29-a5c9d20f8b13` |
| OpenCode | `1.18.30` | OpenAPI `1.0.0`; authenticated numeric-loopback HTTP |
| Codex | `0.154.0` | Native observation hooks, not App Server |

Version mismatches fail closed. Do not edit a pin merely to accept an upgrade.
Recheck the provider schema, adapter conformance, and native canary first.

Semantic assessment is fail-closed for executing control authorization. Configured
provider, checkpoint, role, and question-version labels do not prove that an
assessment is authoritative. Review-required actions in executing modes therefore
remain denied with `semantic_evidence_required`; supplying an assessment that has
only those labels is rejected as `semantic_evidence_invalid`. Deterministically
permitted actions do not invoke an assessment service, so their existing approval
and capability gates remain usable. Observe-only and advisory modes still never
deliver controls.

The loopback LocalJev deployment remains pinned to `localjev-qwen3-14b` with
checkpoint `qwen3:14b@sha256:bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8`
for non-authorizing assessment workflows. A configured label is not a
per-response weight attestation. Do not weaken TLS, native trust, or managed
policy to make a connection or canary succeed.

## Adapter capabilities

This table records adapter declarations, not authorization or a promise that every
capability is exposed by the executable. `internal` and `experimental` controls
need approval. Stable interruption is disruptive and also needs approval.

| Capability | Prime Agent | OpenCode | Codex |
| --- | --- | --- | --- |
| `observe_lifecycle` | supported / internal | supported / stable | supported / stable |
| `observe_messages` | supported / internal | supported / stable | supported / stable |
| `observe_tools` | supported / internal | supported / stable | supported / stable |
| `observe_approvals` | unsupported | supported / stable | supported / stable |
| `replay_events` | supported / internal | unsupported | unsupported |
| `queue_follow_up` | supported / internal | supported / experimental | unsupported |
| `steer_active_turn` | supported / internal | unsupported | unsupported |
| `interrupt_turn` | supported / internal | supported / stable | unsupported |
| `stop_session` | supported / internal | unsupported | unsupported |
| `reply_to_approval` | unsupported | supported / experimental | unsupported |
| `attach_existing` | supported / internal | supported / stable | unsupported |

- Prime's replay seam does not make `veyro attach` history complete. The public
  command requests snapshot metadata, not transcript replay; its output cursor is
  local to that connection. Prime approval observation and replies are unsupported.
- OpenCode SSE is live-only. `abort` interrupts; it does not stop or delete a
  session. Veyro never substitutes destructive session deletion for stop.
  Approval replies require an observed ID and map to `once` or `reject`, not `always`.
- Codex observations are hook boundaries, usually `unknown`, not successful tool
  outcomes or completion evidence. Local journal reads work through `veyro
  attach` despite unsupported native replay/attachment. Codex supervision is
  observation-only; follow-up delivery is unavailable.

## Control outcomes and effect evidence

`ControlResult.outcome = executed` means only that the provider acknowledged the
native control request. It does not prove that an asynchronous follow-up was
consumed, a steer changed the active turn, an interrupt took effect, or a queued
message ran. Those successful controls have effect
`acknowledged_unverified`. An exact, validated provider rejection has effect
`failed`, while a control denied before delivery has effect `not_applicable`.

Prime stop is the only implemented verified-effect mechanism, but the public
command cannot currently authorize its review-required proposal. If a direct
library integration supplies valid authorization and delivers the stop, its
effect starts as `acknowledged_unverified` and becomes `verified` only after a
later native `session_completed` or `session_failed` event for the same session.
A delivery or verification exception, cancellation, timeout, or closed event
stream leaves the effect `unknown`; Veyro does not retry it.

Every mode persists an owner-only ledger under `--ledger-dir`. Before returning
an authorization outcome, Veyro fsyncs an immutable sanitized decision receipt
keyed by delivery-scope and authorization digests. Repeating an identical
preflight reuses that receipt. For an authorized execution, Veyro then fsyncs an
exclusive immutable claim before calling the bridge. After the attempt it fsyncs
a sanitized receipt that binds claim, authorization, optional validated provider
result, effect, and timestamps by digest. Stop verification adds at most one
immutable final-effect receipt bound to the attempt. Records contain no control
text or provider detail. A claim without an attempt receipt is crash-ambiguous
and still blocks retry. Unsafe, replaced, linked, exposed, malformed, or
inconsistent ledger records fail closed when the ledger is reopened.


## Privacy and retention

- These boundaries apply to the supervision adapters, not internal legacy factory
  worker logs, which can contain task text and agent output.
- Veyro normalizes structured provider data; it never scrapes a user's terminal.
  Native transports can deliver content transiently before the adapter drops it.
  Codex's hook helper sanitizes input before publishing it across its socket.
- Normalized evidence omits prompts, transcripts, file contents, tool arguments
  and output, control text, credentials, and environment values. IDs, repository
  paths, tool kinds/names, timestamps, and digests can still be sensitive metadata.
  Hashing is not anonymization or encryption.
- `sessions`, `attach`, and `supervise` write metadata to stdout, not a new event
  journal. Protect any redirected output. The Codex listener and generic broker
  can persist private journals under `.veyro/supervision/`. The older native
  launcher sidecar uses `.veyro/native-sessions/`; it is not this control plane.
- Operator proposal files intentionally contain control text. Authorization
  decision, delivery claim, attempt, and final-effect receipts contain only
  sanitized metadata and digests and have no automatic expiration. Keep them
  through any period in which a command might be repeated. Do not put private
  operator files, journals, connection credentials, or ledgers into Git or
  public bug reports.
- Native providers can retain prompts and queued input under their own policies.
  Veyro cannot redact native storage by omitting its own logs.
- Owner-only permissions separate OS users, not processes running as the same
  user. An agent that can edit operator files or write approval stdin can cross
  this trust boundary. Protect those channels with external access controls.
- Deterministic classification uses the declared operation, not proof of arbitrary
  prose safety. Semantic checks use retained metadata and cannot prove unobserved
  content safe or a task complete. Gates cover Veyro-issued controls, not every
  native tool action; native permissions remain necessary.

To pause supervision, close the observer/controller. This does not stop native
work or revoke an already delivered control. Retain delivery claims and receipts.
Handle native stop, credential rotation, and hook removal through the provider's
normal workflow.

