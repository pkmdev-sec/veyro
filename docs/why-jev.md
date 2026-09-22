# How localjev turns Qwen3-14B into a typed assessor

Veyro sends structured state and named questions to localjev at
`http://127.0.0.1:8080`. localjev uses Ollama's `qwen3:14b` to generate answers in
the Jev-compatible response format. The Python client is `typesafe-sdk`;
`jev-latest` is a request alias, not a claim that the hosted Jev model is running.

## Seven checkpoint judgments

The existing-session control plane defines seven Noul questions in
`src/veyro/supervision/checkpoints.py`, version `supervision-checkpoint-v1`:

- `meaningful_progress`
- `work_stuck`
- `work_off_track`
- `verification_sufficient`
- `completion_supported`
- `safe_to_continue`
- `needs_human`

[Noul](https://docs.typesafe.ai/primitives/noul) represents a yes/no judgment as a
value between zero and one. The [TypeSafe Python SDK](https://docs.typesafe.ai/sdk/python)
provides the wire-protocol client; it does not select Veyro's underlying weights. All seven
questions receive the same reduced state in one SDK `system_one` request.
localjev may group inference internally; one SDK request does not imply seven
independent GPU evaluations or a fixed latency.

The separate factory runtime uses nine questions under `veyro-assessment-v1` in
`src/veyro/veyro/jev.py`. Those include implementation completeness and worker
status. They are not the existing-session checkpoint schema.

## What reaches the model

`LocalJevCheckpointAssessor` sends the checkpoint, reduced session state, and any
explicit task context. The current `supervise` command supplies no task-context
text. Normalized supervision state excludes native transcripts, tool arguments,
tool output, credentials, and file contents. It can still include sensitive IDs,
repository paths, counts, digests, and approval metadata.

The internal legacy factory observation can contain task text, Git diffs, and worker
output. Its top-level command is unavailable; do not apply the control plane's metadata-only claim.

## Response validation and provenance

`JevVeyroModel` owns SDK interaction. It rejects missing, nonnumeric, boolean, and
nonfinite scores; finite values outside the unit interval are clamped. Checkpoint
models then validate the result. Bounded SDK retries do not change provider or
model identity. Failed assessment cannot authorize delivery.

Provenance records `localjev-qwen3-14b`, the configured Qwen3 checkpoint, endpoint,
question version, and client inference settings. The
[baseline manifest](../config/baselines/localjev-qwen3-14b.json) also records the
installed `Q4_K_M` GGUF digest and localjev deployment settings.

The probabilities are prompted, model-generated estimates. They are not direct
logit reads or proven calibration. A configured checkpoint label is not an
attestation of weights used for a particular response. See
[deployment verification](localjev.md#check-readiness-and-the-installed-tag).

## Why the model does not choose an action

Capabilities, approval, freshness, native session state, and no-retry claims are
not probability questions. Veyro checks them in code. Forbidden operations never
reach semantic review. Human denial remains a veto, regardless of model scores.

[Run a synthetic assessment](../examples/README.md) to inspect this boundary
without connecting to a coding agent. For the full sequence, read
[the architecture](theory.md).
