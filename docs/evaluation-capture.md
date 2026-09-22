# Development prediction capture

`tools/capture_evaluation.py` retains complete evaluator responses with their input and
provenance. It is the first G6 v2 development increment, not a qualification runner.
The failed `.audit/g6-independent-v1/` study remains sealed and must not be rerun or tuned.

Write task requirements and criterion rubrics before inspecting candidates. A task JSON has
`task_id`, `repository`, `requirement`, a `criteria` object mapping stable IDs to rubrics,
and `protected_suite_sha256`, the SHA-256 commitment to the externally controlled check suite.
The repository identifier must be canonical across your dataset. Freeze its capture contract:

```sh
.venv/bin/python tools/capture_evaluation.py freeze task.json contract.json \
  --profile 14b --partition development
```

Use `calibration` only for the separately reserved calibration partition after evaluator
design is fixed. The contract pins the configured model digest, current protocol, evaluator
schema and ordered feature schema. Criteria become individual binary evaluator questions.
Changing task rubrics changes the feature schema; pooling task-specific schemas into one
decision head is not implemented here.

Supply candidate content as JSON, for example a mapping of file paths to their complete
contents. The candidate digest is SHA-256 over canonical JSON, not a Git commit or raw-file
hash. `veyro.evaluation_capture.digest` implements that encoding. Check evidence JSON has:

- `candidate_sha256`: digest of that exact candidate content.
- `protected_suite_sha256`: the task's check-suite commitment.
- `criterion_evidence`: one nonempty evidence string for every criterion ID, recording the
  checks run, their results and relevant output. Record missing coverage explicitly.

```sh
.venv/bin/python tools/capture_evaluation.py capture contract.json candidate.json checks.json \
  captures/case-001 --case-id case-001 --state-dir /path/to/local-service-state
```

The parent directory must exist; the attempt directory must not. Capture reserves the directory
and writes an attempt record before inference. It retains the contract, exact case, full response
and successful prediction receipt. A model failure consumes the attempt; a provenance mismatch
retains the response without producing a successful receipt. Existing directories and frozen
contracts are never overwritten. Each receipt binds model, protocol, profile, evaluator and
feature schemas, task contract, candidate, check evidence and case content.

These tests establish capture behavior, not improved model accuracy. There is no automatic
retry, calibration fitting, aggregate completion probability, dataset split enforcement,
custodian authentication or sealed-label scoring in this increment. A new directory can create
another capture: one attempt per study case requires a future study-level manifest and controller.
Hash bindings detect mismatched supplied data; they do not prove that checks were run honestly,
that requirements predate candidate inspection, or that the data custodian is independent.

For qualification, use a trusted colleague with separate storage inaccessible to evaluator
development. They must select fresh repositories/tasks, hold the final labels, and receive all
sealed predictions before scoring. Checks supplied as evaluator evidence must be distinct from
withheld checks or review used to assign final labels. Do not expose final labels through the
evidence channel. Another agent with access to this workspace is not an independent custodian.

Next steps are a dataset-level split/exclusion manifest, authenticated check receipts, development
experiments measuring positive recognition, a frozen aggregation rule, calibration using only
its reserved partition, and independent prediction sealing/reveal. Holdout capture is deliberately
unavailable until that workflow exists. G6 requires two new independent passing holdouts with
balanced accuracy >= 0.889, both class accuracies >= 0.833, Brier <= 0.20, log loss <= 0.60,
and zero high-confidence false completions at a preregistered threshold. A production false-accept
bound below 1% at 95% confidence requires about 299 independent negative cases with
zero false accepts. G4 writer reliability remains a separate gate.
