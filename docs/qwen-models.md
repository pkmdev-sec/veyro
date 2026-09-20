# Qwen model variants

Veyro has a **14B released assessor** and a **4B experimental development profile**.
The merge of the scoped supervision release did not include the local model-profile,
readout, or evaluator stack. Do not treat the two sizes as selectable backends in `main`.

## Compare the models

| | Qwen3 4B Instruct | Qwen3 14B |
| --- | --- | --- |
| Ollama tag | `qwen3:4b-instruct-2507-q4_K_M` | `qwen3:14b` |
| Recorded parameter size | 4.0B | 14.8B |
| Weight format | Q4_K_M GGUF | Q4_K_M GGUF |
| Intended use | Lower-memory local coding and finite-label evaluation experiments | Existing-session checkpoint assessment through localjev |
| Status in `main` | Not shipped; development work remains unmerged | Pinned assessor in the scoped release |

At the same quantization, 4B needs less memory for weights. Total memory also includes
context caches and loaded service copies. This does not establish a speed or accuracy
advantage. The 4B experiments have not qualified it for autonomous task completion.

The exact **Instruct** tag matters. The experimental `small` profile does not select
`qwen3:4b`, whose thinking template is unsuitable for that profile's first-answer-position
label scoring. Installing that tag is not a substitute for the 4B Instruct weights.

## The released 14B path

Existing-session supervision uses:

**Veyro checkpoints → localjev (`127.0.0.1:8080`) → Ollama (`127.0.0.1:11434`) → Qwen3 14B**

The [baseline manifest](../config/baselines/localjev-qwen3-14b.json) records the pinned
weight identity and service settings. `jev-latest` is the SDK request alias; it is not
the model's weight name. See [localjev setup](localjev.md) for installation checks.

localjev returns model-generated estimates. These are not calibrated guarantees or
direct token-logit measurements. Veyro rejects invalid checkpoint scores. Deterministic
policy and exact human approval still govern supported controls; a model score cannot
grant permission.

## The unmerged local-profile path

The development profiles are named `small` and `14b`. They select 4B Instruct and 14B
weights for local coding through Ollama and a separate GGUF readout worker.
That readout uses `llama-cpp-python` directly. It does not call localjev.

This path is distinct even when it uses the same 14B weight tag. It is not included in
the current release. `veyro local`, `veyro evaluator`, and the autonomous task options
are not available in `main`. No switching, fallback, or voting between 4B and 14B occurs
in the released existing-session control plane.

The architecture diagram shows both models with separate status labels. The 4B card
is a model key, not a connection to the active supervision path.

## Qualification limits

The development task runs have not passed across both native CLIs and both profiles.
Independent workflow-labelled calibration is also incomplete. Showing both variants
in documentation does not close those gaps. See the [release scope](release-scope.md)
and [evidence limits](what-veyro-proves.md).
