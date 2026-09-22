# Selene structured grounding and training

Status: **research-only implementation**. No Selene adapter has been trained or
qualified. The existing Qwen writer, executable checks, and human review remain
authoritative. The failed `selene-holdout-v1` study is retired and is never a data
source for this system.

The immutable internal study result and stop rules remain at
`.audit/SELENE-TRAINING-HANDOFF.md`. The internal design record remains at
`.audit/05-design-selene-grounding-training.md`, and the prerequisite observation remains at
`.audit/selene-training-system/trainable-model-observation.json`. These custody paths are not
shipped or linked from public release artifacts.

## Decision boundary

```text
typed evidence
  -> dynamically fenced model prompt
  -> clause-by-clause JSON response
  -> deterministic grounding validator
  -> authoritative executable-check result
  -> calibrated threshold, if independently fitted
  -> human approval where required
```

The model does not grant permission. `veyro.grounding.validate_grounding` grants a
provisional structured acceptance only when all of these are true:

- every criterion clause has exactly one assessment;
- every clause is `established`;
- every citation resolves to a unique evidence item;
- only implementation or executed-check evidence establishes implementation;
- every evidence type required by the clause is cited;
- every check receipt is protected, successful, and bound to the current contract
  and candidate snapshot; and
- the model's aggregate result is `yes`.

A requirement, plan, documentation page, test source file, unchecked checklist, or
untrusted artifact can explain why a clause is `missing`, `planned_only`,
`contradicted`, or `uncertain`. It cannot establish implementation. Any failed or
stale executable check rejects the assessment even when the model does not cite it.

## Commands

Render a prompt without running a model:

```sh
veyro selene render-prompt grounding-case.json
```

Validate an already produced response:

```sh
veyro selene validate-grounding grounding-case.json grounding-response.json
```

Validate split custody and leakage controls:

```sh
veyro selene validate-dataset dataset-manifest.json
```

Export a validated non-qualification split:

```sh
veyro selene export-dataset dataset-manifest.json training training.jsonl
veyro selene export-dataset \
  dataset-manifest.json training preference.jsonl --preference
```

Qualification export is always rejected. Qualification manifests contain cases,
an external label commitment, and distinct author/custodian/reviewer identities;
they do not expose a label path.

Verify all prerequisites without starting a run:

```sh
veyro selene preflight training-run.json
```

Start one immutable offline attempt:

```sh
veyro selene train training-run.json
```

The training command creates `<run-id>.attempt.json` with exclusive creation before
launch. Success, failure, timeout, and interruption produce a separate terminal
receipt. Either file prevents reuse of the run ID. A failed run is evidence, not an
invitation to select another seed after seeing protected outcomes.

Fit a frozen calibration contract:

```sh
veyro selene calibrate \
  calibration-contract.json predictions.json labels.json calibration.json
```

Preflight and create one receipt-bound deployment quantization only after an adapter succeeds:

```sh
veyro selene preflight-quantization quantization-run.json
veyro selene quantize quantization-run.json
```

The pinned MLX path fuses the verified adapter to an intermediate FP16 GGUF, invokes a
hash-bound `llama-quantize` executable, verifies one Q4_K_M artifact, and deletes the
large FP16 intermediate only after success. The attempt receipt prevents reuse of the
run ID. Quantization creation does not select the artifact.

Compare the BF16/FP16 candidate with the deployment quantization:

```sh
veyro selene compare-quantization \
  quantization-contract.json \
  bf16-predictions.json \
  quantized-predictions.json \
  labels.json \
  quantization-report.json
```

The comparison fails for incomplete score coverage, any new false accept, positive-
or negative-class regression, frozen Brier or log-loss regression, or latency and
memory limits.

Run a pinned adapter in shadow mode:

```sh
veyro selene shadow shadow-profile.json grounding-case.json
```

The receipt's authority is always `shadow_only`. Even a fully grounded model `yes`
cannot change the current controller status. A native evaluator configuration can opt in with a
hash-bound profile and live evidence bindings:

```json
{
  "grounding_shadow": {
    "profile": "/absolute/path/shadow-profile.json",
    "profile_sha256": "<64 lowercase hex characters>",
    "required_evidence_types": {
      "receipt_authentication": ["implementation", "executed_check"]
    },
    "evidence_bindings": {
      "src/receipt.py": {
        "type": "implementation",
        "relationship": "supports",
        "clause_ids": ["receipt_authentication"]
      }
    }
  }
}
```

`native_judge` builds the case from the current task, rubric, exact evidence bytes,
and current protected check receipts. It derives fresh contract and candidate hashes
at the checkpoint; empty or unbound files remain untrusted context. Every native
clause must require both implementation and executed-check evidence. The evaluator then
appends the shadow receipt, rechecks its original evidence after shadow inference,
and leaves `status` and `scores` unchanged. A shadow runtime error is recorded as
`grounding_shadow_error`; it does not become an authoritative pass or failure.

## Evidence schema

A `GroundingCase` contains:

- a stable ID, full criterion, contract hash, and candidate hash;
- one or more atomic clauses, each with explicit required evidence types; and
- typed evidence with a content hash and either source provenance or an executed
  check receipt; and
- explicit clause IDs plus a `supports`, `contradicts`, or `context` relationship.

Contradictory evidence is checked even when the model omits its ID. An established
clause with any unresolved contradiction fails deterministic validation, so a larger
number of supporting snippets cannot outvote it.

A `GroundingResponse` contains:

- the case ID;
- exactly one status, citation list, and rationale per clause; and
- a final lowercase `yes` or `no` result.

The final result is serialized last. The reference inference worker checks the exact
Yes and No token logits at that decision position and refuses a generated result
that disagrees with them. Runtime identity, generation time, response hash, and the
conditional Yes probability are added by the trusted adapter as a
`GroundingAssessment`; the model does not author those fields.

The prompt serializes all criterion and evidence text as one JSON data block. Its
backtick fence is longer than any run of backticks in the payload. Instructions
inside evidence therefore remain data even when an artifact contains markdown
fences or asks the evaluator to change its result.

## Dataset custody

`veyro.grounding_data` requires all four roles:

1. `training` changes weights;
2. `development` selects prompts and adapters;
3. `calibration` fits post-hoc probability mappings only; and
4. `qualification` is a fresh, sealed, one-attempt evaluation.

Repositories and task families cannot cross roles. Validation also rejects:

- repeated case IDs;
- exact normalized case text across roles;
- repeated evidence content hashes across roles;
- token-shingle near-duplicates above the frozen threshold;
- labels not bound to the exact case hash;
- labels authored or reviewed by the training implementer;
- model-generated labels;
- readable qualification labels; and
- any origin or source path from `selene-holdout-v1`.

A label must be backed by an executable check or independent human adjudication and
have different author and reviewer identities. Contrastive rejected responses can
be stored for preference training, but the preferred response must itself pass the
deterministic grounding validator.

## Training runtime

`veyro.selene_training` provides dependency-light run control. A training runtime is a
separate manifest that pins its Python executable, launcher, package manifest,
framework version, and supported methods. The repository includes a reference
Transformers/PEFT worker for:

- supervised LoRA;
- supervised 4-bit NF4 QLoRA on a pinned CUDA/bitsandbytes runtime;
- weighted hard negatives; and
- DPO-style preference loss using the disabled adapter as the reference policy.

The repository also includes a pinned MLX-LM worker for supervised Apple Silicon
LoRA. It uses prompt masking, exact chat JSONL, frozen LoRA layer and module choices,
and integer replication for weighted hard negatives. MLX QLoRA is accepted only
when the input manifest already describes a quantized model; it is never silently
substituted for LoRA on the official float checkpoint. Preference optimization stays
on the Transformers/PEFT worker. The Transformers worker fails rather than pretending
bitsandbytes QLoRA works on macOS. Ordinary Veyro installation does not import or
install any training packages.

Each run verifies local model, licence, runtime, dataset, disk, and output custody
before launch. Network access is denied by the macOS sandbox and Hugging Face and
dataset offline variables are forced. The model and runtime are rehashed after a
successful worker response before the adapter receipt is accepted.

## Calibration

`veyro.selene_calibration` uses exact Yes and No logits. A frozen contract may compare
temperature scaling, positive-slope Platt scaling, and isotonic calibration.
Identity is always an eligible baseline. A calibrated candidate is rejected if it
changes a predicted class or worsens frozen Brier or log loss. Calibration requires
100% score coverage and the uncalibrated candidate must first meet its frozen
classification and false-accept prerequisites.

Calibration adjusts probabilities. It does not repair a wrong classifier, establish
independence, or qualify production use.

## Current prerequisites

The official trainable repository was observed at
`AtlaAI/Selene-1-Mini-Llama-3.1-8B@427792f1c3e2073cb7da216924fd884b1ba496e0`.
Its visible files total 16,078,724,732 bytes. The card declares Apache-2.0 and names
a Llama 3.1 base model whose card declares the Llama 3.1 licence. No standalone
licence file was visible in the Selene repository tree.

That observation is not legal approval. Before download or training, an independent
record must approve fine-tuning, quantization, redistribution, and deployment under
both layers. The handoff recorded about 33 GiB free, implementation checks observed as little as
25 GiB, and the recovery session later observed 62 GiB. Free space is volatile and no
peak budget has been approved for weights, caches, checkpoints, FP16 fusion, adapters,
and quantized exports. No weights were downloaded and no training or quantization
started while preparing this implementation.

## Qualification remains separate

This implementation does not create independent labels or a qualification corpus.
The training implementer cannot provide either. A later qualification needs fresh
repositories, task families, cases, custodians, labels, contract, immutable
predictions, and independent review. Its gates must be approved and frozen before
inference. The retired holdout cannot be rerun, repaired, or cited for that claim.
