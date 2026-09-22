# G4 prompt experiment: negative result

Ran on 2026-09-20 against the live local stack (Ollama on 127.0.0.1:11434) to test
whether prompt wording can close G4. **It cannot.** This directory records the raw
runs so nobody repeats the same six variants.

## Question

G4 needs both native CLIs to complete the disposable slug task on both profiles. The
14B model had written `re.sub(r"[\W_]+", "-", text)`, which fails the held-out
`CASES[6]` fixture `'CAF\u00c9' -> 'caf'` because Python's `\W` is Unicode-aware.
Could a better prompt produce a correct implementation?

## Method

`prompt_probe.py` asks the model for an implementation, extracts the last fenced code
block, `exec`s it, and scores the produced `slug` against all 8 `CASES` from
`tools/benchmark_native_autonomy.py`. Temperature 0, fixed seeds. No Veyro judge,
verifier, or fixture is involved; this measures raw model output only.

Rerun with:

    .venv/bin/python docs/evidence/g4-prompt-experiment/prompt_probe.py <spec.json>

Variant texts are in `variants.json`.

**Probe revision note.** `small-round1.json` and `small-round2.json` were generated with
an earlier extractor that took the *last* fenced code block. That version mis-captured
replies whose final block was a usage example rather than the implementation. The probe
now selects the block that defines `slug`. Re-checked: exactly one small-profile run was
affected, and it was a harness timeout already excluded above, so no published
small-profile score changes. A rerun today reproduces baseline seed 1 at 7/8 with
`CAF\u00c9 -> 'caf\u00e9'`, matching the recorded data.

## Result: no leak-free variant beats the existing prompt

Small profile (`qwen3:4b-instruct-2507-q4_K_M`), 8/8 rate:

| Variant | 8/8 | New failure mode introduced |
| --- | --- | --- |
| A current baseline | 1/3 (33%) | - |
| C warn regex class | 1/3 (33%) | run-collapse |
| D worked example (LEAKY control) | 1/3 (33%) | run-collapse |
| E ascii predicates | 1/5 (20%) | run-collapse |

One F run (seed 5) is excluded from its denominator: it was a harness timeout, not a
model failure. Nulls caused by the probe are never counted as model failures.
| B explicit non-ASCII | 0/3 (0%) | run-collapse |
| F restate separator | 0/4 valid (0%) | strips all separators |

Every added instruction traded the single Unicode failure for worse structural
failures: `' Hello, World! '` became `'hello--world'` under B/C/E, and `'helloworld'`
under F. The baseline prompt is the best or tied-best option tested.

Variant D states the held-out expectation outright. It is a diagnostic control to
separate "model cannot" from "model did not", and it is **not** a shippable prompt:
putting `CASES[6]` in the prompt destroys the overfitting probe, exactly as asserting
it in `verify.py` would. Even D did not reliably help.

## 14B profile: reliable in this offline probe (raw data `14b-corrected.json`)

With the corrected extractor and an adequate token budget, `qwen3:14b` scored **8/8 on
all 6 runs** (baseline and variant C, 3 seeds each, temperature 0), including
`CAF\u00c9 -> 'caf'`.

| Variant | 8/8 |
| --- | --- |
| A current baseline | 3/3 |
| C warn regex class | 3/3 |

Two points this does **not** establish:

1. **It does not close G4's 14B half.** This probe asks the model for code in one shot
   and scores the reply. The G4 canary runs the whole agent loop: plan, file edits, a
   verifier, retries, and a judge. Canary run 2 scored 2/8 and left `slug.py` as the
   identity stub after repeated `SyntaxError` edits - a loop failure, not a code-writing
   failure. One-shot competence does not imply loop competence.
2. **It does not justify a prompt change.** Baseline already scores 3/3, so variant C
   adds nothing on 14B while actively hurting the small profile. The correct action is to
   leave the prompt alone.

The earlier recorded 7/8 canary result came from the live TUI run, not from this probe.
Both measurements are real; they measure different things.

## Corrected probe defects (disclosed, since they affected published numbers)

Three harness bugs were found and fixed during this experiment. Each would have looked
like a model failure if reported uncritically:

1. 300s HTTP timeout, too short for a thinking model - caused 10 nulls in a discarded batch.
2. `num_predict` 700, too small - raised to 2048.
3. Extractor took the *last* fenced block, which is sometimes a usage example rather than
   the implementation - now selects the block defining `slug`. This caused the last 2
   nulls in the 14B batch; both rerun to 8/8.

## Two corrections to earlier framing

1. **The root cause is broader than `\W`.** The small model never uses regex at all.
   It writes `char.isalnum()`, which is also Unicode-aware. The shared defect across
   both profiles is *Unicode-aware default predicates*, not one regex class. Runs that
   pass use `char.isascii() and char.isalnum()`.

2. **The prompt was never underspecified.** It already says "lowercase ASCII letters
   and digits". A reference implementation derived from that wording alone,
   `re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")`, satisfies all 8 cases
   including `CAF\u00c9`. The models were given sufficient information and still chose
   Unicode-aware predicates.

## The reliability finding that matters more

The small profile is **unreliable, not merely wrong**. At temperature 0 with an
identical prompt, baseline scores 8/8 on seed 2 but 7/8 on seeds 1 and 3. G4 asks that
the canaries *complete*; a 33% pass rate does not support that claim at any prompt
tested here.

## Invalidated data, deliberately not published here

A first 14B batch (4 variants x 3 seeds) returned 10 nulls out of 12. Those were
**my own 300-second HTTP timeouts**, not model failures: `qwen3:14b` emits long
`<think>` blocks and needed a larger token budget and timeout. That batch is excluded
because reporting it as model failure would be false. The corrected probe uses
timeout 1800s, `num_predict` 2048, and strips `<think>` before extraction.

## Conclusion

G4 is not closable by prompt engineering. Closing it requires either a more reliable
local model, or narrowing G4's stated scope to the profiles it can actually support and
recording the exclusion honestly. Do not close G4 by editing the fixture, the verifier,
or the judge rubric.
