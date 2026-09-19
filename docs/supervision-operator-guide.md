# Operate the supervision control plane

Use `foreman sessions` to discover metadata and `foreman attach` to observe an
existing session. Use `foreman supervise` only when you want to evaluate one
control proposal. It defaults to observe-only; it is not an unattended agent.

Activate this checkout's environment with `source .venv/bin/activate`, or replace
`foreman` below with `.venv/bin/foreman`.

Foreman leaves native interfaces, credentials, configuration, and session storage
with the provider. Pi and Claude Code are outside this supervision roadmap; their
native launcher support is unchanged.

Check [version pins and authority](supervision-reference.md#compatibility) before
connecting. LocalJev is not needed for read-only observation.

## Select and observe a session

1. Keep the native session open through its normal interface. Foreman discovery
   and attachment never start or resume it.
2. Use the exact repository and endpoint. Copy a selector from discovery:

   ```sh
   foreman sessions --agent prime-agent --repo /path/to/repo \
     --socket /path/to/existing/daemon.sock --limit 100
   foreman sessions --agent opencode --repo /path/to/repo \
     --server http://127.0.0.1:4096 --limit 100
   foreman sessions --agent codex --repo /path/to/repo --limit 100
   ```

   Prime requires a private current-user-owned socket. A listed client-owned
   session can still be inaccessible to a different native client. Do not change
   its ownership to force attachment.

   For OpenCode, supply the existing server's `OPENCODE_SERVER_PASSWORD` through
   the process environment. `OPENCODE_SERVER_USERNAME` defaults to `opencode`.
   Never put credentials in a URL, command argument, or report. Foreman does not
   discover credentials for you. See [server setup](opencode-server-bridge.md).

   Codex discovers only existing Foreman journals. Set up the
   [observe-only hook listener](codex-hooks-bridge.md) through native hook review
   if you need new observations. A journal does not prove a native process is live.
3. Attach for a bounded observation window:

   ```sh
   foreman attach --agent prime-agent --repo /path/to/repo \
     --socket /path/to/existing/daemon.sock --session ACTIVE_ID \
     --watch-seconds 30 --max-events 100
   foreman attach --agent opencode --repo /path/to/repo \
     --server http://127.0.0.1:4096 --session SESSION_ID \
     --watch-seconds 30 --max-events 100
   foreman attach --agent codex --repo /path/to/repo \
     --session FOREMAN_JOURNAL_ID --after-sequence 0 \
     --watch-seconds 30 --max-events 100
   ```

Read the `attachment` record before interpreting events. All providers report
`native_history_complete: false` and `controls_enabled: false`. Codex reports
`local_journal_only`; Prime and OpenCode report `live_native_stream`. Follow the
[attachment reference](existing-session-attachment.md) for cursor and page limits.
Ctrl-C detaches the observer; it does not stop the native session.

## Evaluate a control before enabling delivery

1. Choose an operator-controlled directory outside the agent-editable workspace.
   Use private files (`0600`) and directories (`0700`). Keep one persistent ledger
   for the same delivery scope. Every ledger path component must be nonsymlinked;
   on macOS use `/private/var/...`, not `/var/...` or `/tmp/...`.
2. Save a proposal with a command ID that identifies this intended action:

   ```json
   {"command_id":"review-001","operation":"unknown","intent":{"action":"queue_follow_up","message":"Review verification results and report what remains."}}
   ```

   This text is an example, not a request to submit model input during verification.
   Natural-language instructions are not proven safe by their operation label.
   The operator must review the actual text and selected session.
3. Evaluate with no policy override. This sends no control and calls no assessor:

   ```sh
   foreman supervise --agent prime-agent --repo /path/to/repo \
     --socket /path/to/existing/daemon.sock --session ACTIVE_ID \
     --proposal /real/private/operator/proposal.json
   ```

For advisory assessment without delivery, use a separate policy file containing
`{"protocol_version":"1.0","mode":"advisory"}` and pass it with `--policy`.
Neither observe-only nor advisory mode needs or creates a ledger.

## Approve one exact control

1. Save `{"protocol_version":"1.0","mode":"approval_required"}` as a private
   policy file. Re-read the proposal and verify the native target before approval.
2. Run with that policy and the persistent ledger:

   ```sh
   foreman supervise --agent prime-agent --repo /path/to/repo \
     --socket /path/to/existing/daemon.sock --session ACTIVE_ID \
     --proposal /real/private/operator/proposal.json \
     --policy /real/private/operator/policy.json \
     --ledger-dir /real/private/operator/delivery --timeout-seconds 180
   ```

   For OpenCode use `--agent opencode --server http://127.0.0.1:PORT` instead of
   `--socket`. The public command does not provide Codex queue dispatch.
3. Read the emitted `supervision` and, if present, `approval_required` records.
   Check the identity, action, command ID, policy digest, and request digest.
   In the same process, send one JSON line on stdin using the emitted digest and
   current UTC times:

   ```json
   {"approval_id":"operator-001","request_sha256":"<emitted SHA-256>","decision":"approve","approved_by":"<operator label>","issued_at":"<current UTC timestamp>","expires_at":"<later UTC timestamp>"}
   ```

   Use `"decision":"deny"` to veto. The label is not cryptographic authentication.
   Stdin is a trusted human channel; never connect model output to it. A previous
   invocation's approval is not reusable on a newly attached request.
4. Read the final `decision`. `authorization.outcome: authorized` alone is not
   execution evidence. Inspect `delivery.outcome` and `verification` separately.
   A queue receipt is not model execution. A stop receipt without a later terminal
   event is not a verified stop. Exit zero means evaluation completed, not delivery.

Approval can expire or observations can change during assessment, human review,
or ledger persistence. Foreman rechecks before dispatch and does not silently
refresh evidence. A retained claim can exist even when no native control was sent.
These checks are not an atomic compare-and-execute transaction at the provider.

**No pinned adapter currently qualifies for automatic delivery.** Even in
`automatic` mode, current native controls need approval. Only denial of an observed
pending approval can be allowlisted, and only with a supported stable capability
and usable low-risk evidence. See [rollout policy](supervision-rollout.md).

The [capability matrix](supervision-reference.md#adapter-capabilities) distinguishes
adapter support from executable access. An advertised capability never overrides
policy or human approval.

## Recover without duplicate delivery

| Signal | Operator action |
| --- | --- |
| `observe_only`, `advisory_only` | Expected non-delivery. Change the separate policy only if you intend to enable approved controls. |
| `capability_unsupported`, `capability_unknown` | Keep controls disabled. Use the native interface if needed; do not substitute another operation. |
| `boundary_forbidden` | Do not retry with approval or a different model. Remove the forbidden operation. |
| `human_approval_required`, `human_approval_invalid` | Check the current request digest and UTC times. If this invocation is awaiting stdin, approve or deny there. A finished invocation cannot be revived with old evidence. |
| `human_denied` | Respect the veto. Do not use automatic mode or regenerate approval to bypass it. |
| `semantic_evidence_required`, `semantic_evidence_invalid` | Restore the pinned LocalJev service and provenance. Observe-only remains available; no fallback assessor is allowed. |
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
to satisfy a Foreman check.

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
