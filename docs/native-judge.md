# Typed completion evaluator

## Run a rubric after executable checks

Add `--evaluator` to an autonomous native launch:

```sh
veyro agent prime-agent --repo . --autonomous \
  --prompt "Implement and document the requested behavior" \
  --check '.venv/bin/python -m pytest -q' --evaluator rubric.json
```

Replace `prime-agent` with `opencode` for the same contract. The rubric file is
explicit opt-in to sending its selected artifacts and task to the configured
evaluator endpoint. No global agent configuration changes are needed.

Example `rubric.json`:

```json
{
  "rubric_version": "api-documentation-v1",
  "criteria": {
    "limitations_explained": "The README explains the API's supported inputs, failure modes, and known limitations without claiming unsupported behavior."
  },
  "evidence_files": ["README.md", "src/api.py"],
  "pass_threshold": 0.9,
  "provider": {
    "timeout_seconds": 30
  }
}
```

Use your actual repository paths and requirements. Prefer executable tests for
requirements they can establish. Use the typed evaluator for natural-language
contracts or other evidence that needs interpretation.

Provider defaults reuse Veyro's configured localjev/Qwen3-14B baseline: local
endpoint `http://127.0.0.1:8080`, request alias `jev-latest`, and
`TYPESAFE_API_KEY`. The evaluator reads that key from its environment or the target
repository's `.env`. The key is not copied into Veyro's run configuration or sent
as evidence. See [localjev setup](localjev.md). Other Jev-compatible endpoints can
be configured through `provider`; there is no automatic provider fallback.
Non-loopback endpoints require HTTPS. Endpoint credentials must use the named
environment variable, not URL userinfo or query parameters.

The runnable [slug rubric](../examples/native-judge.json) and
[labelled fixture set](../examples/native-judge-cases.json) are examples, not
universal acceptance tests.

## Completion policy

1. Run every required executable check.
2. If any check fails, request a repair. Do not call the typed evaluator.
3. If all checks pass and no evaluator is configured, complete as before.
4. If an evaluator is configured, read only its explicit evidence files and score
   each criterion against the original task and that evidence.
5. Complete only when every criterion meets `pass_threshold`.

A score at or above the threshold passes its criterion. A score at or below
`1 - pass_threshold` is a failure. Other scores are uncertain. Failure and
uncertainty request bounded repairs; neither counts as completion. Provider
errors, missing credentials, invalid scores, missing/oversized evidence, or a
changed evidence snapshot block automation. The deadline remains authoritative,
even if a late response says the task passed.

There are no human approval checkpoints. Native permission denials remain in
force. Evaluator scores never authorize tool use or override failed executable
checks. A valid high score can still be wrong; this policy does not turn a model
estimate into a correctness guarantee.

## Implementation boundary

`autonomy_check.py` retains the model-free state machine. Only an enabled evaluator,
after passing executable checks, starts the bounded `veyro.native_judge` child
process. Python isolated mode prevents the target repository or `PYTHONPATH` from
replacing that module. Machine responses have a separate complete 256 KB capture
limit; oversized responses are rejected, not interpreted from a diagnostic tail. Cancellation kills the child's process group. Model inference is not
added to every native event or to the default checkpoint path.

`native_judge.py` owns rubric validation, explicit artifact collection, and
score-to-verdict policy. It reuses `JevVeyroModel.assess_values` and its SDK,
authentication, retry, timeout, and provenance handling. An opt-in `strict_scores`
setting rejects finite out-of-range values rather than clamping them to passing
scores. The existing factory and supervision clients keep their prior behavior.

The current `native-rubric-v2` protocol sends independent requests per criterion,
with shared evidence but no other criterion's answer or rubric in that request.
Requests can run concurrently. This avoids deliberately conditioning one
criterion on another's answer; it cannot guarantee neural independence or
perfect calibration. We chose it after a batched local benchmark showed
order-sensitive false acceptance. The earlier results are retained, not replaced
by a success-only report.

Each result records:

- Criterion scores, threshold, and passed/failed/uncertain disposition.
- Rubric version and task, rubric, and evidence digests.
- Per-criterion provider/model/checkpoint provenance and SDK latency.
- Whole-evaluation and child-process latency.

The configured checkpoint identifies intended weights; it does not attest the
weights used for each response. These are model-generated probability estimates,
not measured calibrated correctness or verified direct token-logit readouts.

### Evidence and privacy

`evidence_files` accepts explicit repository-relative UTF-8 files. It does not
scan the repository. Symlinks, the `.env` credential file, missing files, binary
content, and a combined byte limit overrun fail closed. Evidence is not silently
truncated. The default combined limit is 32,000 bytes. A second read after the
model response detects evidence changes during judgment.

The request contains the original task, selected artifact contents, and check
exit/timeout facts. It excludes raw check output, commands, unselected files,
native transcripts, and environment values. The configured rubric is trusted;
artifact text is labelled as untrusted evidence in each question. This is
prompt-injection resistance guidance, not a security boundary.

Veyro's run config now stores the task when a typed evaluator is enabled. Results
store hashes and scores, not raw evidence. The native coding agent and configured
evaluator server may keep their own logs. Review their retention policies before
sending sensitive files. The agent and controller share an OS user; use a sandbox
or external acceptance service if a malicious agent must not modify its rubric,
tests, or control journal.

## References and design decisions

- [LangSmith LLM-as-judge documentation](https://docs.langchain.com/langsmith/llm-as-judge):
  explicit input/output mapping, structured feedback, requirement-level rubrics,
  and labelled examples. We borrow these evaluation patterns without adding a
  LangSmith service dependency. Few-shot human calibration is not implemented;
  the checked-in labels are synthetic.
- [Jev's Architecture, Unmasked](https://archerhume.com/posts/jevs-architecture-unmasked/?v=3):
  shared state plus typed questions, the distinction between numerical readouts
  and generated probability text, and the need for calibration, repeatability,
  and order-perturbation checks. Its proposed internal architecture and reported
  scores are the author's black-box findings, not independently verified facts
  about TypeSafe or Veyro's local Qwen adapter. We do not claim to reproduce its
  inferred attention masks, caching, MoE structure, or proprietary model.
- ["Building a Harness with Jev" by Sydney Runkle](https://x.com/i/article/2100744524951932928),
  stored verbatim at [docs/sources/building-a-harness-with-jev.md](sources/building-a-harness-with-jev.md):
  a LangChain post about TypeSafe AI's Jev model and the `langchain-typesafe`
  integration. We take four things from it: the three typed question kinds, one
  shared state carrying many questions per request, parallel per-question
  evaluation, and the name "reinforcement learning for calibrated decisions
  (RLCD)". It names only `TypeSafeClassifier`, `ModelRouterMiddleware`, and
  `AutoModeMiddleware`. It is **not** a supervisor specification: it never
  describes checkpoints, stopping rules, or grading a coding agent's output to
  decide whether that agent continues. Veyro's checkpoint schema, stop conditions,
  and typed evaluator are our own design and are not justified by this post. The article
  states that Score returns a confidence value but never defines it, so Score
  confidence stays unset rather than invented. Run
  `.venv/bin/python tools/check_jev_source.py` to re-verify these claims against
  the stored text.

## Repeatable evaluation

```sh
# Preview labels and test the integration without model calls.
.venv/bin/python tools/benchmark_native_judge.py --output /tmp/judge-preview.json
.venv/bin/python -m pytest -q tests/test_native_judge.py

# Actual localjev calls; repeat with reversed criterion submission order.
.venv/bin/python tools/benchmark_native_judge.py --live --repeats 2 \
  --output /tmp/judge-results.json

# Actual coding-agent TUI plus executable and semantic completion checks.
.venv/bin/python tools/benchmark_native_autonomy.py --live prime-agent \
  --model litellm/claude-haiku-4-5 --evaluator examples/native-judge.json \
  --output /tmp/prime-with-judge.json
.venv/bin/python tools/benchmark_native_autonomy.py --live opencode \
  --model litellm/claude-haiku-4-5 --evaluator examples/native-judge.json \
  --output /tmp/opencode-with-judge.json
```

The fixture set was labelled before model evaluation. It covers complete work,
missing and contradictory documentation, stubs, partial work, irrelevant output,
empty evidence, and an injected evaluator instruction. The first iteration uses
the declared criterion order; the second reverses submission order. Scores and
all errors are retained. Model errors count as rejected completions, not as valid
probabilities for Brier/calibration calculations.

Reports include completion and criterion confusion matrices, false acceptance,
false rejection, accuracy, precision, recall, Brier score, calibration-bin counts,
repeat score differences, and latency. The hypothetical no-evaluator comparison
assumes shallow executable checks passed on every fixture. It is not a claim that
well-designed executable tests would accept every bad implementation.

The small fixture set is diagnostic, not a production accuracy guarantee. Once
used to choose an implementation, it is development data, not an untouched
holdout. Keep the [initial batched results](native-judge-batched-benchmark.json)
when comparing later results. Neither a high score nor a tiny calibration table
establishes reliability for your workflow. Validate new rubrics on independently
labelled workflow examples before relying on them.

## Recorded limits

The [independent-criterion results](native-judge-benchmark.json) cover twelve
synthetic cases, each evaluated twice. Completion accuracy was **22/24 (91.7%)**,
precision **80%**, and recall **100%**. Two of sixteen negative cases were falsely
accepted: the undocumented implementation in both repetitions. This is a concrete
reason not to treat the local model as proof of semantic completion.

Criterion-level accuracy improved from **40/48 (83.3%)** in the earlier batched
experiment to **44/48 (91.7%)**. Brier score improved from **0.1963** to **0.1161**.
The observed repeat score delta was zero for independent requests, versus a
maximum delta of 1.0 in the batched experiment. This small test does not establish
general determinism. The independent run rejected the injected artifact in both
repetitions, but does not establish general prompt-injection resistance.

Independent scoring cost more: approximately **7.3 seconds median and 9.8 seconds
p95** per two-criterion evaluation, versus **3.5/5.2 seconds** for the batched
experiment. The default model-free checkpoint path is measured separately. We
selected independence for clearer criterion boundaries and the improved observed
criterion accuracy/repeatability, not because it eliminated false completion.


[Final native verification](native-judge-verification.json) records real Prime
Agent 0.9.5 and OpenCode 1.18.31 TUI runs with `native-rubric-v2`. Both completed
without task input after launch, passed all eight independent behavior cases,
kept the executable verifier unchanged, and produced a function docstring covering
the requested rules. Native end-to-end times were about 101 and 88 seconds,
respectively. The default model-free checkpoint remained at about 129 ms p95.
The broader test suite retains its pre-existing `test_noisy_events_are_coalesced`
timing failure; it is not suppressed by this change.
