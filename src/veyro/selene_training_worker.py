"""Isolated Transformers/PEFT worker for grounded Selene LoRA and QLoRA runs.

This module is launched by :mod:`veyro.selene_training` with network access denied.
It intentionally imports the optional training stack only after validating its input.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import shutil
import sys
from pathlib import Path

_MAX_INPUT_BYTES = 16 * 1024 * 1024
_MAX_RECORD_BYTES = 2 * 1024 * 1024


def main() -> None:
    try:
        payload = _payload()
        records = _read_records(Path(payload["training_path"]), preference=_preference(payload))
        development = _read_records(
            Path(payload["development_path"]), preference=_preference(payload)
        )
        _train(payload, records, development)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
        print(f"Selene training worker failed: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps({"status": "succeeded"}))


def _payload() -> dict:
    raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        raise ValueError("training payload exceeds 16 MiB")
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise ValueError("training payload is not valid JSON") from error
    required = {
        "protocol",
        "run_id",
        "run_sha256",
        "model_root",
        "model_identity",
        "training_path",
        "development_path",
        "output_directory",
        "framework",
        "framework_version",
        "hyperparameters",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("training payload fields do not match the worker protocol")
    if value["protocol"] != "selene-adapter-training-v1":
        raise ValueError("unsupported training protocol")
    for name in ("model_root", "training_path", "development_path", "output_directory"):
        path = Path(value[name])
        if not path.is_absolute():
            raise ValueError(f"{name} must be absolute")
    expected_root = os.environ.get("SELENE_MODEL_ROOT")
    if (
        expected_root is None
        or Path(expected_root).resolve() != Path(value["model_root"]).resolve()
    ):
        raise ValueError("model root does not match the pinned environment")
    hyperparameters = value["hyperparameters"]
    if not isinstance(hyperparameters, dict):
        raise ValueError("training hyperparameters must be an object")
    return value


def _preference(payload: dict) -> bool:
    return payload["hyperparameters"].get("stage") == "preference"


def _read_records(path: Path, *, preference: bool) -> list[dict]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_INPUT_BYTES:
        raise ValueError(f"unsafe training records: {path}")
    records = []
    for number, line in enumerate(path.read_bytes().splitlines(), 1):
        if not line.strip():
            continue
        if len(line) > _MAX_RECORD_BYTES:
            raise ValueError(f"training record {number} exceeds 2 MiB")
        try:
            record = json.loads(line)
        except ValueError as error:
            raise ValueError(f"training record {number} is invalid JSON") from error
        required = {"id", "prompt", "chosen"}
        if preference:
            required.add("rejected")
        if not isinstance(record, dict) or not required <= set(record):
            raise ValueError(f"training record {number} is missing required fields")
        if any(not isinstance(record[name], str) or not record[name] for name in required):
            raise ValueError(f"training record {number} has invalid text fields")
        weight = record.get("hard_negative", False)
        if not isinstance(weight, bool):
            raise ValueError(f"training record {number} has invalid hard-negative flag")
        records.append(record)
    if not records:
        raise ValueError(f"training records are empty: {path}")
    return records


def _train(payload: dict, records: list[dict], development: list[dict]) -> None:
    try:
        import torch
        import torch.nn.functional as functional
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
            Trainer,
            TrainingArguments,
            set_seed,
        )
    except ImportError as error:
        raise ImportError(
            "the pinned training runtime needs torch, transformers, and peft"
        ) from error

    settings = payload["hyperparameters"]
    _validate_settings(settings)
    set_seed(settings["seed"])
    random.seed(settings["seed"])
    model_root = Path(payload["model_root"]).resolve(strict=True)
    output = Path(payload["output_directory"])
    if output.exists() or not output.parent.is_dir() or output.is_symlink():
        raise ValueError("training output must be a new directory with an existing parent")
    output.mkdir(mode=0o700)

    method = settings["method"]
    load = {
        "local_files_only": True,
        "trust_remote_code": False,
        "low_cpu_mem_usage": True,
    }
    use_bf16 = bool(
        torch.cuda.is_available() and getattr(torch.cuda, "is_bf16_supported", lambda: False)()
    )
    use_fp16 = bool(torch.cuda.is_available() and not use_bf16)
    if method == "qlora":
        if not torch.cuda.is_available():
            raise RuntimeError(
                "reference QLoRA worker requires a CUDA runtime with bitsandbytes; "
                "use a separately pinned MLX worker on Apple Silicon"
            )
        load["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16 if use_bf16 else torch.float16,
        )
        load["device_map"] = "auto"
    elif method != "lora":
        raise ValueError("training method must be lora or qlora")
    elif use_bf16:
        load["torch_dtype"] = torch.bfloat16
    elif use_fp16 or torch.backends.mps.is_available():
        load["torch_dtype"] = torch.float16

    tokenizer = AutoTokenizer.from_pretrained(
        model_root,
        local_files_only=True,
        trust_remote_code=False,
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(model_root, **load)
    model.config.use_cache = False
    if method == "qlora":
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    layer_count = getattr(model.config, "num_hidden_layers", None)
    if not isinstance(layer_count, int) or settings["lora_layers"] > layer_count:
        raise ValueError("requested LoRA layer count exceeds the model architecture")
    lora = LoraConfig(
        r=settings["lora_rank"],
        lora_alpha=settings["lora_alpha"],
        lora_dropout=settings["lora_dropout"],
        target_modules=settings["target_modules"],
        layers_to_transform=list(range(layer_count - settings["lora_layers"], layer_count)),
        layers_pattern="layers",
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora)
    model.gradient_checkpointing_enable()

    preference = settings["stage"] == "preference"
    encoder = _PreferenceEncoder(tokenizer, settings["max_sequence_length"])
    if preference:
        training_items = [encoder.preference(item, settings) for item in records]
        development_items = [encoder.preference(item, settings) for item in development]
        collator = _PreferenceCollator(tokenizer.pad_token_id, torch)
        trainer_type = _preference_trainer(Trainer, functional, torch)
    else:
        training_items = [encoder.supervised(item, settings) for item in records]
        development_items = [encoder.supervised(item, settings) for item in development]
        collator = _SupervisedCollator(tokenizer.pad_token_id, torch)
        trainer_type = _weighted_trainer(Trainer, functional, torch)

    arguments = TrainingArguments(
        output_dir=str(output / "checkpoints"),
        overwrite_output_dir=False,
        num_train_epochs=settings["epochs"],
        learning_rate=settings["learning_rate"],
        per_device_train_batch_size=settings["batch_size"],
        per_device_eval_batch_size=settings["batch_size"],
        gradient_accumulation_steps=settings["gradient_accumulation_steps"],
        gradient_checkpointing=True,
        logging_strategy="steps",
        logging_steps=settings["logging_steps"],
        eval_strategy="steps",
        eval_steps=settings["logging_steps"],
        save_strategy="steps",
        save_steps=settings["save_steps"],
        save_total_limit=1,
        bf16=use_bf16,
        fp16=use_fp16,
        report_to=[],
        remove_unused_columns=False,
        seed=settings["seed"],
        data_seed=settings["seed"],
    )
    trainer = trainer_type(
        model=model,
        args=arguments,
        train_dataset=_ListDataset(training_items),
        eval_dataset=_ListDataset(development_items),
        data_collator=collator,
    )
    if preference:
        trainer.label_names = ["chosen_labels", "rejected_labels"]
    result = trainer.train()
    adapter = output / "adapter"
    model.save_pretrained(adapter, safe_serialization=True)
    tokenizer.save_pretrained(adapter)
    metrics = {
        "train": result.metrics,
        "evaluation": trainer.evaluate(),
        "training_examples": len(training_items),
        "development_examples": len(development_items),
    }
    (output / "training-metrics.json").write_text(
        json.dumps(metrics, indent=2, allow_nan=False) + "\n"
    )
    shutil.rmtree(output / "checkpoints", ignore_errors=True)
    _write_adapter_manifest(payload, output)


def _validate_settings(settings: dict) -> None:
    required = {
        "stage",
        "method",
        "seed",
        "epochs",
        "learning_rate",
        "max_sequence_length",
        "batch_size",
        "gradient_accumulation_steps",
        "lora_rank",
        "lora_alpha",
        "lora_layers",
        "lora_dropout",
        "target_modules",
        "hard_negative_weight",
        "preference_beta",
        "save_steps",
        "logging_steps",
    }
    if set(settings) != required:
        raise ValueError("training hyperparameter fields do not match the worker protocol")
    if settings["stage"] not in {"supervised", "preference"}:
        raise ValueError("training stage must be supervised or preference")
    numeric = [
        settings["epochs"],
        settings["learning_rate"],
        settings["lora_dropout"],
        settings["hard_negative_weight"],
        settings["preference_beta"],
    ]
    if any(isinstance(value, bool) or not math.isfinite(value) for value in numeric):
        raise ValueError("training numeric hyperparameters must be finite")


class _PreferenceEncoder:
    def __init__(self, tokenizer, maximum: int):
        self.tokenizer = tokenizer
        self.maximum = maximum

    def supervised(self, record: dict, settings: dict) -> dict:
        chosen = self._completion(record["prompt"], record["chosen"])
        chosen["example_weight"] = (
            settings["hard_negative_weight"] if record.get("hard_negative") else 1.0
        )
        return chosen

    def preference(self, record: dict, settings: dict) -> dict:
        chosen = self._completion(record["prompt"], record["chosen"])
        rejected = self._completion(record["prompt"], record["rejected"])
        return {
            "chosen_input_ids": chosen["input_ids"],
            "chosen_labels": chosen["labels"],
            "rejected_input_ids": rejected["input_ids"],
            "rejected_labels": rejected["labels"],
            "example_weight": (
                settings["hard_negative_weight"] if record.get("hard_negative") else 1.0
            ),
            "preference_beta": settings["preference_beta"],
        }

    def _completion(self, prompt: str, completion: str) -> dict:
        prefix = self.tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt}],
            tokenize=True,
            add_generation_prompt=True,
        )
        complete = self.tokenizer.apply_chat_template(
            [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": completion},
            ],
            tokenize=True,
            add_generation_prompt=False,
        )
        if complete[: len(prefix)] != prefix:
            raise ValueError("tokenizer chat template does not preserve the prompt prefix")
        if len(complete) > self.maximum:
            overflow = len(complete) - self.maximum
            if overflow >= len(prefix):
                raise ValueError("completion cannot fit within the maximum sequence length")
            prefix = prefix[overflow:]
            complete = complete[overflow:]
        labels = [-100] * len(prefix) + complete[len(prefix) :]
        if not any(value != -100 for value in labels):
            raise ValueError("training example contains no assistant target tokens")
        return {"input_ids": complete, "labels": labels}


class _ListDataset:
    def __init__(self, values: list[dict]):
        self._values = values

    def __len__(self) -> int:
        return len(self._values)

    def __getitem__(self, index: int) -> dict:
        return self._values[index]


class _SupervisedCollator:
    def __init__(self, pad_token_id: int, torch):
        self.pad_token_id = pad_token_id
        self.torch = torch

    def __call__(self, examples: list[dict]) -> dict:
        maximum = max(len(item["input_ids"]) for item in examples)
        return {
            "input_ids": self.torch.tensor(
                [
                    item["input_ids"] + [self.pad_token_id] * (maximum - len(item["input_ids"]))
                    for item in examples
                ]
            ),
            "attention_mask": self.torch.tensor(
                [
                    [1] * len(item["input_ids"]) + [0] * (maximum - len(item["input_ids"]))
                    for item in examples
                ]
            ),
            "labels": self.torch.tensor(
                [item["labels"] + [-100] * (maximum - len(item["labels"])) for item in examples]
            ),
            "example_weight": self.torch.tensor(
                [item["example_weight"] for item in examples], dtype=self.torch.float32
            ),
        }


class _PreferenceCollator:
    def __init__(self, pad_token_id: int, torch):
        self.supervised = _SupervisedCollator(pad_token_id, torch)

    def __call__(self, examples: list[dict]) -> dict:
        chosen = self.supervised(
            [
                {
                    "input_ids": item["chosen_input_ids"],
                    "labels": item["chosen_labels"],
                    "example_weight": item["example_weight"],
                }
                for item in examples
            ]
        )
        rejected = self.supervised(
            [
                {
                    "input_ids": item["rejected_input_ids"],
                    "labels": item["rejected_labels"],
                    "example_weight": item["example_weight"],
                }
                for item in examples
            ]
        )
        return {
            **{f"chosen_{key}": value for key, value in chosen.items() if key != "example_weight"},
            **{
                f"rejected_{key}": value
                for key, value in rejected.items()
                if key != "example_weight"
            },
            "example_weight": chosen["example_weight"],
            "preference_beta": examples[0]["preference_beta"],
        }


def _weighted_trainer(base, functional, torch):
    class WeightedTrainer(base):
        def compute_loss(self, model, inputs, return_outputs=False, **_kwargs):
            weights = inputs.pop("example_weight")
            labels = inputs.pop("labels")
            outputs = model(**inputs)
            losses = _sequence_losses(outputs.logits, labels, functional, torch)
            loss = (losses * weights.to(losses.device)).sum() / weights.sum()
            return (loss, outputs) if return_outputs else loss

    return WeightedTrainer


def _preference_trainer(base, functional, torch):
    class PreferenceTrainer(base):
        def compute_loss(self, model, inputs, return_outputs=False, **_kwargs):
            weights = inputs.pop("example_weight")
            beta = inputs.pop("preference_beta")
            chosen = _pair_inputs(inputs, "chosen")
            rejected = _pair_inputs(inputs, "rejected")
            policy_chosen, chosen_output = _sequence_log_prob(model, chosen, functional, torch)
            policy_rejected, _ = _sequence_log_prob(model, rejected, functional, torch)
            with torch.no_grad(), model.disable_adapter():
                reference_chosen, _ = _sequence_log_prob(model, chosen, functional, torch)
                reference_rejected, _ = _sequence_log_prob(model, rejected, functional, torch)
            advantage = (policy_chosen - policy_rejected) - (reference_chosen - reference_rejected)
            losses = -functional.logsigmoid(beta * advantage)
            weights = weights.to(losses.device)
            loss = (losses * weights).sum() / weights.sum()
            return (loss, chosen_output) if return_outputs else loss

    return PreferenceTrainer


def _pair_inputs(inputs: dict, prefix: str) -> dict:
    return {
        key.removeprefix(prefix + "_"): value
        for key, value in inputs.items()
        if key.startswith(prefix + "_")
    }


def _sequence_log_prob(model, inputs: dict, functional, torch):
    labels = inputs["labels"]
    outputs = model(
        input_ids=inputs["input_ids"],
        attention_mask=inputs["attention_mask"],
    )
    shifted_logits = outputs.logits[:, :-1, :]
    shifted_labels = labels[:, 1:]
    mask = shifted_labels != -100
    safe_labels = shifted_labels.masked_fill(~mask, 0)
    token_log_probs = (
        functional.log_softmax(shifted_logits, dim=-1)
        .gather(-1, safe_labels.unsqueeze(-1))
        .squeeze(-1)
    )
    return (token_log_probs * mask).sum(dim=-1), outputs


def _sequence_losses(logits, labels, functional, torch):
    shifted_logits = logits[:, :-1, :].contiguous()
    shifted_labels = labels[:, 1:].contiguous()
    token_losses = functional.cross_entropy(
        shifted_logits.view(-1, shifted_logits.size(-1)),
        shifted_labels.view(-1),
        ignore_index=-100,
        reduction="none",
    ).view(shifted_labels.shape)
    mask = shifted_labels != -100
    counts = mask.sum(dim=-1).clamp_min(1)
    return (token_losses * mask).sum(dim=-1) / counts.to(torch.float32)


def _write_adapter_manifest(payload: dict, output: Path) -> None:
    artifacts = []
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "adapter-manifest.json":
            continue
        artifacts.append(
            {
                "path": path.relative_to(output).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    if not artifacts:
        raise ValueError("training produced no adapter artifacts")
    manifest = {
        "schema_version": 1,
        "protocol": "selene-adapter-artifact-v1",
        "run_id": payload["run_id"],
        "run_sha256": payload["run_sha256"],
        "base_model_identity": payload["model_identity"],
        "framework": payload["framework"],
        "framework_version": payload["framework_version"],
        "stage": payload["hyperparameters"]["stage"],
        "method": payload["hyperparameters"]["method"],
        "terminal_status": "succeeded",
        "artifacts": artifacts,
    }
    (output / "adapter-manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False) + "\n"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
