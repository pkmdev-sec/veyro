<p align="center">
  <img src="docs/assets/veyro-relay-logo.svg" width="720" alt="Veyro wordmark beside a tiled aperture and amber checkpoint">
</p>

<h1 align="center">Veyro</h1>
<p align="center"><strong>Native coding agents. Local assessment. Explicit checks.</strong></p>
<p align="center">Keep the agent, terminal, tools, and permission system you already use.</p>
<p align="center">
  <a href="https://github.com/pkmdev-sec/veyro/actions/workflows/ci.yml"><img alt="CI status" src="https://github.com/pkmdev-sec/veyro/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://www.python.org/downloads/"><img alt="Python 3.11 or newer" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/License-MIT-93dec4"></a>
</p>
<p align="center">
  <a href="#quickstart">Quickstart</a> ·
  <a href="docs/supervision-operator-guide.md">Operator guide</a> ·
  <a href="docs/README.md">Documentation</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

Veyro observes Prime Agent, OpenCode, and Codex hook metadata. The public
existing-session `veyro supervise` command evaluates one proposal without calling
an assessor. Observe-only and advisory modes do not deliver controls, and
review-required proposals fail with `semantic_evidence_required` before approval.
Separate standalone examples and direct library callers can use
[localjev](https://github.com/githubnext/localjev) with Qwen3-14B. Veyro uses
structured events, not terminal scraping.

> [!IMPORTANT]
> Veyro is pre-release software. [`production_qualified`](release-status.json) is `false`.
> The supported package interfaces work as documented, but G4 autonomous-task reliability
> and G6 evaluator calibration remain open. [`GATES.md`](GATES.md) records the evidence.

## Quickstart

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). This first run does
not require Ollama, localjev, a model download, or a running coding agent.

```sh
git clone https://github.com/pkmdev-sec/veyro.git
cd veyro
uv sync --frozen --extra dev

# Inspect native agent interfaces installed on this machine.
uv run veyro agents

# Reduce a checked-in failed-verification fixture. No model call is made.
uv run python examples/assess_localjev.py

# Render typed evaluation questions without inference.
uv run veyro evaluator preview \
  examples/evaluator-definition.json examples/evaluator-case.json
```

The first command reports executable availability separately from version qualification.
The synthetic assessment prints seven named questions, pinned provider provenance, and
`controls_enabled: false`. Continue with the [example walkthrough](examples/README.md).
Deploy [localjev](docs/localjev.md) only for the standalone live assessment or a
direct library integration.

## What Veyro supports

| Command | Status | Purpose |
| --- | --- | --- |
| `veyro agents` | Supported | Inspect native providers, versions, interfaces, and declared capabilities. |
| `veyro sessions` | Supported | Discover bounded metadata for existing sessions without starting or resuming them. |
| `veyro attach` | Supported | Stream normalized, read-only metadata from one existing session. |
| `veyro supervise` | Supported | Evaluate one operator proposal through assessor-free policy and delivery gates. Observe-only and advisory never deliver; review-required proposals fail before approval. |
| `veyro agent` | Experimental | Run a pinned local OpenCode task with worker-visible checks and bounded repair. G4 is open. |
| `veyro local`, `veyro evaluator`, `veyro selene`, `veyro import-jeff-weights` | Experimental | Run local model, typed evaluation, calibration, import, and offline research workflows. G6 is open. |

Unavailable legacy commands: `veyro run`, `veyro demo`, `veyro inspect`, and `veyro runs`. They were removed without compatibility aliases.
[`release-status.json`](release-status.json) is the machine-readable interface taxonomy.
"Supported" means implemented, packaged, and covered by the release checks. It does not
mean that every provider operation or model result is production-qualified.

## Existing-session review

Observation is read-only. `supervise` handles one explicit proposal. It does not watch a
session and invent actions, and it never calls LocalJev or another assessor. Observe-only
and advisory evaluation sends no control. In an executing mode, a review-required
proposal stops at `semantic_evidence_required` before approval. Deterministically
permitted proposals still pass the applicable approval, freshness, capability, and
durable no-retry delivery gates.

The diagram below records the former assessor-injected design. It is a historical
artifact, not the current public `veyro supervise` flow.

<p align="center">
  <img src="docs/assets/veyro-supervision.gif" width="1120" alt="Historical existing-session design with a LocalJev assessment step; the current public supervise command does not use this path">
</p>

[Open the historical still diagram](docs/assets/veyro-supervision.png) if you prefer no animation.

The provider adapter declares what is technically available. Policy and current evidence
decide whether Veyro may use it.

| Provider | Pinned version | Existing-session boundary |
| --- | --- | --- |
| Prime Agent | `0.9.5` | Live daemon metadata and approved controls, with partial history. |
| OpenCode | `1.18.30` | Authenticated loopback HTTP/SSE metadata and approved controls, with no replay. |
| Codex | `0.154.0` | Local hook metadata only. This CLI exposes no Codex control channel. |

Use the [operator guide](docs/supervision-operator-guide.md) for connection commands,
private policy files, approval input, and recovery. The [capability reference](docs/supervision-reference.md)
separates adapter capability from authorization.

## The design beliefs

- **Native stays native.** Veyro does not replace the coding agent's terminal, tools, or permissions.
- **Checks come before opinion.** Executable evidence remains authoritative. Model output is advisory.
- **Approval is exact.** A human approval binds to one request digest and expires with changed evidence.
- **Uncertainty remains visible.** Missing history, unknown delivery, failed calibration, and negative studies do not become success.
- **Local is a boundary.** Assessment endpoints must resolve to loopback. Task logs can still contain source and command output, so use disposable checkouts for untrusted work.

Read [what Veyro proves](docs/what-veyro-proves.md) for the evidence boundary and
[the architecture](docs/theory.md) for the reducer, checkpoints, policy, and delivery flow.

## Experimental checked tasks

The local task path is separate from existing-session supervision. A recorded autonomous
example paired the pinned OpenCode `coder30` writer with optional Laya evaluation on a
pre-provisioned maintainer machine. This repository has no reproducible public Laya
installation or checkpoint procedure. In that example, executable checks remained
authoritative, failed evidence could return for bounded repair, and passing evidence
stopped at operator review.

<p align="center">
  <img src="docs/assets/veyro-task-readout.png" width="1120" alt="Recorded maintainer-machine task example from Qwen3 Coder 30B through checks and optional Laya evaluation to human review">
</p>

The writer and evaluator examples have separate jobs:

| Role | Pinned profile | Runtime | Authority |
| --- | --- | --- | --- |
| Standalone and library assessor | `localjev-qwen3-14b`, `qwen3:14b`, `Q4_K_M` | localjev at `127.0.0.1:8080`, request alias `jev-latest` | Scores bounded checkpoint questions only when a standalone example or direct library caller invokes it. The public `veyro supervise` command never does. |
| Recorded experimental writer | `coder30`, `qwen3-coder:30b` | OpenCode through loopback Ollama on a pre-provisioned maintainer machine | Plans, uses native tools, and edits within native permissions. |
| Recorded typed evaluator | `laya`, `convaiinnovations/laya/typed-decisions` | Pinned offline subprocess on that pre-provisioned machine; no reproducible public setup is documented | Scores declared evidence after checks; cannot edit or accept work. |

Veyro does not download models or silently change global provider configuration. See
[model roles](docs/qwen-models.md), [native tasks](docs/native-autonomy.md), and the
[local evaluation guide](docs/local-harness.md). A runnable rubric is available at
[`examples/native-judge.json`](examples/native-judge.json).

## Documentation

| Goal | Guide |
| --- | --- |
| Understand current support and open gates | [Release scope](docs/release-scope.md) and [GATES.md](GATES.md) |
| Prepare LocalJev for a standalone assessment | [localjev deployment](docs/localjev.md) |
| Observe or supervise an existing session | [Operator guide](docs/supervision-operator-guide.md) |
| Understand privacy, versions, and capabilities | [Supervision reference](docs/supervision-reference.md) |
| Try a pinned local coding task | [Native autonomy](docs/native-autonomy.md) |
| Build typed evaluations and calibration experiments | [Local harness](docs/local-harness.md) |
| Understand architecture and evidence limits | [Theory](docs/theory.md) and [what Veyro proves](docs/what-veyro-proves.md) |
| Browse every guide | [Documentation index](docs/README.md) |

## Develop and verify

```sh
uv run ruff check src tests tools examples
uv run pytest -q
uv run python tools/generate_header_logo.py --check
uv run --script tools/generate_supervision_diagram.py --check
rm -rf dist && uv build --out-dir dist
uv run python tools/check_release.py dist/*.whl dist/*.tar.gz
```

The release checker validates archive safety, metadata, public documentation, wheel structure,
and wheel-to-sdist source identity. Its receipt has artifact-only authority and keeps
`production_qualified: false`.

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before opening a pull request. Use the issue forms
for reproducible bugs and focused feature requests. Report vulnerabilities privately through
[`SECURITY.md`](SECURITY.md).

[MIT license](LICENSE)
