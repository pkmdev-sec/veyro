# Operate the supervision control plane

Use `veyro sessions` to discover metadata and `veyro attach` to observe an
existing session. Use `veyro supervise` to evaluate one policy-gated control
proposal against current observations. The public command is not an unattended agent.

Activate this checkout's environment with `source .venv/bin/activate`, or replace
`veyro` below with `.venv/bin/veyro`.

Veyro leaves native interfaces, credentials, configuration, and session storage
with the provider. Pi and Claude Code support native launch, not this existing-session
control plane.

Check [version pins and authority](supervision-reference.md#compatibility) before
connecting. `veyro supervise` does not call LocalJev or any other assessor.
Read-only observation, observe-only evaluation, and advisory evaluation do not
require a model service.

## Select and observe a session

1. Keep the native session open through its normal interface. Veyro discovery
   and attachment never start or resume it.
2. Use the exact repository and endpoint. Copy a selector from discovery:

   ```sh
   veyro sessions --agent prime-agent --repo /path/to/repo \
     --socket /path/to/existing/daemon.sock --limit 100
   veyro sessions --agent opencode --repo /path/to/repo \
     --server http://127.0.0.1:4096 --limit 100
   veyro sessions --agent codex --repo /path/to/repo --limit 100
   ```

   Prime requires a private current-user-owned socket. A listed client-owned
   session can still be inaccessible to a different native client. Do not change
   its ownership to force attachment.

   For OpenCode, supply the existing server's `OPENCODE_SERVER_PASSWORD` through
   the process environment. `OPENCODE_SERVER_USERNAME` defaults to `opencode`.
   Never put credentials in a URL, command argument, or report. Veyro does not
   discover credentials for you. See [server setup](opencode-server-bridge.md).

   Codex discovers only existing Veyro journals. Set up the
   [observe-only hook listener](codex-hooks-bridge.md) through native hook review
   if you need new observations. A journal does not prove a native process is live.
3. Attach for a bounded observation window:

   ```sh
   veyro attach --agent prime-agent --repo /path/to/repo \
     --socket /path/to/existing/daemon.sock --session ACTIVE_ID \
     --watch-seconds 30 --max-events 100
   veyro attach --agent opencode --repo /path/to/repo \
     --server http://127.0.0.1:4096 --session SESSION_ID \
     --watch-seconds 30 --max-events 100
   veyro attach --agent codex --repo /path/to/repo \
     --session VEYRO_JOURNAL_ID --after-sequence 0 \
     --watch-seconds 30 --max-events 100
   ```

Read the `attachment` record before interpreting events. All providers report
`native_history_complete: false` and `controls_enabled: false`. Codex reports
`local_journal_only`; Prime and OpenCode report `live_native_stream`. Follow the
[attachment reference](existing-session-attachment.md) for cursor and page limits.
Ctrl-C detaches the observer; it does not stop the native session.

## Evaluate one proposal without delivery

1. Choose an operator-controlled directory outside the agent-editable workspace.
   Use private files (`0600`) and directories (`0700`). Keep one persistent ledger
   for the same delivery scope. Every ledger path component must be nonsymlinked;
   on macOS use `/private/var/...`, not `/var/...` or `/tmp/...`.
2. Save a proposal with a command ID that identifies this intended action:

   ```json
   {"command_id":"review-001","operation":"unknown","intent":{"action":"stop_session","reason":"Stop only after reviewing the native session."}}
   ```

   This text is an example, not a request to stop a session during verification.
   The `unknown` operation is review-required. Do not relabel an operation as
   low-risk to make authorization advance. Review the actual proposal and selected
   session before you run the command.
3. Evaluate with no policy override:

   ```sh
   veyro supervise --agent prime-agent --repo /path/to/repo \
     --socket /path/to/existing/daemon.sock --session ACTIVE_ID \
     --proposal /real/private/operator/proposal.json \
     --ledger-dir /real/private/operator/delivery
   ```

   Add `--timeout-seconds SECONDS` to bound the evaluation window. The timeout does
   not add an assessor or enable delivery.

   The default observe-only mode calls no assessor and sends no control. The final
   decision has reason `observe_only` when the earlier identity, capability, and
   state checks pass.

For advisory evaluation, use a separate policy file containing
`{"protocol_version":"1.0","mode":"advisory"}` and pass it with `--policy`.
Advisory mode also calls no assessor and sends no control. Its final decision has
reason `advisory_only` when the earlier checks pass. Every mode records its decision
in the same persistent ledger.

### Review-required proposals fail closed

The public command creates `SupervisionControlLoop` without an assessor. When a
review-required proposal reaches the semantic gate, both `approval_required` and
`automatic` mode produce `semantic_evidence_required`. This happens before human
approval. The command emits no `approval_required` record, does not read an approval
from stdin, and sends no native control for that proposal.

No policy setting supplies the missing semantic evidence or makes a review-required
proposal deliver through this command. Restoring LocalJev does not change this path
because the command does not connect it. Do not relabel the operation, forge
evidence, or change policy to bypass the denial.

`veyro supervise` supports Prime with `--socket` and OpenCode with `--server`.
Codex supervision is observation-only. `veyro supervise` rejects Codex and cannot
deliver a Codex control.

The [capability matrix](supervision-reference.md#adapter-capabilities) distinguishes
adapter support from executable access. An advertised capability never overrides
boundary classification or authorization policy.

## Recover without duplicate delivery

| Signal | Operator action |
| --- | --- |
| `observe_only`, `advisory_only` | Expected non-delivery. The public command calls no assessor in either mode and sends no control. |
| `capability_unsupported`, `capability_unknown` | Keep controls disabled. Use the native interface if needed; do not substitute another operation. |
| `boundary_forbidden` | Do not retry with approval or a different model. Remove the forbidden operation. |
| `human_approval_required`, `human_approval_invalid` | A review-required proposal cannot reach this stage through the public command. For a supported low-risk proposal, check the current request digest and UTC times. If this invocation is awaiting stdin, approve or deny there. A finished invocation cannot be revived with old evidence. |
| `human_denied` | Respect the veto. Do not use automatic mode or regenerate approval to bypass it. |
| `semantic_evidence_required`, `semantic_evidence_invalid` | The public command cannot supply or repair semantic evidence. Restoring LocalJev or changing policy cannot authorize a review-required action. Observe-only remains available; no fallback assessor is allowed. |
| `unsafe_state`, `stale_observation` | Observe the current native state. Do not reuse the snapshot or approval. Check whether a claim was retained before considering a distinct reviewed action. |
| `identity_mismatch`, `evidence_mismatch` | Stop. Recheck repository, session, proposal, and evidence binding; do not patch serialized evidence to make it fit. |
| `delivery_unavailable` | Preserve the ledger. Check its ownership and actual path, or whether the command was already claimed. A claim is not proof of execution. |
| `authorized` | Read delivery and verification; authorization is only one step. |
| Exit 2, Ctrl-C, disconnect, timeout, missing decision, or failed delivery | Treat a possible dispatch as uncertain. Inspect the native session and preserve all claims. Neither cancellation nor detachment undoes an in-flight action. |

For uncertain delivery, do not delete claims, move to another ledger, or invent a
new command ID to resend. Reconcile the native result with the operator first.
There is no force-retry or claim-reset command. Back up the persistent ledger with
its private permissions; restoring an older copy loses duplicate protection.

For an observation disconnect, rediscover the same existing native session and
attach read-only. Prime/OpenCode start a new local stream; do not pass an old local
sequence as a native cursor. Record the gap. For Codex, only `--after-sequence`
resumes the local journal. A replaced, truncated, insecure, or incompatible journal
must not be repaired by forging records or changing identities. Preserve it for
inspection. Do not change a native repository's permissions or credentials merely
to satisfy a Veyro check.

On Codex listener restart, review the new generated hook path with native trust.
Old hook commands refer to the old connection and can silently lose observations.
Remove only the hook definitions you added when retiring a listener. Never disable
managed policy or overwrite unrelated hooks.

## Protect operator data

Follow the [privacy and retention rules](supervision-reference.md#privacy-and-retention).
Protect stdout captures, operator files, and journals. Retain delivery claims.
Owner-only permissions do not isolate an agent running as the same OS user.

To pause supervision, close the observer/controller. This does not stop native
work or undo a control already sent. Handle native stop, credential rotation, and
hook removal through the provider's normal workflow.

## Verify this installation

Run the commands and review the explicit limits in
[version-pinned verification](supervision-verification.md). The
[recorded machine-readable results](supervision-verification.json) contain no
session IDs, prompts, credentials, or raw provider output.

For adapter details, see [Prime](prime-agent-daemon-bridge.md),
[OpenCode](opencode-server-bridge.md), and [Codex](codex-hooks-bridge.md).
