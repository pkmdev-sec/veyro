# Native autonomous work

## Launch a task

```sh
veyro agent opencode --repo . --autonomous \
  --coding-profile coder30 \
  --prompt "Fix the failing tests" \
  --check '.venv/bin/python -m pytest -q'
```

Use commands that actually verify your task. Repeat `--check` for independent checks.
A pinned coding profile is mandatory. Native `--model` overrides, remote endpoints, and unpinned
interactive launches are rejected. The separate legacy factory command is unavailable. The selected
`coder30` profile remains experimental because its unforced held-out canary scored 7/8; G4 is open.

Launch is the explicit opt-in. Veyro adds no human approval checkpoints inside the repair loop.
The agent inspects and implements in its first turn. Veyro then runs worker-visible checks and
requests bounded repairs until the checks pass or a limit is reached. Passing checks stop the run
for operator review. They never authorize completion. Native tool permissions still apply.
OpenCode runs headlessly with its supported `run --auto` interface, which preserves explicit
denies. Veyro disables the OpenCode build agent's question and plan-transition tools.

The strict deployment target is macOS. Veyro wraps local coding processes in
`sandbox-exec`, denies outbound networking, and permits only loopback traffic. OpenCode
also receives `OPENCODE_DISABLE_MODELS_FETCH=true` and
`OPENCODE_DISABLE_AUTOUPDATE=true`. Prime Agent is excluded because its bounded local
model turn stalled without an edit or checkpoint. Codex local mode is excluded because a live probe
observed an external HTTPS connection. OpenCode is therefore the only eligible coding
driver. Qwen3 Coder 30B exposes structured tools through Ollama's native `/api/chat`
but not its OpenAI-compatible endpoint. Veyro therefore starts a launch-scoped loopback
adapter that translates only the pinned model's chat requests and tool results. The adapter
has no remote route and exits with the task.

Automation stops for operator review when its checks and optional evaluator pass. OpenCode
autonomy uses its documented JSON `run` mode. After a failed checkpoint, Veyro starts another
bounded `run --session <id>` process. Repair stays in the same OpenCode session without an
in-process idle-event follow-up.

### Limits and evidence

| Option | Default | Meaning |
| --- | --- | --- |
| `--max-continuations` | `6` | Maximum repair follow-ups |
| `--check-timeout` | `120` | Maximum seconds per verification command |
| `--model-turn-timeout` | `120` | Local model header, chunk, and full-turn timeout |
| `--timeout` | `1800` | Wall-clock task budget in seconds |

For local OpenCode profiles, Veyro replaces the general OpenCode system prompt with a
small coding prompt, disables unrelated skill/task/web tools, limits each native turn
to eight agent steps, caps tool output, and keeps the first task plus the newest
messages within a 24 KiB serialized-message budget. It advertises a 16K context to
OpenCode and enforces a 1K output limit. The forced text-only eighth turn is limited
to 128 tokens; a new repair message resets the per-message counter. An over-budget
newest message fails closed before Ollama is called. A failed local run unloads the model to clear damaged
runner state.

Exhaustion, cancellation, native provider errors, and uncertain delivery block the run. There is
no automatic reconnect or replay of an interrupted task. Launch a new task after you inspect the
recorded result. A passing final build turn still stops for operator review.

Each launch writes a private `.veyro/autonomy/<run-id>/` directory:

- `config.json`: repository, check commands, deadlines, and limits.
- `state.json`: phase, bound native session, processed turns, and last decision.
- `events.jsonl`: ordered decisions, check exit codes, bounded output, and latency.
- `adapter-error.json`: the first adapter, native cancellation, or transport failure, when present.
- `context-events.jsonl`: local-context reductions, when any were required.
- `model-recovery.json`: the Ollama unload result after an unsuccessful local run.
- `adapter.mjs`: the launch-scoped native entry point.

Add `.veyro/` to your repository's ignore rules if needed. Veyro does not change
those rules for you. These logs differ from the metadata-only existing-session
supervisor: plans and check output can contain source text or secrets. The run
directory is owner-only, but native agents keep their own session logs. Delete
run directories according to your retention policy. Do not publish raw terminal
logs from the canary.

Checks are trusted shell commands executed with your account's permissions.
They are not sandboxed by native tool permissions. Use a disposable checkout or
an OS/container sandbox for untrusted repositories. Keep acceptance tests
outside the agent's write scope when tamper resistance matters. Veyro and the
agent run as the same OS user; the journal is not an adversarial security boundary.

## Agent-agnostic architecture

```text
veyro agent --autonomous
    -> launch-scoped native extension/plugin
    -> native agent inspects and implements in one turn
    -> standalone JSON checkpoint checker
         building -> verifying -> blocked for operator review
             ^          |
             +-- repair-+  (failed checks; bounded)
    -> native same-session follow-up, without a human approval step
```

`autonomy.py` prepares the launch. `autonomy_check.py` owns the shared state machine,
verification subprocesses, budgets, deduplication, and evidence. It uses only the
Python standard library and is invoked directly, avoiding the main CLI's imports
on each checkpoint. Neither the state machine nor the verifier imports a native
agent SDK. There is no network call on the default checkpoint path.

The small adapters in `src/veyro/integrations/` translate native events:

| Native API | Prime Agent 0.9.5 | OpenCode 1.18.30 / 1.18.31 |
| --- | --- | --- |
| Load | Explicit `--extension` file | Launch-only `OPENCODE_CONFIG_CONTENT` plugin |
| Bind session | `before_agent_start` | `chat.message` |
| Check result | `agent_end` | Completed assistant + `session.idle` |
| Continue | `sendUserMessage`, `followUp` | New bounded `run --session <id>` process |
| Cancel | Native abort signal/context | Session error/deletion and SDK abort |

The version-1 checker interface is:

```text
python autonomy_check.py CONFIG SESSION TURN [claim]
-> one JSON object: action = continue | blocked | ignore
```

A `continue` decision supplies a repair message. Passing checks produce a `blocked` decision with
the reason `operator_review_required`. `ignore` covers foreign sessions, processed turns, and
terminal runs. One native session owns each launch.

Processed turn IDs prevent delayed duplicate events from causing more work.
An OS file lock serializes checkpoint updates within a run. Different launches
use different directories. The checker records `verifying` before executing
checks. If it crashes there, a later event blocks rather than replaying uncertain
side effects. Delivery has no automatic retry after an uncertain native receipt.

Both adapters stop on native errors and observe cancellation before delivering a
follow-up. Verification kills its process group on timeout, cancellation, and
normal return. The native launcher forwards signals and escalates a task deadline
to SIGKILL after a two-second grace period; it reports timeout as exit code `124`.
Native plugin timers also abort their session at the deadline. These controls do
not govern unrelated processes or other independently launched sessions.

To add a provider, implement load, session binding, end-of-turn, continuation, and
cancellation mappings against its documented native API. Reuse the JSON checker.
Add adapter contract tests and a live disposable-repository canary. Unsupported
providers fail explicitly; terminal scraping is not a fallback.

This path does not change `veyro supervise`, its approval policy, or the separate
factory runtime. It does not install global hooks, edit shell startup files, or
activate in independently opened sessions. One launch supervises one task.

## Verification and performance

```sh
.venv/bin/python -m pytest -q tests/test_native_autonomy.py
.venv/bin/python tools/benchmark_native_autonomy.py \
  --output /tmp/veyro-baseline.json

# This makes only loopback model calls and opens a disposable native PTY.
.venv/bin/python tools/benchmark_native_autonomy.py --live opencode \
  --local-profile small --output /tmp/veyro-opencode-local.json
```

The local benchmark measures a cold checkpoint process, a real verifier process,
and state/journal IO. Its p95 must be below one second. Native UI startup, model
inference, and arbitrary test suites are separate costs; they are not promised
to finish in one second.

Accuracy, precision, recall, false completions, and raw latency samples are logged
for balanced synthetic pass/fail fixtures. Live canaries use the real native agent,
require build-and-repair verification, check that the supplied verifier
was not changed, and independently score eight cases, five of which are absent
from that verifier. They send no task input after launch. The terminal is closed
only after a terminal outcome or timeout. A tiny fixture is not evidence of
accuracy across general software tasks.

[Recorded benchmark results](native-autonomy-benchmark.json) contain the measured
latency and accuracy results, including the provider-error attempt.
The final baseline measured 91 ms median and 129 ms p95 over 30 samples, with
30/30 correct synthetic decisions. Earlier hosted-model canaries passed 8/8 cases, but they are historical comparison
evidence and do not qualify this local-only deployment. Qwen3 Coder 30B passed three direct 8/8 seeds. The OpenCode unforced canary completed
with 7/8 held-out assertions; its deterministic one-repair canary completed with 8/8,
one same-session continuation, fresh checks, and an unchanged verifier. Initial-pass
reliability remains open. The broader repository suite must still pass before release. Adapter unit tests also exercise repair delivery,
duplicates, cancellation, busy-event races, and session switching. They are
protocol tests, not substitutes for the real native canaries.

### Typed advisory evaluation

Use `--evaluator examples/native-judge.json` to try the checked-in [typed criteria](native-judge.md), or supply your own rubric. For the local dual-model
path, also pass `--coding-profile coder30 --evaluation-profile laya`. Evaluation runs only after
the executable checks pass. Low or uncertain scores request bounded repairs. Passing scores stop
for operator review; they cannot authorize completion. Existing-session supervision remains a
separate control path.
