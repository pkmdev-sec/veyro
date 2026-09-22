# Cross-repository evaluator training

The first repository pilot trains a local adapter and compares criterion decisions before and
after training. It does not replace the writer or qualify an evaluator for production.

**First result: training completed, but evaluator quality failed.** Reserved-test accuracy stayed
at 16/32, and the trained adapter accepted all 16 incorrect criteria. It is not selected for use.
This public page is the release summary of that result.

The internal study remains under `.audit/repository-training-v1/`, with the recovery plan at
`.audit/06-structure-outline-evaluator-recovery.md` and the handoff at
`.audit/EVALUATOR-RECOVERY-HANDOFF.md`. Those private custody paths are not part of release
artifacts. Preserve their frozen experiment, stage receipts, prediction files, and report as
historical evidence. Do not repeat attempted stages or change the training configuration after
inspecting the reserved test results.

## Data and labels

The pilot uses pinned Git revisions, leaving the repositories' working trees untouched:

| Partition | Repositories | Purpose |
| --- | --- | --- |
| Training | Forge, Hydra | Fit the adapter on 48 criterion examples |
| Development | Specter | Compare 16 criterion decisions before and after training |
| Reserved test | Sigil, Chimera | Evaluate new task families after training |

Each task has an original implementation and three small semantic mutations. All related
variants stay in the same partition. These examples exercise real repository functions, but
they are controlled mutations rather than independent historical repair tasks.

Executable assertions determine each criterion label. The builder executes candidate copies
in a network-denied sandbox that cannot read the user's home directory. The evaluator receives
the criterion, relevant implementation and the result of a separate smoke check. It does not
receive the decisive assertions or their results.

`tools/build_repository_dataset.py` validates repository roles and exclusions, retrieves source
with `git show`, checks mutation/evidence boundaries, and retains candidate files and check
receipts. `tools/build_repository_test.py` accepts only the previously reserved repository
revisions and requires a completed adapter manifest bound to the frozen experiment. Test
cases and test labels are written to separate files.

## Model and runtime

The pilot reuses the existing `zeroentropy/zerank-2` checkpoint, a Qwen3-based 4B causal model,
at revision `5eae30d5ee3c6b2df2ef6d723bde45172d761c4c`. It is not vanilla Qwen Instruct.
An offline conversion produced a separate MLX 4-bit copy for adapter training. The original
checkpoint and Ollama GGUF models remain unchanged. No previous ZeRank study data is reused.

The pinned runtime is MLX 0.30.5 and mlx-lm 0.31.1 on Python 3.14. The experiment binds model
files, runtime sources, Python, dataset files, the prompt protocol and the worker. Both baseline
and adapted predictions use the same converted model and exact Yes/No scoring position.

One fixed run trains query/value projection adapters in the last eight layers, with rank eight,
96 updates and a fixed seed. Only the final adapter is selected. The custom loss covers the
decision token and EOS, excluding padding. Preflight rejects overlength examples rather than
truncating away their answers. The trainer and scorer use the same no-thinking chat prefix.

## Evaluation and proof limits

Every stage reserves an attempt before execution and retains its terminal result. Prediction
receipts bind the experiment, model, adapter where applicable, candidate and prompt.
`tools/report_repository_training.py` requires complete baseline and adapter prediction sets,
seals their hashes, and only then reads the test label file. The model worker is also denied
read access to that label file by its OS sandbox.

The report includes both class accuracies, balanced accuracy, false accepts/rejects, Brier score
and log loss. Scores are uncalibrated conditional Yes/No probabilities. Measurements apply to
individual criteria; no aggregate artifact-completion probability is fitted.

This is an internal test. The same development workspace authors the cases and executable
labels, and related variants are correlated. There is no independent custodian, calibration
partition or replicated qualification in this pilot. Even passing internal metrics leave G6
open. A later qualification must use new cases, independent custody and its own frozen protocol.
