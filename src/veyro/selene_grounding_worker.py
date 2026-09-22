"""Isolated Transformers/PEFT inference worker for Selene grounding shadow mode."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path

_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_RESULT_PATTERN = re.compile(r'(?s)"result"\s*:\s*"(?P<label>yes|no)"\s*}\s*$')


def main() -> None:
    try:
        payload = _payload()
        result = _evaluate(payload)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
        print(f"Selene grounding worker failed: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result, allow_nan=False))


def _payload() -> dict:
    raw = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
    if len(raw) > _MAX_REQUEST_BYTES:
        raise ValueError("grounding request exceeds 2 MiB")
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise ValueError("grounding request is not valid JSON") from error
    required = {
        "schema_version",
        "protocol",
        "model_identity",
        "base_model_root",
        "adapter_root",
        "prompt",
        "max_output_tokens",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("grounding request fields do not match the worker protocol")
    if value["schema_version"] != 1 or value["protocol"] != "selene-grounding-worker-v1":
        raise ValueError("unsupported grounding worker protocol")
    if (
        not isinstance(value["prompt"], str)
        or not value["prompt"]
        or type(value["max_output_tokens"]) is not int
        or not 64 <= value["max_output_tokens"] <= 8192
    ):
        raise ValueError("grounding request has invalid prompt or output limit")
    for name, environment in (
        ("base_model_root", "SELENE_BASE_MODEL_ROOT"),
        ("adapter_root", "SELENE_ADAPTER_ROOT"),
    ):
        path = Path(value[name])
        expected = os.environ.get(environment)
        if not path.is_absolute() or expected is None or path.resolve() != Path(expected).resolve():
            raise ValueError(f"{name} does not match the pinned environment")
    return value


def _evaluate(payload: dict) -> dict:
    try:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as error:
        raise ImportError(
            "the pinned grounding runtime needs torch, transformers, and peft"
        ) from error

    base = Path(payload["base_model_root"]).resolve(strict=True)
    adapter = Path(payload["adapter_root"]).resolve(strict=True)
    tokenizer = AutoTokenizer.from_pretrained(
        base,
        local_files_only=True,
        trust_remote_code=False,
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    dtype = None
    if torch.cuda.is_available():
        dtype = (
            torch.bfloat16
            if getattr(torch.cuda, "is_bf16_supported", lambda: False)()
            else torch.float16
        )
    elif torch.backends.mps.is_available():
        dtype = torch.float16
    load = {
        "local_files_only": True,
        "trust_remote_code": False,
        "low_cpu_mem_usage": True,
    }
    if dtype is not None:
        load["torch_dtype"] = dtype
    model = AutoModelForCausalLM.from_pretrained(base, **load)
    model = PeftModel.from_pretrained(model, adapter, is_trainable=False)
    model.eval()
    if torch.cuda.is_available():
        model.to("cuda")
    elif torch.backends.mps.is_available():
        model.to("mps")

    messages = [{"role": "user", "content": payload["prompt"]}]
    input_ids = tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_tensors="pt",
    ).to(model.device)
    attention_mask = torch.ones_like(input_ids)
    with torch.inference_mode():
        generated = model.generate(
            input_ids=input_ids,
            attention_mask=attention_mask,
            max_new_tokens=payload["max_output_tokens"],
            do_sample=False,
            use_cache=True,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )
    completion_ids = generated[0, input_ids.shape[1] :]
    content = tokenizer.decode(completion_ids, skip_special_tokens=True).strip()
    label, prefix = _result_and_prefix(content)
    logits = _decision_logits(model, tokenizer, payload["prompt"], prefix, torch)
    selected = (
        "yes" if logits["yes"] > logits["no"] else "no" if logits["no"] > logits["yes"] else None
    )
    if selected is None or selected != label:
        raise ValueError("generated result disagrees with exact decision logits")
    return {
        "schema_version": 1,
        "protocol": "selene-grounding-worker-v1",
        "model_identity": payload["model_identity"],
        "content": content,
        "decision_logits": logits,
        "usage": {
            "prompt_tokens": input_ids.shape[1],
            "completion_tokens": completion_ids.shape[0],
        },
    }


def _result_and_prefix(content: str) -> tuple[str, str]:
    match = _RESULT_PATTERN.search(content)
    if match is None:
        raise ValueError("grounding response must end with a lowercase result field")
    label = match.group("label")
    return label, content[: match.start("label")]


def _decision_logits(
    model, tokenizer, prompt: str, response_prefix: str, torch
) -> dict[str, float]:
    chat = tokenizer.apply_chat_template(
        [{"role": "user", "content": prompt}],
        tokenize=False,
        add_generation_prompt=True,
    )
    prefix = chat + response_prefix
    prefix_ids = tokenizer(prefix, add_special_tokens=False)["input_ids"]
    if not prefix_ids:
        raise ValueError("grounding decision prefix tokenization is empty")
    label_ids = {}
    for label in ("yes", "no"):
        full = tokenizer(prefix + label, add_special_tokens=False)["input_ids"]
        if full[: len(prefix_ids)] != prefix_ids or len(full) != len(prefix_ids) + 1:
            raise ValueError("Yes and No must each be one exact token at the decision position")
        label_ids[label] = full[-1]
    tensor = torch.tensor([prefix_ids], device=model.device)
    with torch.inference_mode():
        scores = model(input_ids=tensor).logits[0, -1]
    values = {label: float(scores[token_id].float().cpu()) for label, token_id in label_ids.items()}
    if any(not math.isfinite(value) for value in values.values()):
        raise ValueError("grounding decision logits must be finite")
    return values


if __name__ == "__main__":
    main()
