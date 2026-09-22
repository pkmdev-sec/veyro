from __future__ import annotations

import json
from pathlib import Path

import pytest

from veyro import selene_grounding_worker, selene_training_worker


def test_training_worker_reads_supervised_and_preference_records(tmp_path):
    path = tmp_path / "records.jsonl"
    record = {
        "id": "case",
        "prompt": "Assess evidence",
        "chosen": '{"result":"yes"}',
        "rejected": '{"result":"no"}',
        "hard_negative": True,
    }
    path.write_text(json.dumps(record) + "\n")
    assert selene_training_worker._read_records(path, preference=False) == [record]
    assert selene_training_worker._read_records(path, preference=True) == [record]

    del record["rejected"]
    path.write_text(json.dumps(record) + "\n")
    with pytest.raises(ValueError, match="missing required fields"):
        selene_training_worker._read_records(path, preference=True)


def test_training_worker_rejects_empty_oversized_and_invalid_records(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_text("\n")
    with pytest.raises(ValueError, match="empty"):
        selene_training_worker._read_records(path, preference=False)

    path.write_text("not-json\n")
    with pytest.raises(ValueError, match="invalid JSON"):
        selene_training_worker._read_records(path, preference=False)

    path.write_text(json.dumps({"id": "case", "prompt": "x", "chosen": "y", "hard_negative": 1}))
    with pytest.raises(ValueError, match="hard-negative flag"):
        selene_training_worker._read_records(path, preference=False)


class FakeTokenizer:
    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
        assert tokenize is True
        prompt = [11, 12, 13]
        if len(messages) == 1:
            assert add_generation_prompt is True
            return prompt
        assert add_generation_prompt is False
        completion = messages[1]["content"]
        return [*prompt, *(100 + ord(character) % 20 for character in completion), 99]


def test_training_encoder_masks_the_prompt_and_keeps_assistant_targets():
    encoder = selene_training_worker._PreferenceEncoder(FakeTokenizer(), maximum=100)
    settings = {"hard_negative_weight": 4.0, "preference_beta": 0.2}
    record = {
        "prompt": "Prompt",
        "chosen": "yes",
        "rejected": "no",
        "hard_negative": True,
    }
    supervised = encoder.supervised(record, settings)
    assert supervised["labels"][:3] == [-100, -100, -100]
    assert supervised["labels"][3:] == supervised["input_ids"][3:]
    assert supervised["example_weight"] == 4.0

    preference = encoder.preference(record, settings)
    assert preference["chosen_labels"][:3] == [-100, -100, -100]
    assert preference["rejected_labels"][:3] == [-100, -100, -100]
    assert preference["preference_beta"] == 0.2


def test_training_encoder_refuses_to_truncate_away_the_prompt_boundary():
    encoder = selene_training_worker._PreferenceEncoder(FakeTokenizer(), maximum=2)
    with pytest.raises(ValueError, match="cannot fit"):
        encoder.supervised(
            {"prompt": "Prompt", "chosen": "yes", "hard_negative": False},
            {"hard_negative_weight": 1.0},
        )


def test_training_worker_writes_a_complete_adapter_manifest(tmp_path):
    output = tmp_path / "output"
    adapter = output / "adapter"
    adapter.mkdir(parents=True)
    artifact = adapter / "adapter.safetensors"
    artifact.write_bytes(b"adapter")
    metrics = output / "training-metrics.json"
    metrics.write_text("{}")
    payload = {
        "run_id": "run-one",
        "run_sha256": "a" * 64,
        "model_identity": "model@test",
        "framework": "transformers-peft",
        "framework_version": "test",
        "hyperparameters": {"stage": "supervised", "method": "lora"},
    }
    selene_training_worker._write_adapter_manifest(payload, output)
    manifest = json.loads((output / "adapter-manifest.json").read_text())
    assert manifest["terminal_status"] == "succeeded"
    assert {item["path"] for item in manifest["artifacts"]} == {
        "adapter/adapter.safetensors",
        "training-metrics.json",
    }


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"clauses":[],"result":"yes"}', "yes"),
        ('{\n  "clauses": [],\n  "result": "no"\n}\n', "no"),
    ],
)
def test_grounding_worker_requires_the_result_as_the_final_json_field(content, expected):
    label, prefix = selene_grounding_worker._result_and_prefix(content)
    assert label == expected
    assert prefix.endswith('"')


@pytest.mark.parametrize(
    "content",
    [
        '{"result":"yes","clauses":[]}',
        '{"clauses":[],"result":"Yes"}',
        "**Result:** yes",
    ],
)
def test_grounding_worker_rejects_noncanonical_decision_positions(content):
    with pytest.raises(ValueError, match="must end"):
        selene_grounding_worker._result_and_prefix(content)


def mlx_settings(**updates):
    value = {
        "stage": "supervised",
        "method": "lora",
        "seed": 7,
        "epochs": 2.0,
        "learning_rate": 0.0001,
        "max_sequence_length": 1024,
        "batch_size": 2,
        "gradient_accumulation_steps": 2,
        "lora_rank": 8,
        "lora_alpha": 16,
        "lora_layers": 8,
        "lora_dropout": 0.05,
        "target_modules": ["self_attn.q_proj", "self_attn.v_proj"],
        "hard_negative_weight": 3.0,
        "preference_beta": 0.1,
        "save_steps": 10,
        "logging_steps": 2,
    }
    value.update(updates)
    return value


def test_mlx_worker_converts_chat_data_and_freezes_iteration_count(tmp_path):
    from veyro import selene_mlx_training_worker as mlx_worker

    records = [
        {
            "id": "ordinary",
            "prompt": "Prompt",
            "chosen": "Yes",
            "hard_negative": False,
        },
        {
            "id": "hard",
            "prompt": "Adversarial prompt",
            "chosen": "No",
            "hard_negative": True,
        },
    ]
    converted = mlx_worker._chat_records(records, 3)
    assert len(converted) == 4
    assert converted[0]["messages"][0] == {"role": "user", "content": "Prompt"}
    payload = {
        "model_root": str(tmp_path / "model"),
        "framework_version": "0.31.3",
        "hyperparameters": mlx_settings(),
    }
    configuration = mlx_worker._mlx_config(
        payload, tmp_path / "data", tmp_path / "adapter", len(converted)
    )
    assert configuration["iters"] == 2
    assert configuration["mask_prompt"] is True
    assert configuration["lora_parameters"] == {
        "rank": 8,
        "dropout": 0.05,
        "scale": 2.0,
        "keys": ["self_attn.q_proj", "self_attn.v_proj"],
    }


def test_mlx_worker_rejects_preference_and_fractional_hard_negative_weight():
    from veyro import selene_mlx_training_worker as mlx_worker

    with pytest.raises(ValueError, match="supervised"):
        mlx_worker._validate_settings(mlx_settings(stage="preference"))
    with pytest.raises(ValueError, match="integer replication"):
        mlx_worker._validate_settings(mlx_settings(hard_negative_weight=1.5))


def test_mlx_worker_requires_quantized_input_only_for_qlora(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from veyro import selene_mlx_training_worker as mlx_worker

    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    output = tmp_path / "output"
    payload = {
        "run_id": "mlx-run",
        "run_sha256": "a" * 64,
        "model_root": str(model),
        "model_identity": "selene@test",
        "output_directory": str(output),
        "framework": "mlx-lm",
        "framework_version": "0.31.3",
        "hyperparameters": mlx_settings(),
    }
    records = [
        {
            "id": "case",
            "prompt": "Prompt",
            "chosen": "Answer",
            "hard_negative": False,
        }
    ]

    def fake_run(command, *, stdout, stderr, check):
        assert command[3:6] == ["-m", "mlx_lm.lora", "--config"]
        configuration = json.loads(Path(command[-1]).read_text())
        adapter = Path(configuration["adapter_path"])
        adapter.mkdir()
        (adapter / "adapters.safetensors").write_bytes(b"adapter")
        (adapter / "adapter_config.json").write_text("{}")
        stdout.write(b"trained\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mlx_worker.subprocess, "run", fake_run)
    mlx_worker._train(payload, records, records)
    manifest = json.loads((output / "adapter-manifest.json").read_text())
    assert manifest["framework"] == "mlx-lm"
    assert manifest["method"] == "lora"
    assert not (output / ".training-data").exists()

    quantized_model = tmp_path / "quantized-model"
    quantized_model.mkdir()
    (quantized_model / "config.json").write_text('{"quantization":{"bits":4}}')
    qlora = {
        **payload,
        "model_root": str(quantized_model),
        "output_directory": str(tmp_path / "q-output"),
        "hyperparameters": mlx_settings(method="qlora"),
    }
    mlx_worker._train(qlora, records, records)
    assert (
        json.loads((tmp_path / "q-output/adapter-manifest.json").read_text())["method"] == "qlora"
    )

    bad = {
        **payload,
        "output_directory": str(tmp_path / "bad-output"),
        "hyperparameters": mlx_settings(method="qlora"),
    }
    with pytest.raises(ValueError, match="already quantized"):
        mlx_worker._train(bad, records, records)
