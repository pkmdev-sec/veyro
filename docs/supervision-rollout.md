# Roll out supervised controls

For current procedures and recovery, use the [operator guide](supervision-operator-guide.md).
For the dated cross-provider checks, see [verification results](supervision-verification.md).

`veyro supervise` evaluates **one** proposal against an existing Prime Agent or
OpenCode session. It does not start or resume a native session. The default is
observe-only. `veyro attach` remains strictly read-only.

Codex hook journals remain available through `veyro attach`; Codex supervision is
observation-only. Pi and Claude Code support native launch, not existing-session
supervision.

## Choose an operator policy

| Mode | Assessor in public command | Delivery |
| --- | --- | --- |
| `observe_only` (default) | None | Never |
| `advisory` | None | Never |
| `approval_required` | None. Review-required proposals return `semantic_evidence_required` before approval. | Only deterministically permitted proposals can reach exact-request approval and the remaining delivery gates. |
| `automatic` | None. Review-required proposals return `semantic_evidence_required` before approval. | Only explicitly allowlisted low-risk controls can continue automatically; no pinned adapter currently qualifies. |

The public command constructs `SupervisionControlLoop` without an assessor. No
policy mode connects LocalJev, supplies semantic evidence, or makes a
review-required proposal reach approval. LocalJev is limited to standalone
assessment examples, direct library integrations, and the internal legacy factory
runtime.

A policy is a versioned private JSON file, selected separately from the proposal:

```json
{"protocol_version":"1.0","mode":"approval_required"}
```

No command automatically promotes the mode. Keep operator policy files outside
agent-editable repositories. Policy and proposal files must be current-user-owned,
private regular files, with no symlink leaf or hard links, and at most 64 KiB.
Use mode `0600`. Neither a policy nor a proposal can supply semantic evidence.
Proposals also cannot set the mode, reduced state, or capability declarations.

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
no automatic controls. `review_operations` can raise low risk to review-required,
which makes the public command fail with `semantic_evidence_required` before
approval. It cannot lower risk or make forbidden operations permissible.

**No currently pinned adapter qualifies for automatic delivery.** Prime controls
are internal. OpenCode approval replies and follow-ups are experimental. Its
stable interruption is disruptive and review-required, so the public command
fails before approval. Codex supervision is observation-only. Tests use a
deliberately stable test adapter to exercise the automatic branch; there is no
claim of live automatic control validation or capability promotion.

Free-text follow-ups and steering, interruption, stop, and approval grants are
review-required. The public command therefore rejects them with
`semantic_evidence_required` before requesting human approval. A queued follow-up
can immediately start an idle native turn; queue acceptance does not prove model
execution. Native capabilities remain unchanged and unsupported controls remain
unsupported.

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

Discover the selector with `veyro sessions`, then run:

```sh
veyro supervise --agent prime-agent --repo /path/to/repo \
  --socket /path/to/existing/daemon.sock --session ACTIVE_ID \
  --proposal /private/path/proposal.json \
  --ledger-dir /real/private/path/delivery
```

This default invocation reports a decision and sends no control. To evaluate the
same proposal under approval-required policy, run:

```sh
veyro supervise --agent prime-agent --repo /path/to/repo \
  --socket /path/to/existing/daemon.sock --session ACTIVE_ID \
  --proposal /private/path/proposal.json --policy /private/path/policy.json \
  --ledger-dir /real/private/path/delivery --timeout-seconds 180
```

For the review-required example above, this command returns
`semantic_evidence_required`. It does not call an assessor, emit an
`approval_required` record, read approval from stdin, or deliver the control.

For OpenCode, use `--agent opencode --server http://127.0.0.1:PORT` instead of
`--socket`. Credentials come only from `OPENCODE_SERVER_USERNAME` and
`OPENCODE_SERVER_PASSWORD`. Loopback credentials do not use environment proxies.

Every mode requires a persistent private ledger directory. Every path component must be
nonsymlinked. On macOS, use the real path, such as `/private/var/...`, not the `/var` or
`/tmp/...` aliases. Veyro creates missing ledger directories with owner-only permissions and
persists every decision before returning it.

## Approve the exact request when reached

The executable emits newline-delimited JSON:

1. `supervision`: identity, mode, policy digest, and exact request digest.
2. `approval_required`: authorization metadata with `request_sha256`, only when a
   deterministically permitted proposal reaches an applicable approval gate.
3. `decision`: authorization, delivery outcome if attempted, and terminal stop
   verification if available.

A review-required proposal never reaches step 2 through the public command. It
fails at the semantic gate before approval.

Only when the command emits `approval_required`, review the proposal and selected
native session, then send one JSON line on stdin. Use the emitted digest and
actual current UTC timestamps:

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

- Deterministic rules run before the semantic gate. Credential exposure and
  security-control bypass remain forbidden; neither approval nor a model score
  overrides them.
- The public executable has no assessor and never contacts LocalJev. In both
  executing modes, a review-required proposal returns
  `semantic_evidence_required` before approval. A configured model label, policy
  setting, or proposal field is not semantic evidence.
- The loopback `localjev-qwen3-14b` deployment and configured
  `qwen3:14b@sha256:bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8`
  checkpoint remain available only to explicitly named non-public assessment
  paths. The configured identity is not per-response weight attestation.
- Boundaries bind to the complete request digest. Changed requests cannot reuse
  boundary evidence.
- Veyro checks the live normalized event cursor after any applicable approval and
  revalidates approval time and the cursor after durable persistence. Changed
  observations reject the proposal; the CLI does not silently rebase approval or
  retry. This check is not a provider-side atomic transaction.
- Terminal/failed observation state blocks delivery. Active-turn controls require
  an observed active turn. Approval replies require an observed pending ID; there
  is no backfill of approvals that predate the connection.
- Native history is still partial. A successful control receipt is not proof of
  task completion. An executed stop requires a later terminal provider event.
- Before native delivery, Veyro exclusively creates and fsyncs a no-retry claim.
  Its key uses repository, provider, native session, and command ID, so a fresh
  Veyro attachment cannot resend that command. Concurrent, duplicate, restarted,
  failed, and uncertain deliveries all retain the fence.
- Claims store only digests. They prove an attempted delivery was reserved, not
  that it executed. Keep the same ledger across invocations. Do not delete claims,
  change ledger directories, or mint a new command ID merely to retry uncertainty.
  Inspect the native session first.
- Stdout omits control text, transcripts, model input/output, provider error text,
  and credentials. It includes session IDs and repository paths. Protect redirected
  output. The operator-created proposal file intentionally contains control text;
  Veyro does not copy it into its ledger.

These gates cover controls issued by Veyro. They do not intercept all native
tool execution or replace the native agent's own permission system. This command
is a bounded one-proposal controller, not an unattended proposal-generating daemon.

## Verify

Run the focused tests through the project environment:

```sh
.venv/bin/python -m pytest tests/test_rollout.py tests/test_delivery.py \
  tests/test_control_authorization.py tests/test_supervision_control_loop.py \
  tests/test_supervision_checkpoints.py tests/test_bridge_contract.py -q
```

The existing attachment canaries also run the real `veyro supervise` executable
in default observe-only mode and check that the original native session remains.
Those public-command checks call no assessor and deliver no control.

### Historical approved-stop canaries

The `veyro.supervision.prime_canary` and `veyro.supervision.rollout_canary`
modules remain as historical records of an intended approved-stop path. They are
not current verification procedures. `SupervisionControlLoop.run_control()` does
not consume the assessment service injected by `prime_canary`, and the public CLI
used by `rollout_canary` constructs no assessor. Their review-required stop
therefore returns `semantic_evidence_required` before approval or delivery.

Do not run either module as qualification evidence. The checked-in reports are
dated records from an earlier implementation, not proof that current HEAD can
assess, authorize, or deliver an approved stop.

### Recorded verification

[Installation results](supervision-verification.md#recorded-installation-results)
record native delivery scope, cleanup evidence, and known test failures. They do
not qualify any pinned adapter for automatic control.
