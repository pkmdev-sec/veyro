# Local model roles

Veyro's local autonomous harness assigns a generative Qwen coder and a non-generative
Laya evaluator to different jobs.
They work in one bounded task loop but run through separate local inference paths.

| Role | Profile | Model | Runtime | Authority |
|---|---|---|---|---|
| Coding | `coder30` | `qwen3-coder:30b` | OpenCode through a loopback Ollama adapter | Can plan, use tools, and edit files |
| Typed evaluation | `laya` | `convaiinnovations/laya/typed-decisions` | Pinned offline Safetensors adapter | Can score declared completion criteria |

The 30B writer never approves its own work. Laya cannot generate text or edit the repository.
Executable checks remain authoritative and always run before typed evaluation.

## Task flow

1. OpenCode sends the task to the Qwen3 Coder 30B model.
2. The native agent plans and edits with its normal tools and permission rules.
3. Veyro runs every declared executable check.
4. A failed check returns its bounded output to the same 30B OpenCode session. Laya is not called.
5. After all checks pass, Veyro asks Laya to score typed completion criteria against only the declared evidence files.
6. Failed or uncertain criteria return their scores to the same 30B OpenCode session for repair.
7. Passing checks and typed criteria stop the run for operator review. They do not authorize completion.

Evaluator errors, unavailable services, exhausted limits, and passing scores all leave final acceptance to the operator.

## Example for a pre-provisioned machine

The `coder30` and `laya` pair is available only on a pre-provisioned maintainer
machine that matches the pinned profiles. In particular, the Laya runtime and
checkpoint must already exist at the paths and hashes recorded by the `laya`
profile. This repository does not publish a reproducible Laya installation or
checkpoint acquisition procedure. Veyro does not download Laya, and the coding
model must also already be installed in Ollama.

The following command is a recorded example for an already-provisioned maintainer.
It is not a public setup or first-run procedure.

```sh
veyro local models
veyro local warm coder30

veyro agent opencode --repo . --autonomous \
  --coding-profile coder30 \
  --evaluation-profile laya \
  --prompt 'Implement the requested change' \
  --check '.venv/bin/python -m pytest -q' \
  --evaluator examples/native-judge.json \
  --allow-uncalibrated-evaluator
```

For this invocation, the CLI replaces the rubric's provider settings with a
launch-scoped local provider bound to the pinned `laya` profile. It does not call a
Jev endpoint or read `TYPESAFE_API_KEY`. After the executable checks pass, Veyro
sends the original task, check exit and timeout facts, and only the repository
files declared in `evidence_files` to the pinned offline Laya evaluator.

OpenCode is the only eligible strict-local coding driver. Model selection is
launch-scoped. It does not edit global provider settings or remove native
permission denials.

The evaluator normally requires a matching calibration artifact for each criterion.
`--allow-uncalibrated-evaluator` enables a raw-score experiment. It does not establish
accuracy or completion authority.

## Why separate runtimes?

Coding uses Ollama's chat API because the native agents need generative tool use. Typed
evaluation uses the pinned Laya typed-decisions encoder because it needs bounded typed
probabilities with zero generated answer tokens. Separate processes keep the writer and
critic independent:

| Service | Default endpoint |
|---|---|
| Ollama coding | `127.0.0.1:11434` |
| `small` readout, when used separately | `127.0.0.1:8081` |
| `laya` typed evaluator | On-demand offline subprocess; no listening port |

The historical `14b` Qwen readout remains available for regression comparison, but it is
not the default evaluator.

## Provenance and limits

The autonomy snapshot records both profile IDs and pinned model identities under
`model_roles`. The typed result also records readout profile and model provenance.
Ollama coding requests identify the configured model tag but do not attest the weight
digest on every response.

This architecture is implemented and covered by local end-to-end tests. Qwen3 Coder 30B
passed the structured-tool gate, three direct 8/8 seeds, and a bounded same-session repair
canary at 8/8. Its unforced native canary passed 7/8. G4 remains open for initial-pass reliability. G6 remains open until
independent workflow-labelled calibration evidence is complete. See [GATES.md](../GATES.md).

The Jev article supports typed classification and software routing. It does not specify
this coding, check, evaluation, or repair design. See the stored
[source boundary](sources/building-a-harness-with-jev.md).
