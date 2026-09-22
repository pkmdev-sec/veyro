"""Manifest-bound datasets and split-custody checks for grounded evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from veyro.grounding import (
    GroundingCase,
    GroundingResponse,
    render_grounding_prompt,
    validate_grounding,
)

DATASET_PROTOCOL = "veyro-grounding-dataset-v1"
RETIRED_STUDY_IDS = frozenset({"selene-holdout-v1"})
RETIRED_SOURCE_PREFIXES = (".audit/selene-holdout-v1",)
_ID_PATTERN = r"^[a-z][a-z0-9_.-]{0,127}$"
_HASH_PATTERN = r"^[0-9a-f]{64}$"
_MAX_SHARD_BYTES = 256 * 1024 * 1024
_MAX_LINE_BYTES = 2 * 1024 * 1024


class Config(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
        validate_default=True,
    )


class DatasetRole(StrEnum):
    TRAINING = "training"
    DEVELOPMENT = "development"
    CALIBRATION = "calibration"
    QUALIFICATION = "qualification"


class LabelSource(StrEnum):
    EXECUTABLE_CHECK = "executable_check"
    INDEPENDENT_HUMAN = "independent_human"


class GroundingDatasetCase(Config):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=_ID_PATTERN)
    repository: str = Field(min_length=1, max_length=1000)
    repository_revision: str = Field(min_length=1, max_length=200)
    task_family: str = Field(pattern=_ID_PATTERN)
    origin_study_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    contrastive_group: str | None = Field(default=None, pattern=_ID_PATTERN)
    grounding: GroundingCase

    @model_validator(mode="after")
    def matching_case_id(self) -> Self:
        if self.id != self.grounding.id:
            raise ValueError("dataset case ID must match grounding case ID")
        return self


class LabelReceipt(Config):
    schema_version: Literal[1] = 1
    case_id: str = Field(pattern=_ID_PATTERN)
    case_sha256: str = Field(pattern=_HASH_PATTERN)
    expected_result: Literal["yes", "no"]
    response: GroundingResponse
    rejected_response: GroundingResponse | None = None
    source: LabelSource
    author: str = Field(min_length=1, max_length=500)
    reviewer: str = Field(min_length=1, max_length=500)
    evidence_sha256: str = Field(pattern=_HASH_PATTERN)
    issued_at: datetime
    model_generated: Literal[False] = False
    hard_negative: bool = False

    @model_validator(mode="after")
    def independent_consistent_label(self) -> Self:
        if self.author == self.reviewer:
            raise ValueError("label author and reviewer must be independent")
        if self.case_id != self.response.case_id:
            raise ValueError("label response case ID mismatch")
        if self.expected_result != self.response.result:
            raise ValueError("expected result must match the preferred response")
        if self.rejected_response is not None:
            if self.rejected_response.case_id != self.case_id:
                raise ValueError("rejected response case ID mismatch")
            if self.rejected_response == self.response:
                raise ValueError("preferred and rejected responses must differ")
        return self


class QualificationCustody(Config):
    labels_commitment: str = Field(pattern=_HASH_PATTERN)
    case_author: str = Field(min_length=1, max_length=500)
    label_custodian: str = Field(min_length=1, max_length=500)
    independent_reviewer: str = Field(min_length=1, max_length=500)
    labels_revealed: Literal[False] = False

    @model_validator(mode="after")
    def distinct_owners(self) -> Self:
        owners = {self.case_author, self.label_custodian, self.independent_reviewer}
        if len(owners) != 3:
            raise ValueError("qualification author, label custodian, and reviewer must differ")
        return self


class DatasetShard(Config):
    role: DatasetRole
    cases_path: str
    cases_sha256: str = Field(pattern=_HASH_PATTERN)
    case_count: int = Field(ge=1)
    labels_path: str | None = None
    labels_sha256: str | None = Field(default=None, pattern=_HASH_PATTERN)
    qualification_custody: QualificationCustody | None = None

    @field_validator("cases_path", "labels_path")
    @classmethod
    def relative_data_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts or value == ".":
            raise ValueError("dataset shard paths must be manifest-relative")
        return value

    @model_validator(mode="after")
    def role_custody(self) -> Self:
        if self.role is DatasetRole.QUALIFICATION:
            if self.labels_path is not None or self.labels_sha256 is not None:
                raise ValueError(
                    "qualification labels must not be readable from the dataset manifest"
                )
            if self.qualification_custody is None:
                raise ValueError("qualification shard requires sealed label custody")
        elif (
            self.labels_path is None
            or self.labels_sha256 is None
            or self.qualification_custody is not None
        ):
            raise ValueError("non-qualification shards require labels and no qualification custody")
        return self


class GroundingDatasetManifest(Config):
    schema_version: Literal[1] = 1
    protocol: Literal["veyro-grounding-dataset-v1"] = DATASET_PROTOCOL
    dataset_id: str = Field(pattern=_ID_PATTERN)
    created_at: datetime
    implementer: str = Field(min_length=1, max_length=500)
    near_duplicate_threshold: float = Field(default=0.85, ge=0.5, le=1)
    shards: list[DatasetShard] = Field(min_length=4, max_length=64)
    additional_retired_study_ids: frozenset[str] = Field(default_factory=frozenset)

    @model_validator(mode="after")
    def complete_roles_and_unique_paths(self) -> Self:
        roles = {shard.role for shard in self.shards}
        if roles != set(DatasetRole):
            missing = sorted(role.value for role in set(DatasetRole) - roles)
            raise ValueError(f"dataset requires all four roles; missing={missing}")
        paths = [shard.cases_path for shard in self.shards]
        paths.extend(shard.labels_path for shard in self.shards if shard.labels_path)
        if len(paths) != len(set(paths)):
            raise ValueError("dataset shard paths must be unique")
        return self


@dataclass(frozen=True)
class ValidatedDataset:
    manifest: GroundingDatasetManifest
    manifest_sha256: str
    cases: Mapping[DatasetRole, tuple[GroundingDatasetCase, ...]]
    labels: Mapping[DatasetRole, Mapping[str, LabelReceipt]]

    @property
    def case_counts(self) -> dict[str, int]:
        return {role.value: len(self.cases.get(role, ())) for role in DatasetRole}


def validate_dataset_manifest(path: Path) -> ValidatedDataset:
    manifest_bytes = _read_plain_file(path, maximum=_MAX_SHARD_BYTES)
    manifest = GroundingDatasetManifest.model_validate_json(manifest_bytes)
    root = path.resolve(strict=True).parent
    cases: dict[DatasetRole, list[GroundingDatasetCase]] = {role: [] for role in DatasetRole}
    labels: dict[DatasetRole, dict[str, LabelReceipt]] = {role: {} for role in DatasetRole}

    for shard in manifest.shards:
        case_path = _bound_file(root, shard.cases_path, shard.cases_sha256)
        loaded_cases = _read_jsonl(case_path, GroundingDatasetCase)
        if len(loaded_cases) != shard.case_count:
            raise ValueError(f"case count mismatch for {shard.cases_path}")
        cases[shard.role].extend(loaded_cases)

        if shard.role is DatasetRole.QUALIFICATION:
            _validate_qualification_custody(manifest, shard)
            continue
        assert shard.labels_path is not None and shard.labels_sha256 is not None
        label_path = _bound_file(root, shard.labels_path, shard.labels_sha256)
        loaded_labels = _read_jsonl(label_path, LabelReceipt)
        for label in loaded_labels:
            if label.case_id in labels[shard.role]:
                raise ValueError(f"duplicate label for case {label.case_id}")
            labels[shard.role][label.case_id] = label

    _validate_cases_and_labels(manifest, cases, labels)
    _validate_role_isolation(manifest, cases)
    frozen_cases = MappingProxyType({role: tuple(values) for role, values in cases.items()})
    frozen_labels = MappingProxyType(
        {role: MappingProxyType(dict(values)) for role, values in labels.items()}
    )
    return ValidatedDataset(
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        cases=frozen_cases,
        labels=frozen_labels,
    )


def export_training_records(
    dataset: ValidatedDataset,
    role: DatasetRole,
    *,
    preference: bool = False,
) -> list[dict[str, object]]:
    if role is DatasetRole.QUALIFICATION:
        raise ValueError("qualification cases cannot be exported for training")
    records = []
    for item in dataset.cases[role]:
        label = dataset.labels[role][item.id]
        if preference and label.rejected_response is None:
            continue
        record: dict[str, object] = {
            "id": item.id,
            "role": role.value,
            "repository": item.repository,
            "repository_revision": item.repository_revision,
            "task_family": item.task_family,
            "prompt": render_grounding_prompt(item.grounding),
            "chosen": json.dumps(
                label.response.model_dump(mode="json"),
                separators=(",", ":"),
            ),
            "hard_negative": label.hard_negative,
            "case_sha256": item.grounding.sha256(),
            "label_evidence_sha256": label.evidence_sha256,
        }
        if preference:
            assert label.rejected_response is not None
            record["rejected"] = json.dumps(
                label.rejected_response.model_dump(mode="json"),
                separators=(",", ":"),
            )
        records.append(record)
    return records


def _validate_cases_and_labels(
    manifest: GroundingDatasetManifest,
    cases: dict[DatasetRole, list[GroundingDatasetCase]],
    labels: dict[DatasetRole, dict[str, LabelReceipt]],
) -> None:
    retired = RETIRED_STUDY_IDS | manifest.additional_retired_study_ids
    all_ids: set[str] = set()
    for role, values in cases.items():
        role_ids = {item.id for item in values}
        if len(role_ids) != len(values):
            raise ValueError(f"duplicate case ID within {role.value}")
        overlap = all_ids & role_ids
        if overlap:
            raise ValueError(f"case IDs overlap dataset roles: {sorted(overlap)}")
        all_ids.update(role_ids)
        for item in values:
            if item.origin_study_id in retired or item.grounding.origin_study_id in retired:
                raise ValueError(f"case {item.id} originates from a retired study")
            for evidence in item.grounding.evidence:
                if evidence.source and evidence.source.path.startswith(RETIRED_SOURCE_PREFIXES):
                    raise ValueError(f"case {item.id} cites retired holdout evidence")

        if role is DatasetRole.QUALIFICATION:
            if labels[role]:
                raise ValueError("qualification labels must remain sealed")
            continue
        if set(labels[role]) != role_ids:
            missing = sorted(role_ids - set(labels[role]))
            extra = sorted(set(labels[role]) - role_ids)
            raise ValueError(
                f"label set mismatch for {role.value}: missing={missing}, extra={extra}"
            )
        indexed = {item.id: item for item in values}
        for case_id, label in labels[role].items():
            item = indexed[case_id]
            if manifest.implementer in {label.author, label.reviewer}:
                raise ValueError(f"label for {case_id} is not independent of the implementer")
            if label.case_sha256 != item.grounding.sha256():
                raise ValueError(f"label for {case_id} is bound to a different case")
            decision = validate_grounding(item.grounding, label.response)
            if not decision.assessment_valid:
                raise ValueError(f"preferred response for {case_id} fails grounding validation")
            if (label.expected_result == "yes") != decision.authorizes_completion:
                raise ValueError(f"preferred response for {case_id} conflicts with its label")


def _validate_qualification_custody(
    manifest: GroundingDatasetManifest, shard: DatasetShard
) -> None:
    custody = shard.qualification_custody
    assert custody is not None
    if manifest.implementer in {
        custody.case_author,
        custody.label_custodian,
        custody.independent_reviewer,
    }:
        raise ValueError("qualification custody is not independent of the implementer")


def _validate_role_isolation(
    manifest: GroundingDatasetManifest,
    cases: dict[DatasetRole, list[GroundingDatasetCase]],
) -> None:
    repositories: dict[str, DatasetRole] = {}
    task_families: dict[str, DatasetRole] = {}
    fingerprints: dict[str, DatasetRole] = {}
    evidence_digests: dict[str, DatasetRole] = {}
    documents: list[tuple[DatasetRole, str, frozenset[str]]] = []

    for role, values in cases.items():
        for item in values:
            _claim_group(repositories, item.repository, role, "repository")
            _claim_group(task_families, item.task_family, role, "task family")
            fingerprint = _normalized_fingerprint(item)
            _claim_group(fingerprints, fingerprint, role, "normalized case")
            for evidence in item.grounding.evidence:
                _claim_group(
                    evidence_digests,
                    evidence.content_sha256,
                    role,
                    "evidence content",
                )
            documents.append((role, item.id, _shingles(_normalized_text(item))))

    for index, (left_role, left_id, left) in enumerate(documents):
        for right_role, right_id, right in documents[index + 1 :]:
            if left_role is right_role:
                continue
            similarity = _jaccard(left, right)
            if similarity >= manifest.near_duplicate_threshold:
                raise ValueError(
                    "near-duplicate cases cross dataset roles: "
                    f"{left_id}/{left_role.value} and {right_id}/{right_role.value} "
                    f"similarity={similarity:.3f}"
                )


def _claim_group(
    owners: dict[str, DatasetRole],
    key: str,
    role: DatasetRole,
    name: str,
) -> None:
    previous = owners.setdefault(key, role)
    if previous is not role:
        raise ValueError(f"{name} leaks across {previous.value} and {role.value}")


def _normalized_fingerprint(item: GroundingDatasetCase) -> str:
    return hashlib.sha256(_normalized_text(item).encode()).hexdigest()


def _normalized_text(item: GroundingDatasetCase) -> str:
    values = [item.grounding.criterion]
    values.extend(clause.text for clause in item.grounding.clauses)
    values.extend(evidence.content for evidence in item.grounding.evidence)
    return " ".join(re.findall(r"[a-z0-9]+", "\n".join(values).lower()))


def _shingles(value: str, width: int = 5) -> frozenset[str]:
    tokens = value.split()
    if len(tokens) < width:
        return frozenset(tokens)
    return frozenset(
        " ".join(tokens[index : index + width]) for index in range(len(tokens) - width + 1)
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def _bound_file(root: Path, relative: str, expected_sha256: str) -> Path:
    path = root / relative
    data = _read_plain_file(path, maximum=_MAX_SHARD_BYTES)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError(f"dataset shard escapes manifest directory: {relative}")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"dataset shard SHA-256 mismatch: {relative}")
    return resolved


def _read_plain_file(path: Path, *, maximum: int) -> bytes:
    try:
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError(f"dataset files cannot use symbolic links: {path}")
        size = path.stat().st_size
        if size > maximum:
            raise ValueError(f"dataset file exceeds {maximum} bytes: {path}")
        data = path.read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read dataset file: {path}") from error
    if len(data) != size:
        raise ValueError(f"dataset file changed while reading: {path}")
    return data


def _read_jsonl(path: Path, model: type[Config]) -> list:
    values = []
    data = _read_plain_file(path, maximum=_MAX_SHARD_BYTES)
    for number, line in enumerate(data.splitlines(), 1):
        if not line.strip():
            continue
        if len(line) > _MAX_LINE_BYTES:
            raise ValueError(f"JSONL line {number} is too large in {path}")
        try:
            values.append(model.model_validate_json(line))
        except ValueError as error:
            raise ValueError(f"invalid JSONL line {number} in {path}: {error}") from error
    if not values:
        raise ValueError(f"dataset shard is empty: {path}")
    return values
