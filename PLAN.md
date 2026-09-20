# Remaining local harness implementation

## Scope and constraints

- User authorized remaining tasks from docs/evidence/harness-status-review.md.
- Native autonomy, optional judge and uncommitted earlier work are the baseline; preserve them.
- Machine: Apple M4 Pro, 48 GiB RAM. Existing Ollama models: qwen2.5:7b (small), qwen3:14b.
- Exact small profile is qwen2.5:7b for this implementation; do not claim it is Qwen3-small.
- Existing services on ports8080/11434 must not be stopped/reconfigured globally.
- Owned new services bind127.0.0.1; state/PID files are user-private and per instance.
- Prefer using existing GGUF weights for direct scoring; do not download new multi-GB model copies without need.
- Third source recovered and stored in docs/sources/; still never claim exact Jev proprietary
  internals. It describes TypeSafe's classifier and LangChain middleware, not a supervisor spec.
- Warm two-question judge p95<1s is a test target, not a promise; record raw/cold/loaded conditions honestly.
- No commits, external publishing, global shell/config changes, or cloud inference in local canaries.

## Ownership and contracts

### Parent: integration and fast scoring
Own src/veyro/cli.py, agents.py, autonomy.py, native_judge.py, integrations/*,
pyproject.toml, docs/*, tools/benchmark*.py, src/veyro/local_server.py and backend modules.
Own integration with independent model-profile and evaluator modules after children finish.
Fast backend choice must follow an actual native-interface proof. Direct label-logit readout
is distinct from trained/calibrated correctness; experimental raw scores must be marked.

### Worker local-models: profile and Ollama residency lifecycle
Own ONLY src/veyro/local_models.py, tests/test_local_models.py, config/model-profiles.json.
Use standard library + existing pydantic/httpx2 if needed. Stable interface:
- ModelProfile pydantic config: id, ollama_model, expected_digest (required for shipped profiles),
  parameter_size, quantization, model_family.
- load_profiles(path: Path | None = None) -> dict[str, ModelProfile]. Shipped profiles small/14b.
- LocalModels(base_url='http://127.0.0.1:11434'): inventory(), status(profile), warm(profile),
  gguf_path(profile) -> Path; status must distinguish installed vs resident and digest mismatch.
  Work through documented Ollama APIs; warm via /api/generate keep_alive=-1 without user prompt.
- Do not start/stop Ollama or unload other user models. Explicit error if profiles do not fit/runtime unavailable.
- Resolve actual GGUF through Ollama manifest layers, not the model manifest digest itself.
- Only read Ollama model store; never modify weights or metadata. Profile config must package cleanly.

### Worker evaluators: generic definition and calibration primitives
Own ONLY src/veyro/evaluators.py, tests/test_evaluators.py, examples/evaluator-definition.json.
Pure/config module, no network/model calls. Stable concepts:
- Typed questions: noul (yes/no), choice (named outcomes/descriptions), score (ordered outcomes).
  Runtime output normalization/Choice confidence must match the formula reported in source audit,
  with semantic labels explicit; distinguish score concentration from correctness.
- EvaluatorDefinition: version/name, questions mapping, variable mapping from input/output/reference,
  preview rendering (no code/template evaluation), validated bounded continuous feedback mapping if exposed.
- Correction records with case input/output/reference and corrected labels; load/save JSONL examples
  and build a bounded few-shot preview; never put benchmark expected labels in live holdout requests.
- Calibrator fit/apply/serialization for numeric distributions using a proper scoring loss;
  train/holdout example IDs must be disjoint and artifact includes training IDs/hash/model/profile/schema.
- Keep this small: no database/UI/framework. Tests must cover schema, mapping, correction lifecycle,
  probability bounds, confidence, calibration identity and leakage detection.

## Work sequence

1. Ground/runtime probes + ledger (in progress).
2. Parallel local profiles and evaluator primitives; parent proves direct scoring backend.
3. Build persistent local service with typed scoring and shared-state reuse; verify model identity.
4. Wire CLI/profile choices, optional judge and fully local native coding adapters.
5. Run performance/quality experiments and local native canaries, close outstanding concrete failures.
6. Reconcile documentation/ledger; deliver only measured claims, retain explicit unmet gates.

## Verification artifacts
GATES.md is the outcome ledger; .audit/local-harness-decisions.tsv is the decision trail.
Each child writes a concise result to /tmp/veyro-<worker>-handoff.md. Parent reruns tests.

## Fast backend contract (selected for local proof)

Compare: (a) Ollama JSON generation (existing baseline, seconds), (b) MLX model conversion
(would duplicate existing weights), (c) llama.cpp raw logits over existing GGUF (selected).
Selection preserves installed model identity and exposes explicit prefill/KV primitives.

- ReadoutEngine(model_path, profile, context_size, max_questions).evaluate(state, questions)
  owns one model/context and serializes access. It tokenizes a shared evidence prefix once,
  copies its sequence references to sibling suffixes, batches suffix tokens, and reads logits
  at each final position. Numeric option probabilities come from selected single-token label
  rows of the existing language-model head; no autoregressive answer/JSON loop.
- Prefix cache identity includes model, prompt protocol and exact encoded shared evidence.
  Questions and option descriptions stay in suffixes; branch suffixes never attend siblings.
  The single latest prefix can persist across calls; no stale completed-score cache is used.
- Qwen3 uses its no-thinking assistant prefix; Qwen2.5 uses ordinary ChatML.
- This is a label-logit readout of a pretrained model, NOT a freshly outcome-trained head.
  Raw probabilities must be explicitly marked uncalibrated. Calibration is a separate artifact
  with model/profile/schema identity; synthetic demonstrations never count as workflow holdouts.
- A loopback stdlib HTTP service owns the persistent engine, exposes /health and /v1/evaluate,
  requires an instance token for inference, bounds requests and queued work, never executes tools.
- CLI lifecycle, native coding configuration and judge routing are launch-scoped. Native agents
  remain tool executors; deterministic checks remain authoritative. Preserve explicit native denials.

### Worker local-service: persistent loopback service/lifecycle (parent ownership amended)
Own ONLY src/veyro/local_server.py, tests/test_local_server.py. Do not edit readout.py,
local_models.py, evaluators.py, CLI or docs. Import ReadoutEngine/ReadoutQuestion from readout.
Implement executable module + service management functions, parent integrates Typer:
- start_service(profile_id, *, port=None, state_dir=None, context_size=8192) -> dict
- service_status(profile_id, *, state_dir=None) -> dict
- stop_service(profile_id, *, state_dir=None) -> dict
- evaluate_local(profile_id, state, questions, *, timeout=60, state_dir=None) -> dict
  questions wire shape: {name: {"text":str, "outcomes":[{"name":str,"description":str},...]}}.
  Return ReadoutEngine.evaluate result plus exact profile/model identity and instance ID.
- serve(profile_id, port, state_dir, context_size) foreground executable module.
Use local_models load_profiles()/LocalModels.gguf_path() once available. Resolve profile model_family
for ReadoutEngine.family qwen2.5/qwen3. Backend receives existing GGUF path, not Ollama generated scores.
Service loopback only; default ports8081 small/8082 14b, never mutate8080/11434. Runtime metadata/logs
under user-private ~/.local/state/veyro/readout by default; allow state_dir for isolated tests.
Bearer instance token, protected metadata, no unauthenticated inference/shutdown, Content-Type JSON,
strict request-size/question/outcome/timeout bounds, no request-controlled model/file paths.
Persistent engine serializes model state; bounded queue rejects excess rather than unbounded thread work.
Health must distinguish loading vs ready; identity includes pinned weight/profile and protocol. Shutdown
through authenticated endpoint, never kill a PID merely read from a state file. Reject invalid/stale/mismatched
metadata safely. Lifecycle idempotent, use per-profile advisory lock for start to prevent double workers;
spawn `sys.executable -I -m veyro.local_server ...` to prevent repository module shadowing.
Start may wait boundedly for readiness; no global config/service changes. For shutdown, engine currently
has close() but no cancel() yet; it evaluates with timeout. Parent can add cancellation if necessary.
Tests exercise real HTTP server with lightweight fake engine; do not load/warm actual models while parent
runs GPU proof. Write /tmp/veyro-local-service-handoff.md. Keep module as small as these guarantees allow.

### Worker local-integration-tests (new verification leaf)
Own ONLY tests/test_local_evaluation.py, tests/test_local_judge.py, tests/test_local_cli.py,
tests/test_readout.py. Read current implementation contracts, but do not edit src/tools/docs/other tests.
Cover CLI preview/correct/calibrate round trips and safe errors; typed evaluator-to-readout mapping,
malformed probabilities/protocol/outcomes, source concentration versus correctness, calibration model/schema
mismatch and correction leakage. Cover local JudgeConfig provider union rejecting malformed local config
instead of silently using Jev, explicit uncalibrated opt-in, local checkpoint without remote credentials,
checks/evidence/provenance binding and existing denial behavior. Test ReadoutEngine preflight and prompt
special-token control isolation using lightweight fakes (no GPU/model loads). Parent owns actual GPU tests.
Use real temporary files/HTTP where useful, project .venv tests. Write /tmp/veyro-local-integration-tests.md
with exact results and source-backed bugs; do not hide a discovered defect by weakening a test.

## Implementation evidence and remaining acceptance

- Pinned Qwen3-4B/14B GGUF profiles, authenticated persistent readout, shared KV prefixes,
  finite typed evaluators, human-correction records and correction-bound scalar calibration exist.
- Real small-service proof checks 401 authentication, two admitted/four rejected requests and
  cached-prefix reuse. Direct readout reports cover both profiles and all promised shapes.
- The full suite reached 938 passes. The two legacy timing-sensitive tests now use event
  barriers; their original assertions and production policy are unchanged.
- Prime's daemon survives TUI detach. Both completed and blocked native work now receive an
  abort; disposable canaries separately stop only daemon sessions in their own workspace.
- Native OpenCode configuration proves a global build deny survives launch-scoped overrides.
  Automatic titles are disabled only in local sessions; all helper model selection stays local.
- Qwen3-4B's old template ignores the API thinking flag at its assistant prefix. The soft
  request hint has not made the Prime canary succeed. The 14B template does honor the flag.
- Small-model scoring reliability is now debugged and fixed at protocol qwen-label-readout-v4.
  Four scoring-path defects were localized by running identical cases on both profiles:
  the label was scored where the model writes prose, the small profile pinned thinking-only
  weights, the label alphabet could not address more than ten options, and evidence text
  overrode the question. Evidence serialization also hid code position. Small development
  accuracy moved 38/94 -> 90/94, the independent holdout 72/86 -> 82/86, and a third
  confirmation holdout scores 72/80; label mass moved from 7e-07 to at least 0.99997. The
  small profile is repinned to qwen3:4b-instruct-2507-q4_K_M. See docs/local-harness.md.
- Two residual weaknesses are measured, not fixed, and must stay visible: exact counting fails
  on BOTH profiles and is unstable across protocol versions, and positional code questions
  still fail on one small case and two 14B cases. These are capability limits of zero-generation
  scoring; the cases must not be edited to make them pass.
- Qualification is still incomplete: Prime14B has a held-out Unicode false completion, the
  small profile still fails the native canaries, the 14B readout benchmark and the 14B
  calibration predate v4, independent workflow calibration data and trained decision heads are
  absent. See GATES.md, not a broad “done” claim.
