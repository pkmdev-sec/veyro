"""Assess synthetic failed-test metadata with localjev; never connect to an agent."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from pydantic import TypeAdapter

from veyro.models import SupervisionEvent
from veyro.supervision.checkpoints import (
    AUTHORITATIVE_MODEL_CHECKPOINT,
    AUTHORITATIVE_PROVIDER_ID,
    CHECKPOINT_QUESTIONS,
    CheckpointSelector,
)
from veyro.supervision.reducer import SessionReducer
from veyro.supervision.supervisor import authoritative_assessments
from veyro.veyro.base import VeyroModelError


async def run_example(*, live: bool) -> dict[str, object]:
    fixture = Path(__file__).with_name("failed-verification.json")
    events = TypeAdapter(list[SupervisionEvent]).validate_json(fixture.read_bytes())
    reducer = SessionReducer(events[0].session)
    for event in events:
        state = reducer.apply(event)
    checkpoint = CheckpointSelector().select(event, state)
    assert checkpoint is not None
    result: dict[str, object] = {
        "mode": "live" if live else "preview",
        "provider_id": AUTHORITATIVE_PROVIDER_ID,
        "configured_checkpoint": AUTHORITATIVE_MODEL_CHECKPOINT,
        "checkpoint_kind": checkpoint.kind.value,
        "questions": list(CHECKPOINT_QUESTIONS),
        "synthetic_input": True,
        "controls_enabled": False,
        "per_response_weight_attestation": False,
    }
    if not live:
        return result
    service = authoritative_assessments()
    try:
        assessment = await service.assess_if_needed(event, state)
        assert assessment is not None
        result["assessment"] = assessment.model_dump(mode="json")
    finally:
        await service.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="send the fixture to localjev")
    args = parser.parse_args()
    try:
        result = asyncio.run(run_example(live=args.live))
    except VeyroModelError:
        parser.exit(
            1, "Assessment failed. Check localjev readiness and the Qwen3-14B deployment.\n"
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
