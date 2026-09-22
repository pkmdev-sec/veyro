# Historical Veyro harness status review (superseded)

> **Historical snapshot.** This read-only review predates the checked-in local readout,
> Qwen3 4B coding profile, Qwen3 14B typed evaluator, and recovery of the complete Jev
> article. Its component table records the gaps at that earlier point. Do not use it as
> current implementation status. See [Qwen3 model roles](../qwen-models.md),
> [the local harness guide](../local-harness.md), and [GATES.md](../../GATES.md).

## Verdict

The native harness and optional typed evaluator work. The complete performance-critical,
two-size local decision harness is not built. The principal gap is the model/serving
layer, not another prompt: localjev generates score JSON with a chat model, rather
than implementing the direct predictive readout and shared-state branch execution
proposed in Archer Hume's article.

This was a read-only review of Veyro, the running localjev checkout, current
localhost service metadata, and recorded benchmarks. No inference or implementation
changes were made for this review. Existing changes remain uncommitted.

## Source-of-truth boundaries

The three links are not a single formal specification:

1. LangSmith: https://docs.langchain.com/langsmith/llm-as-judge
   - Configurable evaluators, prompt/model selection, input/output/optional-reference
     mapping, preview, named Boolean/categorical/continuous feedback, per-assertion
     grading, datasets/experiments, and optional human-correction/few-shot alignment.
   - The guide describes a product workflow. It does not require Qwen, localhost,
     a particular inference architecture, or a specific speed target.
2. Archer Hume: https://archerhume.com/posts/jevs-architecture-unmasked/?v=3
   - Shared state plus runtime-defined typed questions/outcomes; prediction distinct
     from downstream policy; finite choices, yes/no and ordered scores.
   - Reports Jev's claimed non-autoregressive probability output. Proposes direct
     readouts, shared prefill/KV, isolated question branches, option interaction,
     branch batching and outcome-directed calibration/training.
   - Exact heads, attention/cache implementations and MoE are hypotheses, not
     recovered source code. Readout implementations are alternatives, not a list
     of mandatory modules. Dense Qwen is not disqualified merely for lacking MoE.
3. Sydney Runkle: https://x.com/sydneyrunkle/status/2100754364545761643
   - Official metadata identifies the article “Building a Harness with Jev” at
     https://x.com/i/article/2100744524951932928 . Its preview says an LLM decides,
     a tool executes, a model evaluates, and the loop continues until completion.
   - Full article content was not retrievable. Native curl verified TLS but the
     article returned an unavailable/404 page. Syndication exposes only metadata
     and a truncated preview. Detailed requirements cannot be invented from it.

Consequently full three-article compliance remains unverified. The explicit
small-Qwen/14B and localhost requirements come from the user.

## Component status

| Component | Status | Actual implementation / gap |
|---|---|---|
| Native Prime/OpenCode integration | Implemented | Launch-scoped hooks; no terminal scraping; real TUI canaries passed. |
| Plan/build/verify/repair/stop | Implemented | Shared checkpoint state machine; checks authoritative; bounded follow-ups, cancellation, deadline and duplicate handling. |
| Typed evaluator | Implemented, limited | Optional versioned criteria, explicit artifacts, strict probability bounds, per-criterion scores/provenance, errors block completion. It remains fallible. |
| Complete action/tool loop | Delegated/partial | Native agents select and execute tools. Veyro judges turn completion/idle, not every tool action. No separate generic Jev-driven tool planner is demonstrated. Detailed Sydney requirements unknown. |
| Generic evaluator authoring | Partial | JSON rubric/criteria and saved results exist. No general variable/reference mapping, arbitrary prompt preview, evaluator registry/UI parity, or automatic human-correction/few-shot loop. |
| Noul, Choice, ordered Score API | Partial across repositories | localjev implements all three, dynamic JSON schemas, normalized vectors and scalar scores. Veyro's native evaluator exposes only Noul-style criterion probabilities. |
| Confidence semantics | Not equivalent | localjev uses normalized entropy; Archer reports normalized maximum probability for Choice and a distinct Score formula. Native Noul path does not use these summaries. Wire shape is not semantic equivalence. |
| Direct probability readout | Missing | localjev sends /chat/completions with a JSON schema and parses generated text. No exposed Qwen readout heads/logit scorer ends inference after prefill. |
| Shared-state prefill/KV branching | Missing in this integration | Veyro sends one full-state request per criterion. Reusing a Python dictionary or concurrency is not shared neural computation. No application-managed shared-state KV branch engine is implemented. |
| Isolated branch attention / listwise option readout | Missing as a model mechanism | Separate calls provide criterion separation. No shared-cache attention isolation or trained option-set-aware scoring implementation is present. |
| Scheduling/backpressure | Partial | localjev has a semaphore, queue limits, group sizing and retries; Veyro submits independent requests concurrently. This is not the proposed packed-branch scheduler after one prefill. |
| Outcome-trained/calibrated probabilities | Missing | Generic Qwen generated estimates; diagnostic synthetic benchmarks exist, but no trained decision heads or validated workflow calibration. |
| Localhost service | Implemented now | Verified localjev on 127.0.0.1:8080 and Ollama on 127.0.0.1:11434. Ready reports qwen3:14b. |
| Qwen14B option | Working external deployment | Qwen3:14b Q4_K_M manifest, matching installed digest, local canaries and judge benchmarks. Service installation/model provisioning are external to Veyro. |
| Small-Qwen option | Not integrated | qwen2.5:7b is installed, but there is no supported small profile, manifest, model-specific routing or recorded Veyro accuracy/latency validation. “Small” needs an exact model/size definition. |
| Resident models / warm start | Missing guarantee | Ollama /api/ps returned no resident models. localjev readiness checks model inventory, not that weights are warm. |
| Reproducible two-model deployment | Missing | localjev is a separate custom checkout. Its recorded revision matches the running checkout, but Veyro docs say it is not publicly fetchable. No bundled start/stop/profile installer. |
| Entire coding workflow offline | Not established | Local evaluator is real; prior native coding canaries used remote litellm/claude-haiku-4-5. Local-only coding-provider tests are still needed if that is required. |

## Performance and accuracy evidence

- Default cold checkpoint subprocess + trivial verifier + journal: p95 about 129 ms.
  This is not model-inference latency.
- Independent two-criterion Qwen14B judge: median 7.3 s; p95 9.8 s.
- Earlier batched judge: median 3.5 s; p95 5.2 s, with order-sensitive errors.
- Final judge: 22/24 completion classifications correct; 80% acceptance precision;
  two false acceptances among sixteen negative cases. The dataset is small,
  synthetic and became development data after design selection.
- Native coding + checks + judge canaries: roughly 101 s Prime / 88 s OpenCode,
  eight behavior assertions passed per CLI; not general coding-task accuracy.
- Latest recorded complete suite: 675 passes and the known pre-existing
  test_noisy_events_are_coalesced timing failure. Later focused verification:154 passes.

“Absolute blazing speed” is not met by the semantic path. The articles do not set
an SLO for local Qwen. Archer's hosted service timings cannot be transplanted to
this laptop or compared with Veyro's no-model checkpoint time as like-for-like.

## Specific remaining build tasks, in order

1. Finish the spec inventory: obtain the full Sydney article; make a traceability
   list separating required product behavior from Archer's proposed mechanisms.
   Define the exact small Qwen checkpoint and numeric cold/warm latency and quality
   budgets per hardware, context length, question count and concurrency.
2. Build reproducible dual-model localhost profiles: pin small +14B weights,
   tokenizer, quantization and runtime; add explicit judge-profile selection;
   route to correctly identified workers without globally mutating a shared
   server. Changing SDK alias jev-latest does not select a different Qwen weight.
   Package/reproduce the compatible localjev build and readiness/digest probes.
3. Implement warm serving: persistent workers/connection pools, preload and
   residency policy, warm-vs-inventory readiness, bounded batching/queueing and
   per-profile resource limits. Do not reload weights or duplicate model residency
   accidentally for every criterion or task.
4. Implement the fast scoring backend if Archer's proposal is the required target:
   choose a concrete direct-readout design over each Qwen backbone; return numeric
   predictions without an autoregressive JSON-generation loop; train or adapt the
   readout and calibrate it on outcomes. This is model/runtime work, not a prompt
   edit. A next-token restricted-label shortcut is not automatically equivalent to
   an outcome-trained calibrated decision model.
5. Implement shared-state execution: tokenize/prefill shared evidence once;
   branch criterion suffixes against reusable prefix KV; enforce sibling isolation;
   batch branches; process full option sets for Choice/Score. Test isolation and
   option interactions before optimizing. Preserve hashes/model/rubric identity
   when introducing caches; stale judgments must not be reused.
6. Finish the generic evaluator/decision surface: Noul/Choice/Score and, where
   required, Boolean/categorical/bounded-continuous feedback; input/output/reference
   mapping; named reusable evaluators and prompt preview; per-assertion results;
   explicit stages for dependent decisions. Align confidence formulas or label
   differences. Add human correction/few-shot datasets if LangSmith parity is required.
   Only add a separate action selector/tool registry if the full harness article
   or chosen ownership contract requires Veyro to own that part instead of native agents.
7. Establish quality gates: independent labelled workflow holdouts, per-size
   calibration, false-acceptance limits, threshold validation, prompt-injection,
   missing-evidence and option/question-permutation tests. Current high scores can
   be confidently wrong. Preserve failures rather than retuning only to the tiny fixture.
8. Prove deployment/performance end to end: both sizes × both CLIs; cold/warm,
   short/long context, 1/N questions and outcomes, load/backpressure, model memory,
   prefill/readout/decode time, p50/p95/p99, errors and full loop duration. If the
   requirement is fully local coding, configure local coding providers and run
   those canaries without the remote LiteLLM service. Keep default no-model speed
   as a separate regression test.

## Source-code evidence

Veyro: src/veyro/autonomy_check.py; src/veyro/native_judge.py;
src/veyro/autonomy.py; src/veyro/config.py; src/veyro/veyro/jev.py;
src/veyro/supervision/checkpoints.py; docs/native-judge-benchmark.json;
docs/native-judge-verification.json; config/baselines/localjev-qwen3-14b.json.

Running localjev checkout: ~/localjev, revision
0a2d1b889ce1a056e13feddce8fd04532c116d78. src/engine.ts: typed preparation,
JSON-schema/chat inference, distribution normalization/confidence, queue/semaphore;
src/config.ts: one upstreamModel and generic Jev aliases; src/types.ts: typed domains.
Full source inventory: /tmp/veyro-harness-source-audit.md.
Local GET-only probe snapshot: /tmp/veyro-harness-local-audit.json.
