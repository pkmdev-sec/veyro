# G4 diagnosis: the judge did not accept the Unicode false completion

Written while investigating a request to "repair the supervisor/judge logic so it
rejects the false completion in Prime/14B". The request's stated root cause does not
match the recorded evidence. This file records what the evidence shows, so the wrong
fix is not attempted again.

## The claim under test

> Prime/14B produces `re.sub(r"[\W_]+", "-", text)` scoring 7/8, but the judge
> accepted a false completion that fails on "CAFÉ".
> Root cause: supervisor accepts wrong implementations.

## What the evidence actually shows

Source: `docs/local-prime-14b-canary.json` (the 7/8 `completed` run) and
`examples/local-native-canary-judge.json` (the rubric that run used).

The judge was asked exactly two questions:

| Criterion | Question text | Truth for the defective code | Judge score |
| --- | --- | --- | --- |
| `docstring_present` | "The slug function has a Python docstring." | true | 0.9999999999 |
| `implementation_present` | "The slug function contains executable normalization logic, not a placeholder or unconditional fixed return." | true | 0.9999999999 |

The defective implementation does have a docstring, and it does contain real
normalization logic rather than a placeholder. **Both judge answers were correct.**
The judge was never asked whether the slug output was correct, and it never saw the
expected outputs. It cannot have accepted a claim it was not asked to make.

The 8/8 figure is not a judge score. It is a separate post-hoc measurement:
`benchmark_native_autonomy.py` runs all 8 `CASES` against the produced `slug.py`
after the agent finishes. The agent's own executable gate, `verify.py`, asserts only
`CASES[:3]`. The `CAFÉ` case is `CASES[6]`, deliberately held out.

So the run reached `completed` because:

1. `verify.py` passed. All three asserted cases genuinely pass for the defective code.
2. The judge's two semantic criteria were genuinely satisfied.

Neither gate was wrong about its own question. The gap is that **no gate tested
non-ASCII behavior**, because the held-out case is held out on purpose. Holding it out
is what makes the benchmark able to detect an overfitted implementation at all.

## Why the requested fix is the wrong fix

"Make the judge reject this implementation" can only be achieved by giving the judge
the held-out expected outputs, or by asserting `CASES[6]` in `verify.py`. Either one
destroys the measurement:

- Adding `CASES[6]` to `verify.py` removes the only held-out probe of overfitting. The
  canary would then report 8/8 forever, and a future regression of exactly this kind
  would become undetectable.
- `GATES.md` records for both the v3 and v4 runs that "The 8-case fixture and the
  verifier were NOT weakened". Editing either would falsify that record.
- `PLAN.md` states the residual failures "must not be edited to make them pass".

The measurement is behaving correctly. It caught a real model defect. A benchmark that
reports a model weakness is not itself defective.

## What the real defect is

The 14B model wrote `re.sub(r"[\W_]+", "-", text)`. Python's `\W` is Unicode-aware, so
`É` counts as a word character and survives; the task asked for ASCII letters and
digits only. This is a **model capability limit**, reproducible with
`docs/evidence/g4_unicode_probe.py`. It is not a bug in Veyro's supervisor, judge,
evaluator, or verifier.

`docs/local-harness.md` already states the correct design position: "Native denial
controls and executable checks remain authoritative. Do not weaken them to make a
local model appear successful."

## What would legitimately close G4

G4 requires both native CLIs to complete disposable tasks on both profiles. Closing it
requires better model behavior, not different scoring code. Options, none of which is a
judge repair:

1. A stronger local model, or better prompting, so the model writes `[^a-z0-9]+`
   against a casefolded ASCII string.
2. Accept that the small profile cannot pass and restate G4's scope to the profiles it
   actually targets, recording the exclusion honestly.
3. Leave G4 open and ship with executable checks authoritative, which is the current
   documented position.

Option 1 is a model/prompt experiment. It cannot be verified by editing the judge.
