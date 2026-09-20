# Qwen3 model roles

Veyro's local autonomous harness assigns two pinned Qwen3 variants to different jobs.
They work in one bounded task loop but run through separate local inference paths.

| Role | Profile | Model | Runtime | Authority |
|---|---|---|---|---|
| Coding | `small` | `qwen3:4b-instruct-2507-q4_K_M` | Native agent through Ollama | Can plan, use tools, and edit files |
| Typed evaluation | `14b` | `qwen3:14b` | Veyro GGUF readout service | Can score declared completion criteria |

The 4B model never approves its own work. The 14B model never edits the repository.
Executable checks remain authoritative and always run before typed evaluation.

## Task flow

1. Prime Agent or OpenCode sends the task to the Qwen3 4B coding model.
2. The native agent plans and edits with its normal tools and permission rules.
3. Veyro runs every declared executable check.
4. A failed check returns its bounded output to the 4B coding loop. The 14B evaluator is not called.
5. After all checks pass, Veyro asks the Qwen3 14B readout service to score typed Noul criteria against only the declared evidence files.
6. Failed or uncertain criteria return their scores to the 4B coding loop for repair.
7. Veyro reports completion only when checks and typed criteria pass within the continuation and time limits.

Evaluator errors, unavailable services, and exhausted limits do not count as completion.

## Run the pair

The 14B readout service must be ready. The 4B model must be installed in Ollama.
Veyro does not download either model.

```sh
veyro local models
veyro local start 14b
veyro local status 14b
veyro local warm small

veyro agent prime-agent --repo . --autonomous \
  --coding-profile small \
  --evaluation-profile 14b \
  --prompt 'Implement the requested change' \
  --check '.venv/bin/python -m pytest -q' \
  --evaluator rubric.json
```

Use `opencode` instead of `prime-agent` for the other native adapter. Local model
selection is launch-scoped. It does not edit global provider settings or remove native
permission denials.

The evaluator normally requires a matching calibration artifact for each criterion.
`--allow-uncalibrated-evaluator` enables a raw-score experiment. It does not establish
accuracy. The older option names `--judge` and `--allow-uncalibrated-judge` remain CLI
aliases for existing scripts; new documentation uses evaluator terminology.

## Why separate runtimes?

Coding uses Ollama's chat API because the native agents need generative tool use. Typed
evaluation uses `llama-cpp-python` over the installed GGUF file because it needs bounded,
finite-label token scores with zero generated answer tokens. Separate processes also keep
service state and ports explicit:

| Service | Default endpoint |
|---|---|
| Ollama coding | `127.0.0.1:11434` |
| `small` readout, when used separately | `127.0.0.1:8081` |
| `14b` typed evaluator | `127.0.0.1:8082` |

Loading Ollama 4B and the GGUF 14B worker at the same time can use substantial memory.

## Provenance and limits

The autonomy snapshot records both profile IDs and pinned model identities under
`model_roles`. The typed result also records readout profile and model provenance.
Ollama coding requests identify the configured model tag but do not attest the weight
digest on every response.

This architecture is implemented and covered by local end-to-end tests. That does not
qualify either model for unsupervised use. G4 remains open until both native adapters and
both model paths pass the required held-out reliability evidence. G6 remains open until
independent workflow-labelled calibration evidence is complete. See [GATES.md](../GATES.md).

The Jev article supports typed classification and software routing. It does not specify
this coding, check, evaluation, or repair design. See the stored
[source boundary](sources/building-a-harness-with-jev.md).
