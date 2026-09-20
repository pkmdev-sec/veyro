# Verify the supervision control plane

Run from the Veyro repository with its installed `.venv`. These checks exercise
real binaries without submitting work prompts to native agents. Check the
[version pins](supervision-reference.md#compatibility) first. A blocked or mismatched
canary is a failed check, not permission to bypass native security.

## Check contracts and documentation

```sh
.venv/bin/python -m pytest tests/test_supervision_operator_docs.py -q
.venv/bin/python -m pytest tests/test_bridge_contract.py tests/test_rollout.py \
  tests/test_delivery.py tests/test_control_authorization.py \
  tests/test_supervision_control_loop.py tests/test_supervision_checkpoints.py \
  tests/test_attachment.py tests/test_readonly_journal.py \
  tests/test_prime_agent_bridge.py tests/test_opencode_bridge.py \
  tests/test_codex_hooks.py tests/test_codex_canary.py \
  tests/test_loopback_assessment_transport.py -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
uv pip check --python .venv/bin/python
git diff --check
```

The documentation tests compare the matrix to all live adapter declarations,
including both Codex queue settings. They also check source pins, recovery-reason
coverage, local links, and the executable help for documented flags. They make no
native connection. Runtime tests exercise rejection, replay limits, exact approval,
claim persistence, reconnect deduplication, cancellation, and metadata filtering.
They do not substitute for native delivery proof.

## Check the assessor deployment

Before enabling reviewed controls, verify that the local deployment is the one you
intend to trust. Veyro requests `jev-latest` at fixed `http://127.0.0.1:8080`.
Its checkpoint provenance is a configured label, not a digest attested by the
server for each inference. A passing stop canary does not remove this limitation.

1. Query readiness without routing loopback traffic through ambient proxies:

   ```sh
   curl --noproxy '*' --fail --silent --show-error --max-time 5 http://127.0.0.1:8080/ready
   ```

   Require `status: ready` and `upstream_model: qwen3:14b`.
2. Read only the intended tag from the local Ollama inventory:

   ```sh
   curl --noproxy '*' --fail --silent --show-error --max-time 5 http://127.0.0.1:11434/api/tags | .venv/bin/python -c 'import json, sys; print(json.dumps([{ "name": m["name"], "digest": m["digest"] } for m in json.load(sys.stdin)["models"] if m["name"] == "qwen3:14b"]))'
   ```

   Require one record with digest
   `bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8`.
   An empty or different record is not a successful pin check.
3. Confirm the deployment owner and effective LocalJev upstream configuration
   through your normal trusted administration workflow. Do not publish environment
   dumps or credentials. Do not enable controls if you cannot establish that trust.

These GET requests send no model prompt. They verify reported readiness and the
installed tag, not the weights used by a particular response. The recorded deployment probe
found matching readiness and tag digest, but `/api/ps` listed no loaded models at
probe time. It therefore does not claim a live loaded-weight or per-response
attestation. Loading a model just to make this check look stronger is not required.

## Verify Prime read-only observation

Discover a session you own with `veyro sessions`. Use its selector and the
existing daemon socket:

```sh
.venv/bin/python -m veyro.supervision.attachment_canary --agent prime-agent \
  --repo /path/to/repo --socket /path/to/existing/daemon.sock --session ACTIVE_ID
```

This drives the actual discovery, attachment, and default-observe supervision
commands. It sends no control and confirms the selected native session still
exists. It does not prove complete history or access to another client's session.

## Verify one approved Prime control

Read this scope before running the command: it creates and stops **one disposable
resident `noSession` fixture**, not a selected user session. The pinned LocalJev
service must be available. `--approve-stop` is explicit approval for this fixture
only; the canary supplies the exact request-bound evidence through CLI stdin.

```sh
.venv/bin/python -m veyro.supervision.rollout_canary --repo /path/to/repo \
  --approved-by OPERATOR --approve-stop
```

Require `status: passed`, `control: executed`, one delivery claim, a terminal
`verification`, and `native_fixture_removed: true`. The canary uses the actual
`veyro supervise` subprocess. It sends no native-agent work prompt, removes its private
operator files/ledger, and verifies native roster removal. The resident fixture
allows a separate CLI client to attach without bypassing client-owned access rules.
This proves approved stop, not active-turn steering, interruption, or automatic
native delivery. A stale-observation rejection is a valid safety result but not a
successful canary; inspect fixture cleanup before a new independent canary run.

## Verify OpenCode observation

```sh
.venv/bin/python -m veyro.supervision.opencode_canary --attachment
```

This starts an isolated authenticated `--pure` loopback server, verifies rejection
of unauthenticated health requests, checks API routes and SSE, and creates one
empty fixture session. It drives discovery, attachment, and observe-only
supervision, then confirms that the session remains. Finally it closes the owned
server port and removes HOME/XDG storage. It sends no prompt, approval reply,
interrupt, or deletion request. It does not verify live control delivery.

## Verify Codex hook delivery

The current isolated canary requires macOS `sandbox-exec`, system Python, and the
pinned Apple-Silicon native binary. `--executable` can select another installation
path, but its SHA-256 must still match the pinned binary; it is not a cross-platform
compatibility override. It uses its own PTY only to drive native trust and exit:

```sh
.venv/bin/python -m veyro.supervision.codex_canary --timeout 30
```

Require native-approved `SessionEnd`, a valid UUID, exit zero, denied network, and
removed temporary storage. The real home is inaccessible to the fixture; installed
managed requirements remain readable and are never changed. Do not weaken those
requirements or use a credential/proxy wrapper to get a pass.

This proves native hook loading and delivery to a standalone metadata helper.
The separate `test_codex_hooks.py` helper-process tests prove publication to the
real authenticated broker. Neither test path proves live queue execution, native
tool outcomes, task completion, or synchronous policy enforcement.

## Recorded installation results

See [machine-readable results](supervision-verification.json) for the tested
runtime revision, UTC time, and normalized native reports. The September 19, 2026 record is
pre-rename evidence. Its command spellings were translated to current equivalents;
they are not the exact commands executed at that historical revision. For the new
package, see [recorded 0.4 release checks](rename-verification.json).
These are observations of the recorded installation, not guarantees after an
upgrade. No provider was promoted to automatic-control eligibility.

The full suite passed **497 tests**. The focused control-plane/documentation suite
passed **346 tests**, including eight documentation checks. Ruff, dependency
compatibility, and `git diff --check` passed. The installed pre-rename
entry point also accepted the documented `supervise --help` command.

All four native checks passed without native-agent prompts. Prime observation preserved
the selected existing session. Approved Prime stop used one claim and verified
`session_failed` for the disposable stopped worker; that terminal event is the
expected stop evidence, not a failed assessment. OpenCode preserved its fixture
through attachment and removed its isolated server afterward. Codex preserved
native trust and managed policy while delivering `SessionEnd`.

Historical runs reproduced a timing race in
`tests/test_integration.py::test_noisy_events_are_coalesced` on baseline `43f33c2`.
Later integration runs also expired their short worker budgets during real Git and
disk I/O. These are lifecycle tests, not latency benchmarks.

The fixtures now synchronize simulated phases, allow slow observations within test-only
budgets, and keep each output stream inside its configured debounce window. Completion
must still force the second assessment. Assertions still require all 50 output events,
exactly two assessments, and the expected completion, retry, and verification states.
Slow-observation and delayed-output cases exercise those contracts explicitly.
Production timeouts and debounce defaults are unchanged. No tests are skipped or marked
`xfail`; these checks do not establish a production latency target.

Checkpoint supervision requires strict probability validation. Missing, nonnumeric,
nonfinite, or out-of-range scores reject the assessment. They are not clamped into valid
probabilities or cached as usable evidence. The checkpoint tests cover rejection and a
subsequent valid response for the same checkpoint.
