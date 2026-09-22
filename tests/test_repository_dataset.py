from __future__ import annotations

import json
from pathlib import Path

import pytest

from veyro import repository_dataset as dataset

SOURCE = "export function valid(x) { return x > 0; }\n// END\n"


def test_ssh_alias_identity_must_resolve_to_github(monkeypatch):
    monkeypatch.setattr(dataset.subprocess, "check_output", lambda *a, **k: "hostname github.com\n")
    assert dataset.canonical_repository("git@personal:example/one.git") == "github.com/example/one"
    assert (
        dataset.canonical_repository("https://github.com/example/one.git")
        == "github.com/example/one"
    )
    monkeypatch.setattr(
        dataset.subprocess, "check_output", lambda *a, **k: "hostname elsewhere.test\n"
    )
    with pytest.raises(ValueError, match="resolve"):
        dataset.canonical_repository("git@personal:example/one.git")


@pytest.fixture
def recipe():
    return dataset.TaskRecipe.model_validate(
        {
            "id": "positive",
            "role": "training",
            "repository": "github.com/example/one",
            "source_root": "/example/one",
            "revision": "a" * 40,
            "module": "lib/check.mjs",
            "evidence_start": "export function valid",
            "evidence_end": "// END",
            "criteria": [
                {
                    "id": "boundary",
                    "text": "Reject zero; accept positive integers.",
                    "assertion": "assert.equal(m.valid(0), false); assert.equal(m.valid(1), true);",
                }
            ],
            "visible_assertion": "assert.equal(m.valid(1), true);",
            "variants": [
                {"id": "original", "edits": []},
                {"id": "inclusive", "edits": [{"old": "x > 0", "new": "x >= 0"}]},
            ],
        }
    )


def development(recipe):
    return recipe.model_copy(
        update={"id": "dev", "role": "development", "repository": "github.com/example/two"}
    )


def test_repository_roles_reject_reserved_retired_and_overlap(recipe):
    dev = development(recipe)
    reservation = {"test_repositories": [{"identity": "github.com/example/reserved"}]}
    dataset.validate_roles([recipe, dev], reservation, set())
    for identity in ["github.com/example/one", "GITHUB.COM/EXAMPLE/ONE"]:
        with pytest.raises(ValueError, match="retired"):
            dataset.validate_roles([recipe, dev], reservation, {identity})
    with pytest.raises(ValueError, match="reserved"):
        dataset.validate_roles(
            [recipe, dev.model_copy(update={"repository": "github.com/example/reserved"})],
            reservation,
            set(),
        )
    with pytest.raises(ValueError, match="cross"):
        dataset.validate_roles(
            [recipe, dev.model_copy(update={"repository": recipe.repository})], reservation, set()
        )


def test_candidate_keeps_mutation_in_visible_evidence(recipe):
    candidate, excerpt = dataset.make_candidate(recipe, SOURCE, recipe.variants[1])
    assert "x >= 0" in candidate and "x >= 0" in excerpt
    assert "// END" not in excerpt
    with pytest.raises(ValueError, match="exactly once"):
        dataset.make_candidate(recipe, SOURCE.replace("x > 0", "false"), recipe.variants[1])
    with pytest.raises(ValueError, match="outside"):
        dataset.make_candidate(
            recipe,
            SOURCE,
            dataset.Variant(id="hidden", edits=[dataset.Edit(old="// END", new="// changed")]),
        )


def test_evidence_requires_exact_context(recipe):
    with pytest.raises(ValueError, match="context prefix"):
        dataset.make_candidate(
            recipe.model_copy(update={"context_prefix": "invented"}), SOURCE, recipe.variants[0]
        )


@pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").exists(), reason="macOS sandbox required")
def test_labels_come_from_executed_checks_and_visible_smoke_is_distinct(recipe):
    good, _ = dataset.make_candidate(recipe, SOURCE, recipe.variants[0])
    bad, _ = dataset.make_candidate(recipe, SOURCE, recipe.variants[1])
    positive = dataset.execute_checks(recipe, good)
    negative = dataset.execute_checks(recipe, bad)
    assert positive["results"]["boundary"]["passed"] is True
    assert negative["results"]["boundary"]["passed"] is False
    assert positive["results"]["visible"]["passed"] is True
    assert negative["results"]["visible"]["passed"] is True
    assert positive["harness_sha256"] == negative["harness_sha256"]
    assert positive["candidate_sha256"] != negative["candidate_sha256"]


def test_dataset_never_puts_protected_assertions_or_results_in_prompt(
    tmp_path, monkeypatch, recipe
):
    dev = development(recipe)
    monkeypatch.setattr(
        dataset,
        "read_source",
        lambda task: (
            SOURCE if task.role == "training" else SOURCE.replace("x > 0", "x > 0 /* dev */")
        ),
    )

    def checks(task, candidate):
        return {
            "candidate_sha256": dataset.sha256(candidate.encode()),
            "results": {
                "visible": {"passed": True},
                "boundary": {"passed": ">=" not in candidate, "error": "PRIVATE_LABEL_DETAIL"},
            },
        }

    monkeypatch.setattr(dataset, "execute_checks", checks)
    output = tmp_path / "dataset"
    summary = dataset.build_dataset([recipe, dev], {"test_repositories": []}, set(), output)
    assert summary["roles"]["training"]["count"] == 2
    assert summary["roles"]["development"]["count"] == 2
    for filename in ("train.jsonl", "valid.jsonl"):
        rows = [json.loads(line) for line in (output / filename).read_text().splitlines()]
        assert {row["messages"][-1]["content"] for row in rows} == {"Yes", "No"}
        for row in rows:
            prompt = row["messages"][0]["content"]
            assert "PRIVATE_LABEL_DETAIL" not in prompt
            assert recipe.criteria[0].assertion not in prompt
            assert row["prompt_sha256"] == dataset.sha256(prompt.encode())
    with pytest.raises(FileExistsError):
        dataset.build_dataset([recipe, dev], {"test_repositories": []}, set(), output)


def test_identical_sources_across_repositories_rejected(tmp_path, monkeypatch, recipe):
    monkeypatch.setattr(dataset, "read_source", lambda task: SOURCE)
    with pytest.raises(ValueError, match="identical source"):
        dataset.build_dataset(
            [recipe, development(recipe)], {"test_repositories": []}, set(), tmp_path / "dataset"
        )


def prepare_test_study(study, recipe):
    task = recipe.model_copy(update={"role": "test"})
    rule = {
        "repositories": [task.repository],
        "tasks_per_repository": 1,
        "criteria_per_task": 1,
        "variants_per_task": 2,
    }
    (study / "experiment.json").write_text(json.dumps({"test_selection_rule": rule}))
    experiment_sha = dataset.sha256((study / "experiment.json").read_bytes())
    (study / "adapter").mkdir()
    (study / "adapter/weights").write_bytes(b"test-weights")
    (study / "adapter-manifest.json").write_text(
        json.dumps(
            {
                "experiment_sha256": experiment_sha,
                "files": [{"name": "weights", "sha256": dataset.sha256(b"test-weights")}],
            }
        )
    )
    (study / "test-reservation.json").write_text(
        json.dumps(
            {
                "test_repositories": [
                    {
                        "identity": task.repository,
                        "path": task.source_root,
                        "revision": task.revision,
                    }
                ]
            }
        )
    )
    (study / "dataset").mkdir()
    (study / "dataset/contract.json").write_text(json.dumps({"source_sha256": {"train": "a" * 64}}))
    experiment = {
        "test_selection_rule": rule,
        "bound_files": {
            name: dataset.sha256((study / name).read_bytes())
            for name in ("test-reservation.json", "dataset/contract.json")
        },
    }
    (study / "experiment.json").write_text(json.dumps(experiment))
    experiment_sha = dataset.sha256((study / "experiment.json").read_bytes())
    manifest = json.loads((study / "adapter-manifest.json").read_text())
    manifest["experiment_sha256"] = experiment_sha
    (study / "adapter-manifest.json").write_text(json.dumps(manifest))
    (study / "train.attempt.json").write_text(json.dumps({"experiment_sha256": experiment_sha}))
    (study / "train.result.json").write_text(
        json.dumps(
            {
                "status": "succeeded",
                "adapter_manifest_sha256": dataset.sha256(
                    (study / "adapter-manifest.json").read_bytes()
                ),
                "attempt_sha256": dataset.sha256((study / "train.attempt.json").read_bytes()),
            }
        )
    )
    return task


def test_reserved_test_keeps_labels_separate_and_binds_adapter(tmp_path, monkeypatch, recipe):
    task = prepare_test_study(tmp_path, recipe)
    monkeypatch.setattr(dataset, "read_source", lambda task: SOURCE)
    monkeypatch.setattr(
        dataset,
        "execute_checks",
        lambda task, candidate: {
            "candidate_sha256": dataset.sha256(candidate.encode()),
            "results": {"visible": {"passed": True}, "boundary": {"passed": ">=" not in candidate}},
        },
    )
    result = dataset.build_reserved_test([task], tmp_path)
    assert result["case_count"] == 2
    cases = [json.loads(line) for line in (tmp_path / "test-cases.jsonl").read_text().splitlines()]
    assert all(len(case["messages"]) == 1 and "label" not in case for case in cases)
    labels = [
        json.loads(line) for line in (tmp_path / "test-labels.jsonl").read_text().splitlines()
    ]
    assert {label["label"] for label in labels} == {"yes", "no"}
    contract = json.loads((tmp_path / "test-contract.json").read_text())
    assert contract["labels_sha256"] == dataset.sha256(
        (tmp_path / "test-labels.jsonl").read_bytes()
    )
    with pytest.raises(FileExistsError):
        dataset.build_reserved_test([task], tmp_path)


def test_reserved_test_rejects_revision_drift_before_reading_source(tmp_path, monkeypatch, recipe):
    task = prepare_test_study(tmp_path, recipe).model_copy(update={"revision": "b" * 40})

    def forbidden(task):
        raise AssertionError("source must not be read")

    monkeypatch.setattr(dataset, "read_source", forbidden)
    with pytest.raises(ValueError, match="revision"):
        dataset.build_reserved_test([task], tmp_path)


def test_reserved_test_rejects_changed_adapter(tmp_path, recipe):
    task = prepare_test_study(tmp_path, recipe)
    (tmp_path / "adapter/weights").write_bytes(b"changed")
    with pytest.raises(ValueError, match="adapter has changed"):
        dataset.build_reserved_test([task], tmp_path)


def test_reserved_test_rejects_matching_recipe_and_reservation_drift(tmp_path, recipe):
    task = prepare_test_study(tmp_path, recipe)
    path = tmp_path / "test-reservation.json"
    reservation = json.loads(path.read_text())
    reservation["test_repositories"][0]["revision"] = "b" * 40
    path.write_text(json.dumps(reservation))
    task = task.model_copy(update={"revision": "b" * 40})
    with pytest.raises(ValueError, match="frozen study file"):
        dataset.build_reserved_test([task], tmp_path)
