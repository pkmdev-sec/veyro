# Prime Agent supervision canary

For current procedures and recovery, use the [operator guide](supervision-operator-guide.md).
For the dated cross-provider checks, see [verification results](supervision-verification.md).

`SUP-009` proves the complete control path with a disposable Prime Agent session. The canary creates no saved session, sends no prompt, invokes no model through Prime Agent, and writes no repository files.

## Proven path

1. Create a client-owned Prime Agent session with zero messages.
2. Attach the version-pinned daemon bridge and normalize `session_started`.
3. Reduce the normalized event into provider-neutral session state.
4. Classify the proposed stop as `review_required` using deterministic boundary policy.
5. Assess the risky-action checkpoint with loopback LocalJev and the configured `qwen3:14b` checkpoint identity. The canary does not attest server weights; check the [deployment prerequisite](supervision-verification.md#check-the-assessor-deployment).
6. Bind an explicit local approval to the exact control-request SHA-256.
7. Authorize and dispatch `stop_session` through the Prime Agent bridge.
8. Require the daemon's terminal `session_closed` event before reporting success.
9. Confirm that no canary session remains.

The stop applies only to the newly created disposable session. A failure triggers best-effort cleanup of that same session.

## Run

The operator must state who approved the action and pass the explicit stop flag:

```bash
.venv/bin/python -m veyro.supervision.prime_canary \
  --repo /path/to/repository \
  --approved-by local-operator \
  --approve-stop
```

The command rejects a missing `--approve-stop`. It accepts only the pinned Prime Agent daemon hello and the private current-user Unix socket enforced by the bridge.

## Verified result

The live `SUP-009` run produced:

```json
{
  "authorization": "authorized",
  "boundary": "review_required",
  "checkpoint": "risky_action",
  "control": "executed",
  "semantic_provider": "localjev-qwen3-14b",
  "trigger": "session_started",
  "verification": "session_failed",
  "verification_reason": "killed"
}
```

`session_failed` is Veyro's provider-neutral terminal mapping for a daemon session closed with reason `killed`; it does not mean the canary failed.
