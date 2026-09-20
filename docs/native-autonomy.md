# Native autonomous work

## Launch a task

```sh
veyro agent prime-agent --repo . --autonomous \
  --prompt "Fix the failing tests" --check '.venv/bin/python -m pytest -q'

veyro agent opencode --repo . --autonomous \
  --prompt "Fix the failing tests" --check '.venv/bin/python -m pytest -q' \
  -- --model litellm/claude-haiku-4-5
```

Use commands that actually verify your task. Repeat `--check` for independent
checks. A model and valid native provider credentials must already be available.
The model above is an example, not a required provider or a bundled service.

Launch is the explicit opt-in. Veyro adds no human approval checkpoints. It writes
a plan, starts a separate build turn, checks the result, and requests repairs
until every check passes or a limit is reached. Native tool permissions still
apply. OpenCode uses its supported `--auto` flag, which preserves explicit denies.
Veyro disables the OpenCode build agent's question and plan-transition tools for
this launch. It does not remove permission extensions from Prime Agent.

Native autonomy currently supports macOS and Linux, not Windows. It uses POSIX
file locks, process groups, and shell commands.

The native terminal remains usable after completion. Automation stops at
completion; its old deadline does not kill a successfully completed session.
Closing the native terminal ends the sidecar. Prime's native `--print --mode json`
can also be passed after `--` for an unattended process that exits after work.
OpenCode `run` is not the interactive integration: it can exit on the first idle
event before an asynchronous plugin continuation. Use the native TUI command above.

### Limits and evidence

| Option | Default | Meaning |
| --- | --- | --- |
| `--max-continuations` | `6` | Maximum follow-ups, including plan-to-build |
| `--check-timeout` | `120` | Maximum seconds per verification command |
| `--timeout` | `1800` | Wall-clock task budget in seconds |

At least one nonempty check and a nonempty prompt are required. Exhaustion,
cancellation, native provider errors, and uncertain delivery do not count as
completion. There is no automatic reconnect/replay of an interrupted task.
Launch a new task after investigating the recorded result. The final permitted
build turn can still complete if its checks pass.

Each launch writes a private `.veyro/autonomy/<run-id>/` directory:

- `plan.md`: the agent's implementation and verification plan.
- `config.json`: repository, check commands, deadlines, and limits.
- `state.json`: phase, bound native session, processed turns, and last decision.
- `events.jsonl`: ordered decisions, check exit codes, bounded output, and latency.
- `adapter-error.json`: an adapter, native cancellation, or transport failure, when present.
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
    -> native agent writes plan, then ends its turn
    -> standalone JSON checkpoint checker
         planning -> building -> verifying -> completed
                         ^          |
                         +-- repair-+  (failed checks; bounded)
    -> native follow-up API, without a human approval step
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
| Continue | `sendUserMessage`, `followUp` | SDK `session.promptAsync` |
| Cancel | Native abort signal/context | Session error/deletion and SDK abort |

The version-1 checker interface is:

```text
python autonomy_check.py CONFIG SESSION TURN [claim]
-> one JSON object: action = continue | complete | blocked | ignore
```

A `continue` decision supplies a repair/build message. A `complete` decision
means all configured checks passed, not that arbitrary requirements are proven.
A `blocked` decision stops automation. `ignore` covers foreign sessions,
processed turns, and terminal runs. One native session owns each launch.

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

# These make real model calls and open disposable native PTYs.
.venv/bin/python tools/benchmark_native_autonomy.py --live prime-agent \
  --model litellm/claude-haiku-4-5 --output /tmp/veyro-prime.json
.venv/bin/python tools/benchmark_native_autonomy.py --live opencode \
  --model litellm/claude-haiku-4-5 --output /tmp/veyro-opencode.json
```

The local benchmark measures a cold checkpoint process, a real verifier process,
and state/journal IO. Its p95 must be below one second. Native UI startup, model
inference, and arbitrary test suites are separate costs; they are not promised
to finish in one second.

Accuracy, precision, recall, false completions, and raw latency samples are logged
for balanced synthetic pass/fail fixtures. Live canaries use the real native TUI,
require plan-to-build and verified completion, check that the supplied verifier
was not changed, and independently score eight cases, five of which are absent
from that verifier. They send no task input after launch. The terminal is closed
only after a terminal outcome or timeout. A tiny fixture is not evidence of
accuracy across general software tasks.

[Recorded benchmark results](native-autonomy-benchmark.json) contain the measured
latency and accuracy results, including the provider-error attempt.
The final baseline measured 91 ms median and 129 ms p95 over 30 samples, with
30/30 correct synthetic decisions. Both final native canaries passed 8/8 cases;
they took about 70 seconds (Prime) and 60 seconds (OpenCode), including model time.
The broader repository suite retains its pre-existing
`test_noisy_events_are_coalesced` timing failure. Adapter unit tests also exercise repair delivery,
duplicates, cancellation, busy-event races, and session switching. They are
protocol tests, not substitutes for the real native canaries.

### Typed completion evaluation

Use `--evaluator rubric.json` to add [typed completion criteria](native-judge.md).
For the local Qwen pair, also pass `--coding-profile small --evaluation-profile 14b`.
It runs only after executable checks pass. Rubrics select explicit evidence files
and positive criteria. Errors block completion; low or uncertain scores request
bounded repairs. Model latency and labelled accuracy are measured separately from
the model-free baseline. Existing-session localjev supervision is unchanged.
