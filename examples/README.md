# Assess failed verification with localjev

This example runs Veyro's actual supervision checkpoint path with Qwen3-14B.
It uses [failed-verification.json](failed-verification.json), a synthetic session
start followed by a failed `unit-tests` check. There are no repository contents,
credentials, transcripts, or real native-session identifiers in the fixture.

## Preview without services

Install Veyro using the [project instructions](../README.md). From the checkout:

```sh
.venv/bin/python examples/assess_localjev.py
```

The output identifies `failed_verification`, provider `localjev-qwen3-14b`, the
configured Qwen3 checkpoint, and seven questions. It sets `synthetic_input: true`
and `controls_enabled: false`. Preview mode makes no model request and invents no scores.

## Ask the local model

First complete the [localjev deployment checks](../docs/localjev.md). Then run:

```sh
.venv/bin/python examples/assess_localjev.py --live
```

[assess_localjev.py](assess_localjev.py) validates the events, reduces session
state, selects the checkpoint, and calls the same pinned assessor used by
`veyro supervise`. localjev requests Qwen3-14B through Ollama. The script never
creates a provider bridge, asks for approval, or dispatches a native control.

Read these fields in `assessment`:

| Score | Question |
| --- | --- |
| `meaningful_progress` | Does the available evidence show progress? |
| `work_stuck` | Is the work stuck or looping? |
| `work_off_track` | Is the work drifting off task? |
| `verification_sufficient` | Is there enough successful verification? |
| `completion_supported` | Does the evidence support completion? |
| `safe_to_continue` | Can the session continue without immediate human intervention? |
| `needs_human` | Does the checkpoint require human judgment or approval? |

`assessment.provenance` records the endpoint, model alias, configured checkpoint,
question version, and measured client latency. `jev-latest` is the API alias;
`qwen3:14b` is the configured underlying model. Neither field is weight attestation.

A live check of this fixture returned `verification_sufficient: 0.0`,
`completion_supported: 0.0`, and `needs_human: 1.0`. These are observations of one
run, not golden outputs or calibrated thresholds. Repeated model responses can differ.
The model cannot infer a task's real status from two synthetic events.

## Failure behavior

If localjev is unavailable or returns invalid evidence, the script exits nonzero.
It does not select another model or print fabricated assessments. Follow the
[deployment guide](../docs/localjev.md), not a retry that changes model identity.

For actual sessions, use the [operator guide](../docs/supervision-operator-guide.md).
This sample is a model integration example, not evidence authorizing a real action.
