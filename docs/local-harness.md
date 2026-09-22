# Run the local Qwen harness

Veyro can run a persistent numeric-readout service over the installed Qwen GGUF files.
It does not need the separate localjev checkout for this path.

**This is experimental.** Qwen2.5-7B failed important semantic regression and native coding cases.
The small profile selects a Qwen3-4B **Instruct** build for lower memory use, not as a quality upgrade.
The small profile must not use `qwen3:4b`. That tag is "Qwen3 4B Thinking 2507": its chat
template always starts the answer with `<think>`, so scoring a label at the first answer
position is invalid. The engine now reads the model's own chat template and refuses a
thinking-only generation prompt instead of returning meaningless scores.
A fitted temperature is not proof of correctness. Executable checks provide repair feedback, not
completion authority. Keep the existing Jev provider configuration if you do not want to opt into
the new readout.

## Install and inspect

On the tested Apple Silicon machine, install the optional runtime in the project environment:

```sh
env CMAKE_ARGS="-DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON -DGGML_ACCELERATE=ON" \
  uv pip install -e '.[local]'
veyro local models
```

A C/C++ build toolchain is required when a compatible wheel is unavailable.
The runtime is pinned to `llama-cpp-python==0.3.35`.

The bundled profiles are:

| Profile | Ollama model | Quantization | Role |
|---|---|---|---|
| `small` | `qwen3:4b-instruct-2507-q4_K_M` | Q4_K_M | Rejected coding candidate; compatible readout |
| `coder14` | `qwen2.5-coder:14b` | Q4_K_M | Rejected coding candidate; tool protocol mismatch |
| `coder30` | `qwen3-coder:30b` | Q4_K_M | Selected OpenCode coding and repair model |
| `14b` | `qwen3:14b` | Q4_K_M | Compatible typed readout |

Ollama must already be available on `127.0.0.1:11434` for coding and inventory commands.
Install missing models through Ollama. Then check `veyro local models`: the manifest
digest and metadata must match `config/model-profiles.json`. A changed tag is rejected,
not silently treated as the pinned model. Veyro does not auto-download weights.

The readout locates existing GGUF blobs through verified manifest bytes. It checks the
blob size and GGUF header. It does **not** rehash several gigabytes of weight contents on
each startup. The recorded blob digest is the manifest's expected identity.

## Start and stop an evaluator

```sh
veyro local start small
veyro local status small
veyro local stop small
```

Only `small` and `14b` are compatible typed-readout profiles. Their default readout
ports are `8081` and `8082`. `coder30` is used through Ollama, not the readout service. Both bind only to `127.0.0.1`. Existing services on `8080` and
`11434` are not restarted or reconfigured.

`ready` means that the model loaded and a real warmup forward pass completed.
`loading` is not ready. Startup waits briefly and can return `loading`; check status
before sending evaluations. New prompt shapes and uncached evidence can still be slower.

Runtime metadata and logs live in `~/.local/state/veyro/readout`, with a private directory
and private files. Use `--state-dir PATH` to isolate an instance. Inference and shutdown
require its bearer token. Public status does not expose the token. Stop uses the
authenticated endpoint, never an unverified PID from a file.

The service admits two evaluations and at most eight HTTP connections. Overflow is
rejected instead of building an unbounded queue. It accepts 1–8 questions, 2–128 outcomes
per question, requests up to 1 MiB, and timeouts up to 120 seconds. Shared evidence plus
all question suffixes must fit the configured context; nothing is silently truncated.

Loading both readout profiles uses substantial memory. Local coding through Ollama loads
another copy of the selected model. Do not assume both coding models and both readout
workers can remain resident on every machine.

## Run the dual-model task harness

Warm the 30B Ollama coding model. Laya loads its pinned typed-decisions checkpoint
on demand. Then launch a native autonomous session with both roles explicit:

```sh
veyro local warm coder30
veyro agent opencode --repo . --autonomous \
  --coding-profile coder30 \
  --evaluation-profile laya \
  --prompt 'Implement the requested change' \
  --check 'python verify.py' \
  --evaluator examples/native-judge.json \
  --allow-uncalibrated-evaluator
```

Both profile flags are required for the local pair. The coding profile binds OpenCode to Qwen3 Coder 30B through
Ollama. The evaluation profile snapshots the pinned Laya typed-decisions identity into
the evaluator config.
The configuration is launch-scoped. Veyro does not edit global provider settings or
remove native permission denials.

The control order is fixed:

1. Qwen3 Coder 30B plans and edits through the native agent.
2. Every worker-visible check runs.
3. Failed checks return to the 30B model. The evaluator is not called.
4. When checks pass, Laya scores the declared typed criteria against the listed evidence.
5. Failed or uncertain criteria return their scores for bounded repair.
6. Passing checks and scores stop the run for operator review.

The writer, checks, evaluator, and state run under one OS user. None can authorize completion.
Use their output as repair evidence only.

Local evaluation requires a matching calibration artifact for every criterion. To run a raw-score
experiment instead, add `--allow-uncalibrated-evaluator`. This does not make the scores calibrated,
reliable, or authoritative.

Local coding uses temperature zero and requests no Qwen thinking through Ollama's
`reasoning_effort: "none"` plus request-scoped `/no_think` hints. The pinned 4B template
unconditionally opens `<think>`, while the 14B template respects the API switch. The
soft hint did not make the earlier 4B Prime qualification trial complete. No installed
template or global model is edited. Automatic OpenCode titles are disabled for local
sessions to avoid a second generation competing with coding on the same model queue.
Default-thinking trials timed out and were also affected by leftover Prime daemon work;
they do not isolate the effect of thinking. Ollama rejects the `"low"` effort value for
Qwen3.

`veyro local warm coder30` optionally warms the Ollama coding model. Unlike the readout
worker's readiness, `veyro local models` reports Ollama residency. The warm command
refuses a new load when another model is resident, to avoid silently evicting it. Other
clients can still change Ollama residency after that check.

An evaluator rubric declares typed criteria and evidence. The local profile is supplied
by the launch flags, so the file can stay independent of the machine profile:

```json
{
  "rubric_version": "my-task-v1",
  "criteria": {"implemented": "The requested behavior is implemented."},
  "evidence_files": ["app.py"],
  "provider": {
    "kind": "local",
    "calibrations": {"implemented": "calibration/implemented.json"}
  }
}
```

Calibration paths resolve relative to the rubric file. To use an isolated service, set
`provider.state_dir`. Legacy Jev provider JSON remains supported. Local inference does
not require `TYPESAFE_API_KEY`.

## Preview and run an evaluator

An evaluator maps `input`, `output`, and optional `reference` JSON fields into literal
`{{variable}}` placeholders. It defines Noul, Choice, or ordered Score questions.
See `examples/evaluator-definition.json`.

A case contains `id`, `input`, `output`, and optional `reference`. Expected benchmark
labels do not belong in the live case. Explicit task references are allowed.

```sh
veyro evaluator preview evaluator.json case.json
veyro evaluator run evaluator.json case.json --profile small
```

Results include Boolean, categorical, or bounded continuous feedback as appropriate.
Noul's numeric value is P(yes). Score's value is the expected value over the configured
ordered outcomes. Choice confidence is the source-reported concentration formula,
**not** a probability that the answer is correct. Score confidence is unavailable;
Veyro does not invent the unpublished formula.

## Correct and calibrate

A correction record contains a case plus reviewed outcome labels keyed by question name.
The correction command adds or replaces that case in a JSONL file:

```sh
veyro evaluator correct evaluator.json reviewed-record.json corrections.jsonl
veyro evaluator preview evaluator.json another-case.json --corrections corrections.jsonl
veyro evaluator run evaluator.json another-case.json --corrections corrections.jsonl
```

Few-shot examples are bounded and kept whole. Current-case and reserved holdout IDs
cannot appear among corrections. ID checks cannot detect renamed duplicates or expected
labels hidden in ordinary text; dataset review is still necessary.

For each question, collect labelled training predictions as JSON records containing
`id`, `probabilities`, and the correct outcome `label`. Reserve separate case IDs first:

```sh
veyro evaluator calibrate evaluator.json question_name train.json holdout-ids.json fitted.json \
  --profile small
veyro evaluator validate-calibration fitted.json holdout-predictions.json
veyro evaluator run evaluator.json case.json --profile small \
  --calibration question_name=fitted.json

# Multi-feature post-hoc decision head (still requires an independent holdout)
veyro evaluator fit-decision-head evaluator.json training-features.json holdout-ids.json head.json \
  --profile 14b --feature preservation_regression --feature placeholder_present \
  --regularization 0.01
veyro evaluator validate-decision-head head.json holdout-features-and-labels.json
```

Artifacts bind the model manifest identity, profile, complete evaluator, question, and
readout protocol, and the actual rendered few-shot examples. Pass the same `--corrections`
file to `calibrate` and `run` when using examples. Changed examples require a new fit.
Artifacts record training IDs/hash and reserved holdout IDs. Fitting uses
training labels only. Validation reports held-out log loss and Brier score; either can
worsen. This fits a scalar temperature, not new model weights or a task-trained head.

The full demonstration can be reproduced without another inference run:

```sh
python tools/calibrate_local_readout.py docs/local-readout-small-benchmark.json \
  --split examples/local-calibration-split.json --output-dir /tmp/veyro-calibration-demo
```

That split uses an already-observed synthetic development benchmark. It is not an
untouched production holdout and must not be used to certify production accuracy.

## Scoring reliability (protocol `qwen-label-readout-v4`)

Finite-label readout scores the model's next-token distribution over option labels. Four
scoring-path defects made that unreliable, and all four are fixed. The protocol string is
`qwen-label-readout-v4`; calibrations fitted under an older protocol are refused, not reused.

1. **Scoring position.** The prompt ended where the model starts prose, so the label tokens
   held about 7e-07 of the distribution and the scores were renormalized noise. The answer
   turn now ends with `Option label:`.
2. **Thinking-only weights.** See the warning above; the engine derives the answer preamble
   from the model's chat template and refuses thinking-only prompts.
3. **Label alphabet.** Labels mixed digits, single letters and letter pairs scored as bare
   tokens. Bare numbers are single tokens only up to 9, and `a` is a token prefix of `aa`, so
   only the first ten options were addressable and every 32- or 128-option lookup picked an
   index below 10. Labels are now `A`..`Z`, `AA`.. bound to space-prefixed single tokens.
4. **Evidence and instructions.** Text inside the evidence overrode the question at almost
   full confidence on both profiles. Evidence is now fenced between `<<<EVIDENCE` and
   `EVIDENCE>>>` markers and followed by a reminder that it is data.
5. **Evidence serialization.** `json.dumps` put the whole state on one line with escaped
   `\n`, which hid indentation and position. Evidence now renders with real newlines,
   `|` prefixed code lines and 1-based list indices. Note that `indent=2` alone does not fix
   this, because multiline strings stay escaped. State hashing still uses `json.dumps`, so
   case identity and calibration provenance do not change.

Measured with `tools/benchmark_readout_reliability.py`, which runs every case in both
question orders and asserts zero generated tokens:

| Case set | small before | small after | 14B after |
|---|---:|---:|---:|
| development, 94 questions | 38 | **90** | 90 |
| independent holdout, 86 | 72 | **82** | 82 |
| confirmation holdout, 80 | not run | **72** | 70 |

Selected-label mass rose from about 7e-07 to at least 0.99997. Each case set was authored by
a separate reviewer that could not see earlier results.

### Known weak categories

These are measured model limits, not scoring defects. Do not "fix" them by editing the cases.

- **Exact counting** is the main residual failure on both profiles. Adding explicit list
  indices fixed none of four counting cases, and the result flips between protocol versions.
  A single forward pass with no generated tokens has nowhere to hold a running count.
- **Positional code questions** such as "is the docstring the first body statement" improved
  but are not solved: one small case and two 14B cases still fail.

Use `tools/verify_local_service.py <profile>` to confirm the invariants on a running service:
label choice must survive reversed options, and a changed state must change the answer.

## Measure your workload

```sh
python tools/benchmark_local_readout.py --profile small \
  --output /tmp/readout-small.json
python tools/benchmark_local_readout.py --profile 14b \
  --output /tmp/readout-14b.json
```

Run profiles sequentially. The report separates model startup, new evidence, shared-prefix
reuse, question counts, context lengths, and outcome counts. It records zero generated
answer tokens and retains incorrect predictions.

The engine reads selected rows of the existing language-model head. It prefills common
evidence once, copies sequence references to isolated question suffixes, batches those
suffixes, and reads logits. It does not generate score JSON and does not cache completed
judgments. It retains only the latest evidence prefix. This is not a claim to reproduce
Jev's undisclosed internals or to have trained new outcome-specific readout heads.

Measured on an M4 Pro with 48 GiB RAM, using 12 synthetic cases with two repetitions:

| Measure | Qwen3-4B `small` (v1) | Qwen3-4B `small` (v4) | Qwen3-14B (v4) |
|---|---:|---:|---:|
| Completion classifications correct | 16/24 | **24/24** | 24/24 |
| Individual criteria correct | 18/48 | **44/48** | 48/48 |
| Accepted completions | 0 | 8 | 8 |
| False acceptances / negative cases | 0/16 | 0/16 | 0/16 |
| Warm-prefix p95 | 448 ms | 968 ms | 1,477 ms |
| Overall p95, mixed new/reused prefixes | 928 ms | 1,532 ms | 3,126 ms |
| Binary Brier | 0.2723 | **0.0814** | 0.0425 |
| Peak process RSS | 3.87 GB | 3.91 GB | 10.64 GB |

Under protocol v1 the small model rejected every completion, so its acceptance precision
was undefined, not 100%. Under v4 it accepts correctly with no false acceptance on 16
negative cases, and its warm-prefix p95 of 968 ms meets the sub-second goal.

The 14B column is also protocol v4. Its warm-prefix p95 of 1,477 ms still **misses** the
sub-second goal. Under v1 the same 14B benchmark reported 48/48 criteria and a Brier of
0.000132; do not read those as the better result. They came from a prompt shape whose
selected-label mass was near zero, so they described a near-degenerate distribution. The 14B model passed this small development
set, but that does not establish deployment accuracy. It also missed the warm p95 <1s goal.
Long evidence and large option sets take seconds. Queue waiting is part of request latency.

The previous Qwen2.5-7B profile achieved 34/48 criteria and rejected every completion.
Its preserved results are in `local-readout-qwen25-7b-benchmark.json`; 4B is not a quality
improvement over that baseline. Neither small model is production-qualified. Selected-label mass can be tiny even when
renormalized scores look confident; raw scores are not calibrated probabilities of correctness.

The calibration examples reserve six development cases and fit six others. They are not
an independent holdout. On 14B under v4 this fitting **hurt** one question on the reserved
partition: `contract_documented` Brier rose from 0.0424 to 0.0778. Treat a fitted
temperature as unvalidated until independently labelled workflow data exists. Their multiclass Brier sums errors across outcomes; the binary
benchmark Brier uses only P(yes), so the two numeric scales differ.

### Native workflow evidence

`local-prime-14b-canary.json` records a real all-local Prime workflow that completed,
passed its declared checks and experimental docstring/implementation-presence evaluator,
but passed only **7/8 held-out executable assertions**. Its Unicode handling was wrong.
This is a false completion against the full task, not an end-to-end success. The first
evaluator call took about 8 seconds despite an already-loaded service. The isolated readout
benchmark does not predict latency under coding load or after a long idle period.

OpenCode/14B finally passed **8/8 held-out assertions** with the unchanged verifier and
local evaluator in 479.5 seconds. This run used the real native executable, disabled only
automatic titles, and allowed a 600-second budget. Those changes were combined, so this
trial does not isolate which change caused success. Its checkpoint took about 9.1 seconds.

Earlier OpenCode/14B trials timed out before a plan was written. Reports named `contended` or
`default-thinking-timeout` must not be used as clean latency comparisons. Earlier blocked
Prime sessions continued in the daemon after their TUIs closed and competed for inference.
The adapter now aborts completed and blocked work without closing a successful TUI. The canary explicitly stops daemon sessions belonging
to its disposable workspace. Unit coverage verifies cancellation and cleanup ownership.

The current small-profile reports also failed: Prime stayed in planning at its 300-second
deadline. OpenCode reached building in one 300-second run but still passed only 2/8
assertions; a direct-binary 180-second trial stayed in planning. These are bounded failures,
not proof that every longer run would fail. No verifier was changed. See `GATES.md` for outstanding
acceptance conditions rather than treating a completed infrastructure build as reliable
model behavior.

### Verify the real service

```sh
python tools/verify_local_service.py small --output /tmp/local-service-proof.json
```

This sends six concurrent requests to an already-ready worker. The recorded small-profile
run rejected unauthenticated inference with HTTP 401, admitted two requests, rejected four
with HTTP 429, and reused the evidence prefix. Neither accepted request generated tokens.
It is an admission/authentication check, not a semantic-accuracy benchmark.
The tool also compares a question alone and after an adversarial sibling, reverses option
order, and changes the state. Both model reports retain those distributions. Numerical
sibling isolation uses a 0.01 tolerance; these small probes do not prove universal invariance.
The 14B probe preserved its choice when options were reordered and changed its answer when
the state changed. The 4B probe instead changed its answer with option order and failed the
state-change check. This is further evidence against using its raw scores as a stopping evaluator.

### Remaining limits

- All three specification sources are now recorded. The third is stored at
  [docs/sources/building-a-harness-with-jev.md](sources/building-a-harness-with-jev.md);
  `tools/check_jev_source.py` re-checks it against the claims made about it. It documents
  TypeSafe AI's Jev and its LangChain integration, not a supervisor design, so it does not
  validate Veyro's checkpoint or stopping logic.
- The readout uses existing LM-head rows, not newly outcome-trained heads or Jev internals.
- No independently validated, passing workflow calibration is available; the frozen 18-case G6 decision-head holdout failed its accuracy, Brier, and log-loss gates.
- The small readout fails quality tests. The 14B workflow still has a held-out false completion.
- Native denial controls and executable checks remain authoritative. Do not weaken them to
  make a local model appear successful.

### Native executable selection

The deployment resolves the verified OpenCode executable, disables model discovery and
automatic updates, pins the configured Ollama model, and runs the complete process tree in
a macOS network sandbox that allows loopback only:

```sh
veyro agent opencode --autonomous \
  --coding-profile coder30 --evaluation-profile laya \
  --evaluator examples/local-native-canary-judge.json \
  --allow-uncalibrated-evaluator --check 'python verify.py' \
  --prompt 'Implement the requested task.'
```

Do not pass a native `--model` argument. Veyro rejects it before launch. Native permission
denials remain in force. A pinned profile proves model identity; the operating-system
sandbox prevents accidental cloud fallback or remote discovery during the run.

### Final verification and cleanup

The full suite passed 938 tests. A traced rerun treating unraisable exceptions as errors
also passed all 938; an earlier transient asyncio cleanup warning remains recorded. Ruff,
diff checks, offline packaging, isolated installed CLI/resource checks, and a byte comparison
of all 71 installed source files passed. Task-owned workers were stopped and coding models
unloaded after validation. The original services on 8080 and 11434 remain running.
