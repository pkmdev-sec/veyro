# Roll out supervised controls

For current procedures and recovery, use the [operator guide](supervision-operator-guide.md).
For the dated cross-provider checks, see [verification results](supervision-verification.md).

`foreman supervise` evaluates **one** proposal against an existing Prime Agent or
OpenCode session. It does not start or resume a native session. The default is
observe-only. `foreman attach` remains strictly read-only.

Codex hook journals remain available through `foreman attach`; this command does
not expose a Codex queue channel. Pi and Claude Code are outside the supervision
roadmap. Their existing native launch commands are unchanged.

## Choose an operator policy

| Mode | Assessment | Delivery |
| --- | --- | --- |
| `observe_only` (default) | None | Never |
| `advisory` | Pinned LocalJev at meaningful checkpoints | Never, even with approval |
| `approval_required` | Pinned LocalJev when policy requires review | Every control needs current exact-request approval |
| `automatic` | Same assessment rules | Only explicitly allowlisted low-risk controls; all other controls still need approval |

A policy is a versioned private JSON file, selected separately from the proposal:

```json
{"protocol_version":"1.0","mode":"approval_required"}
```

No command automatically promotes the mode. Keep operator policy files outside
agent-editable repositories. Policy and proposal files must be current-user-owned,
private regular files, with no symlink leaf or hard links, and at most 64 KiB.
Use mode `0600`. Proposals cannot set the mode or supply assessments, reduced state,
or capability declarations.

The initial automatic allowlist contains only `deny_approval`:

```json
{
  "protocol_version": "1.0",
  "mode": "automatic",
  "automatic_actions": ["deny_approval"],
  "review_operations": ["write_repository", "run_local_check"]
}
```

That action declines an approval already observed on the connection. It does not
execute the requested operation. It also requires a supported **stable** capability,
low risk, usable observation state, and no human veto. An empty allowlist permits
no automatic controls. `review_operations` can raise low risk to review-required;
it cannot lower risk or make forbidden operations permissible.

**No currently pinned adapter qualifies for automatic delivery.** Prime controls
are internal. OpenCode approval replies and follow-ups are experimental; its
stable interruption is disruptive and still requires approval. Codex's queue seam
is experimental and is not exposed here. Tests use a deliberately stable test
adapter to exercise the automatic branch; there is no claim of live automatic
control validation or capability promotion.

Free-text follow-ups and steering, interruption, stop, and approval grants always
require human approval. A queued follow-up can immediately start an idle native
turn; queue acceptance does not prove model execution. Native capabilities remain
unchanged and unsupported controls remain unsupported.

## Submit one proposal

Create a private proposal file. For example:

```json
{
  "command_id": "review-001",
  "intent": {
    "action": "queue_follow_up",
    "message": "Review the current verification results and report what remains."
  },
  "operation": "unknown"
}
```

`operation` defaults to `unknown`, which requires review. A declared operation is
not proof that natural-language instructions are harmless. Free text is never
automatic, even if labeled as a repository read. A native denial uses
`reply_to_approval`, `decision: "deny"`, an observed `approval_id`, and
`operation: "decline_approval"`.

Discover the selector with `foreman sessions`, then run:

```sh
foreman supervise --agent prime-agent --repo /path/to/repo \
  --socket /path/to/existing/daemon.sock --session ACTIVE_ID \
  --proposal /private/path/proposal.json
```

This default invocation reports a decision and sends no control. For explicit
approval-required operation:

```sh
foreman supervise --agent prime-agent --repo /path/to/repo \
  --socket /path/to/existing/daemon.sock --session ACTIVE_ID \
  --proposal /private/path/proposal.json --policy /private/path/policy.json \
  --ledger-dir /real/private/path/delivery --timeout-seconds 180
```

For OpenCode, use `--agent opencode --server http://127.0.0.1:PORT` instead of
`--socket`. Credentials come only from `OPENCODE_SERVER_USERNAME` and
`OPENCODE_SERVER_PASSWORD`. Loopback credentials do not use environment proxies.

Executing modes require a persistent private ledger directory. Every path
component must be nonsymlinked. On macOS, use the real path, such as
`/private/var/...`, not the `/var` or `/tmp` aliases. Foreman creates missing ledger
directories with owner-only permissions. Observe/advisory modes create no ledger.

## Approve the exact request

The executable emits newline-delimited JSON:

1. `supervision`: identity, mode, policy digest, and exact request digest.
2. `approval_required`, when needed: authorization metadata with `request_sha256`.
3. `decision`: authorization, delivery outcome if attempted, and terminal stop
   verification if available.

After reviewing the proposal and the selected native session, send one JSON line
on stdin. Use the emitted digest and actual current UTC timestamps:

```json
{"approval_id":"operator-001","request_sha256":"<emitted SHA-256>","decision":"approve","approved_by":"<operator identity>","issued_at":"<current UTC timestamp>","expires_at":"<later UTC timestamp>"}
```

Use `decision: "deny"` to veto. Future-issued, expired, or mismatched evidence
cannot authorize delivery. Stdin is the trusted operator channel, not a model
output channel; `approved_by` records a label, not a cryptographic signature.
Never wire an untrusted agent's output to this approval input.

The total evaluation/approval wait is bounded by `--timeout-seconds` (1–300,
default 180), followed by observer cleanup. A cancelled filesystem claim may
still finish in its worker thread; shutdown can wait for that IO, but cancellation
does not proceed to native delivery. Startup reduction is limited to 1000
metadata events. A normal exit means evaluation completed, **not** that a control
was delivered; inspect `decision.authorization` and `decision.delivery`. Errors
exit 2; Ctrl-C exits 130 and detaches the observer. Neither means a native action
already in flight was undone.

## Safety and evidence

- Deterministic rules run before semantic assessment. Credential exposure and
  security-control bypass remain forbidden; neither approval nor a model score
  overrides them.
- Only loopback `localjev-qwen3-14b`, pinned to
  `qwen3:14b@sha256:bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8`,
  supplies semantic assessments. Loopback SDK requests use a direct transport,
  never ambient proxies. External endpoints keep their proxy and TLS/CA defaults.
  There is no fallback or shadow voting. This checkpoint is a configured identity,
  not per-response weight attestation; verify the deployment as described in the
  [assessor prerequisite](supervision-verification.md#check-the-assessor-deployment).
- Boundaries bind to the complete request digest. Changed requests cannot reuse
  boundary evidence. Checkpoint caches distinguish changed state, risky-action
  evidence, and task context.
- Foreman checks the live normalized event cursor after assessment and after
  approval, and revalidates approval time and the cursor after durable persistence.
  Changed observations reject the proposal; the CLI does not silently
  rebase approval or retry. This check is not a provider-side atomic transaction.
- Terminal/failed observation state blocks delivery. Active-turn controls require
  an observed active turn. Approval replies require an observed pending ID; there
  is no backfill of approvals that predate the connection.
- Native history is still partial. A successful control receipt is not proof of
  task completion. An executed stop requires a later terminal provider event.
- Before native delivery, Foreman exclusively creates and fsyncs a no-retry claim.
  Its key uses repository, provider, native session, and command ID, so a fresh
  Foreman attachment cannot resend that command. Concurrent, duplicate, restarted,
  failed, and uncertain deliveries all retain the fence.
- Claims store only digests. They prove an attempted delivery was reserved, not
  that it executed. Keep the same ledger across invocations. Do not delete claims,
  change ledger directories, or mint a new command ID merely to retry uncertainty.
  Inspect the native session first.
- Stdout omits control text, transcripts, model input/output, provider error text,
  and credentials. It includes session IDs and repository paths. Protect redirected
  output. The operator-created proposal file intentionally contains control text;
  Foreman does not copy it into its ledger.

These gates cover controls issued by Foreman. They do not intercept all native
tool execution or replace the native agent's own permission system. This command
is a bounded one-proposal controller, not an unattended proposal-generating daemon.

## Verify

Run the focused tests through the project environment:

```sh
.venv/bin/python -m pytest tests/test_rollout.py tests/test_delivery.py \
  tests/test_control_authorization.py tests/test_supervision_control_loop.py \
  tests/test_supervision_checkpoints.py tests/test_bridge_contract.py -q
```

The existing attachment canaries also run the real `foreman supervise` executable
in default observe-only mode and check that the original native session remains.
The disposable Prime control canary explicitly selects approval-required mode,
uses a private delivery ledger, requests pinned LocalJev assessment, and verifies
an approved stop without sending a model prompt:

```sh
.venv/bin/python -m foreman.supervision.prime_canary --repo /path/to/repo \
  --approved-by OPERATOR --approve-stop
```

Only this owned no-prompt canary can make one fresh assessment after startup
metadata invalidates the first snapshot. It does not retry native delivery. The
operator command instead reports the stale decision and stops.


For an end-to-end test of the **actual executable and approval stdin protocol**:

```sh
.venv/bin/python -m foreman.supervision.rollout_canary --repo /path/to/repo \
  --approved-by OPERATOR --approve-stop
```

This creates a disposable resident `noSession` fixture, because native client-owned
sessions cannot be attached by a separate CLI client. It approves only the exact
fixture stop digest, verifies the terminal event, confirms removal from the daemon
roster, and checks that exactly one durable claim was written. Its private files
and ledger are removed after the subprocess and native fixture are closed. It
sends no model prompt and does not modify an existing user session.

### Recorded SUP-015 verification

- Final full suite: **488 passed, 1 failed** in the existing
  `test_noisy_events_are_coalesced` timing race (100 events instead of 50).
  That failure was reproduced on the unchanged baseline during SUP-014. A prior
  full run passed all 485 tests before the four transport tests were added.
  No scheduler code or timing thresholds were changed.
- Focused policy, delivery, bridge conformance, attachment, and control-loop checks:
  **256 passed**. Four additional SDK transport regressions passed: loopback
  proxy routing is disabled, external proxy routing is retained, and TLS
  certificate verification remains enabled. Ruff and `git diff --check` passed.
- Prime Agent `0.9.5`: actual executable approval/stdin/claim/terminal-stop canary
  passed twice. The direct control-loop canary also passed with pinned LocalJev.
- Prime Agent `0.9.5` and OpenCode `1.18.30`: actual executable default-observe
  canaries sent no prompts or controls and preserved the selected native sessions.
- No disposable canary tree remained after cleanup. No installed credentials,
  native configuration, or global rollout mode were changed.
