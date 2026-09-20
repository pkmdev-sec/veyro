# Qualified-supervision release scope and checks

> Historical record: this page describes commit `c1a21d4`, the qualified-supervision
> subset merged in PR #1. The current tree also contains the autonomous task and local
> evaluation implementation. Those additions remain unqualified while G4 and G6 are open.

## Included interfaces

That release candidate covered the existing-session control plane and its documentation:

- `sessions` and `attach` read native session metadata. They do not call a model or deliver controls.
- `supervise` reviews one operator proposal. Observe-only is the default. Supported controls
  require policy checks, exact human approval, a durable claim, and a freshness check.
- `agents` and `agent` discover or launch installed native agents. The sidecar records
  lifecycle observations; it is not an autonomous task-completion loop.

The existing factory commands remain unchanged. The factory has a separate privacy
contract and can retain task text and worker output. Its App Server steering and jeff
shadow options remain experimental, disabled by default, and outside this qualification.
See the [factory reference](runtime.md) and [capability matrix](supervision-reference.md).

## Model scope

In the scoped release, **Qwen3 14B** was the localjev assessor. **Qwen3 4B Instruct**
was not included and was not an alternate assessor in that release. The
[model comparison](qwen-models.md) explains their roles and the separate readout path.

## Excluded additions

The following development additions were not in that release tree or its packages:

| Addition | Reason for exclusion |
| --- | --- |
| Native autonomous plan/build/repair loop and optional completion judge | Native canaries have not passed across both CLIs and both local profiles |
| Persistent local GGUF readout workers and model profiles | Part of the unqualified local autonomy/evaluation stack |
| Typed evaluators, correction examples, and temperature calibration | No independent workflow-labelled calibration holdout; development calibration can worsen results |
| Prompt experiments, canary outputs, and development gate ledger | Evidence for the excluded additions, not qualification of this release |

These exclusions do not turn failed gates into passing gates. The development work is
preserved separately. No unattended completion or calibrated-accuracy claim is made.

## Verification

To reproduce that release, check out commit `c1a21d4` and run:

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests tools examples
.venv/bin/python tools/generate_header_logo.py --check
uv run --script tools/generate_supervision_diagram.py --check
uv build --offline --out-dir dist
.venv/bin/python tools/check_release.py dist/veyro_factory-0.4.0-py3-none-any.whl dist/veyro_factory-0.4.0.tar.gz
```

The diagram check covers both the animation and its still image. The package check
requires their source assets and the release scope document. `--offline` requires
cached build dependencies; omit it when downloads are needed.

## Deployment limits

Passing these checks establishes the local code and package contracts. It does not
qualify every installed provider version, every supported operation, or an unattended
production deployment. Recorded native checks cover observation, one disposable approved
Prime stop, and Codex hook delivery; broader control coverage remains limited.

No pinned adapter qualifies for automatic delivery. Keep observe-only as the default,
use the documented pinned provider versions, and verify your own endpoints, credentials,
retention policy, and supported control before enabling it. Model estimates are not
calibrated guarantees. Start with the [operator guide](supervision-operator-guide.md)
and [recorded evidence](supervision-verification.md).
