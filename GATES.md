# Gates: complete local Veyro harness

Scope: Implement remaining two-profile localhost serving, fast scoring, typed evaluators,
calibration workflow, and all-local native integration. Preserve existing uncommitted work.

- [x] G0: Trace the complete three-source specification without inventing inaccessible content.
  EVIDENCE: all three sources are recorded in-repo. The third was recovered from the public
  unauthenticated FixTweet API and is stored verbatim at docs/sources/building-a-harness-with-jev.md
  with the machine-readable Draft.js payload at docs/sources/building-a-harness-with-jev.json
  (51 blocks, 5 code blocks, 24 entities). It was fetched twice from separate processes with
  byte-identical results. No login, credentials, or paywall bypass was used.
  CHECK: .venv/bin/python tools/check_jev_source.py
  EXPECT: status passed; asserts the 10 phrases that back our design claims are present and that
  supervisor vocabulary is absent. Both directions are mutation-tested in tests/test_jev_source.py.
  CORRECTION: the article is "Building a Harness with Jev" by Sydney Runkle (LangChain), published
  2026-09-18. "Sydney" is the AUTHOR'S NAME, not a system name; earlier notes calling it "the Sydney
  article" or implying a Jev/Sydney coding-agent supervisor were wrong. It documents TypeSafe AI's
  Jev System One model and its LangChain integration, and names only TypeSafeClassifier,
  ModelRouterMiddleware, and AutoModeMiddleware.
  SCOPE LIMIT: the article contains no supervisor, checkpoint, rubric, or stop-condition material,
  so it does NOT justify Veyro's checkpoint schema, stopping logic, or judge. Those are Veyro's own
  design; see docs/native-judge.md.
  CONFIRMED AGAINST OUR IMPLEMENTATION: the three typed question kinds (Noul yes/no, Choice with
  per-option probabilities plus an overall confidence, Score over ordered levels), one shared state
  per request, parallel evaluation of every question, and "reinforcement learning for calibrated
  decisions (RLCD)" all match src/veyro/evaluators.py and the shared-prefix readout. The article
  does NOT disclose the Score confidence formula, so normalize_result leaves Score confidence unset
  with confidence_semantics="not_available" rather than inventing one.
- [x] G1: Small and 14B profiles resolve exact installed weights and run on loopback with warm-state verification.
  CHECK: .venv/bin/python -m pytest -q tests/test_local_models.py
  EXPECT: passed
  EVIDENCE: pinned model-profiles.json; both real service warmups and Ollama residency verified; docs/local-service-small-proof.json verifies 401/429 and reuse.
  REPIN: small is now qwen3:4b-instruct-2507-q4_K_M (digest 0edcdef3...). qwen3:4b is "Qwen3 4B Thinking 2507": its chat template always emits <think> with no enable_thinking branch, so zero-generation label scoring is invalid for it.
- [x] G2: Direct numeric readout runs on both Qwen sizes without an autoregressive JSON loop; shared-prefix state reuse is measured.
  EVIDENCE: docs/local-readout-{small,14b}-benchmark.json; zero generated tokens, reversed branch order, real shared-prefix cache hits.
  SCORING RELIABILITY (protocol qwen-label-readout-v4): four scoring-path defects fixed - scoring position, thinking-only weights, label alphabet, evidence/instruction separation - plus evidence serialization. Small development set 38/94 -> 90/94; independent holdout 72/86 -> 82/86; confirmation holdout 72/80. 14B: 90/94, 82/86, 70/80. label_mass 7e-07 -> >=0.99997. docs/local-service-small-proof.json now reports option_order_preserves_label: true and state_change_matches_expected: true (both were false).
  RESIDUAL LIMITS (measured, not fixed): exact counting fails on BOTH profiles and is unstable across protocol versions; positional code questions ("docstring as first body statement") still fail on one small case and two 14B cases.
- [x] G3: Typed finite decisions, evaluator mapping/preview, reusable correction examples, and calibration artifacts are implemented and tested.
  CHECK: .venv/bin/python -m pytest -q tests/test_evaluators.py
  EXPECT: passed
  EVIDENCE: tests/test_evaluators.py; 85 independent integration tests plus correction-bound calibration regressions.
- [ ] G4: Both native CLIs complete disposable tasks using local coding and judging for both profiles.
  EVIDENCE: local-prime-14b-canary.json completes but fails held-out Unicode behavior (7/8); OpenCode14B passed 8/8 with local judge in 479.5s, direct binary and title disabled; small native runs remain failed (2/8). G4 is NOT met for both CLIs/profiles.
  v4 RETRY (2 fresh runs, docs/local-prime-14b-canary-v4.json): G4 REMAINS UNMET and the defect is now reproduced independently of the scoring fixes. Run 1: phase blocked, 7/8, the model wrote re.sub(r"[\W_]+", "-", text); Python's \W is Unicode-aware so É is a word character and "CAFÉ" -> "café" instead of "caf" (verified by running all 8 fixture cases against the generated slug.py via docs/evidence/g4_unicode_probe.py, which is rerunnable against any candidate slug.py directory). Its judge also returned judge_model_error because the 14B service had been stopped during resource cleanup - an operator error, not a code defect. Run 2 with the service running: phase blocked, 2/8, slug.py left as the identity stub after repeated SyntaxError edits, transitions plan_ready + five checks_failed + continuation_limit. The 8-case fixture and the verifier were NOT weakened; the live verifier still asserts only the first 3 cases, so the Unicode case remains genuinely held out.
  PROMPT EXPERIMENT (2026-09-20, negative; raw runs in docs/evidence/g4-prompt-experiment/): prompt wording does NOT close G4. Six variants x 3-5 fixed seeds at temperature 0 on the small profile: the CURRENT prompt is best or tied-best at 1/3 8/8, and every added instruction introduced a WORSE failure mode (B/C/E caused run-collapse ' Hello, World! ' -> 'hello--world'; F stripped all separators -> 'helloworld'; F is 0/4 valid after excluding one probe timeout). Even a leaky control variant that states the held-out expectation outright did not reliably help, and is not shippable because it destroys the overfitting probe. TWO CORRECTIONS to the earlier diagnosis: (1) the root cause is Unicode-aware default predicates generally, not the \W class specifically - the small model never uses regex, it writes char.isalnum(), and passing runs use char.isascii() and char.isalnum(); (2) the prompt was never underspecified - it already says 'lowercase ASCII letters and digits', and re.sub(r'[^a-z0-9]+','-',text.lower()).strip('-') derived from that wording alone satisfies all 8 cases. THE BINDING PROBLEM IS RELIABILITY, NOT WORDING: at temperature 0 with an identical prompt the small profile scores 8/8 on seed 2 but 7/8 on seeds 1 and 3, so a 33% pass rate cannot support a 'canaries complete' claim. An earlier 14B batch was DISCARDED as invalid because 10 of 12 nulls were the probe's own 300s HTTP timeout, not model failures. 14B OFFLINE RESULT (docs/evidence/g4-prompt-experiment/14b-corrected.json): with an adequate token budget and a corrected code-block extractor, qwen3:14b scored 8/8 on 6/6 one-shot runs including the held-out Unicode case, on the EXISTING prompt (variant C added nothing). This does NOT close G4's 14B half: the probe scores a single reply, whereas the canary runs the full agent loop, and canary run 2 failed the LOOP (2/8, identity stub after repeated SyntaxError edits), not the code-writing step. Three probe bugs were found and fixed during this work (300s timeout, num_predict 700, last-block extraction); each would have masqueraded as a model failure. Closing G4 needs a more reliable SMALL profile and a demonstrated agent loop on both profiles, or an honestly narrowed scope; it must NOT be closed by editing the fixture, verifier or rubric.
- [x] G5: Cold/warm latency, question/context scaling, accuracy and false accepts are logged for both profiles; warm judge p95 <1s is measured or explicitly unmet.
  EVIDENCE: small warm p95 448ms but 18/48 criteria and zero accepts; 14B warm p95 1343ms (SLO UNMET), 48/48 development criteria. Both full shape matrices recorded.
  v4 RERUN (small): docs/local-readout-small-benchmark.json warm-prefix p95 968ms (<1s goal MET), median 669ms; completion 24/24, acceptance_precision 1.0 with 16 negative cases (was zero accepts), criteria 44/48, binary Brier 0.0814, generated_tokens 0.
  v4 RERUN (14b): docs/local-readout-14b-benchmark.json warm-prefix p95 1477ms, median 1259ms - the sub-second goal is STILL UNMET for 14B, and slightly worse than the 1343ms v1 figure. Completion 24/24, acceptance_precision 1.0 with 0 false accepts on 16 negative cases, criteria 46/48 (v1 reported 48/48), binary Brier 0.0425 (v1 reported 0.000132), all-latency p95 3126ms, peak RSS 10.64 GB, generated_tokens 0. The v1 criteria and Brier figures are NOT a better result to prefer: they were produced by a prompt shape whose selected-label mass was near zero, so they described a near-degenerate distribution.
- [ ] G6: Independent holdout validation distinguishes raw scores from calibrated probabilities; no fabricated model-training claims.
  EVIDENCE: docs/local-calibration-{small,14b}/report.json are partitioned synthetic development studies, NOT an untouched workflow holdout. Trained task-specific decision heads are not implemented.
  docs/local-calibration-small/ regenerated against the v4 benchmark (the calibrator refuses a stale protocol): contract_documented held-out Brier 0.267 -> 0.203, implementation_present 0.049 -> 2.4e-09.
  docs/local-calibration-14b/ ALSO regenerated under v4 from docs/local-readout-14b-benchmark.json, and it produced a NEGATIVE result that must not be hidden: on the held-out partition, temperature fitting made contract_documented WORSE (Brier 0.0424 -> 0.0778, log loss 0.0760 -> 0.1658). Only implementation_present improved. Fitting a temperature on six synthetic development cases does not generalize even to a sibling partition of the same tiny set.
  G6 REMAINS UNMET: no independent workflow-labelled holdout exists, calibration is post-hoc temperature only, and the 14B result is direct evidence that this method is not yet trustworthy. Closing G6 requires collecting independently labelled workflow outcomes, which is a data-collection task and not a code change.
- [x] G7: Existing contracts, packaging, docs and the full test suite verified; pre-existing failures identified.
  CHECK: .venv/bin/ruff check src tests tools examples
  EXPECT: All checks passed
  EVIDENCE (v4): 948 tests passed after updating the two model-identity pins in tests/test_local_models.py and tests/test_local_native.py to the Instruct model; assertions were tightened to the new exact digest, not relaxed. Ruff clean, git diff --check clean. uv build --offline requires --no-build-isolation plus a setuptools path on this offline machine; both wheel and sdist rebuilt and verified to carry protocol v4, _render, _build_labels and the repinned profile; isolated wheel import and CLI verified; sdist tests 80 passed.
  EARLIER EVIDENCE: 938 tests passed; a traced rerun treating unraisable exceptions as errors also passed938. Ruff and diff check passed. Offline wheel rebuilt, installed in isolation; profiles, CLI, adapters and all71 packaged source files verified. One transient asyncio transport cleanup warning is preserved in docs/evidence/local-final-regression.log and did not recur in the strict rerun.
