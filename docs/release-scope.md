# Current release scope and artifact checks

This page defines the interfaces shipped by the current package. The machine-readable equivalent is
[`release-status.json`](../release-status.json). Passing artifact checks does not qualify model
accuracy, autonomous completion, or automatic delivery.

## Supported interfaces

The following deterministic operator commands are supported:

| Command | Contract |
| --- | --- |
| `agents` | Report installed providers and their versioned machine interfaces. |
| `sessions` | Discover bounded existing-session metadata without starting or resuming a provider. |
| `attach` | Observe bounded session metadata without sending controls. |
| `supervise` | Evaluate one proposal under policy, exact approval, a durable no-retry claim, and fresh evidence. |

"Supported" means implemented and packaged. It does not mean that every provider version or
operation is qualified. No pinned adapter qualifies for automatic delivery.

## Experimental interfaces

The package ships the following interfaces for explicit local experiments:

| Command or group | Boundary |
| --- | --- |
| `agent` | Runs a pinned local autonomous OpenCode task. It requires `--autonomous` and `--coding-profile`. G4 remains open. |
| `local` | Inspects and runs pinned local model workers without changing global service configuration. |
| `evaluator` | Runs typed local evaluation, correction, and calibration experiments. G6 remains open. |
| `selene` | Runs deterministic grounding validation and offline research, training, quantization, and shadow workflows. Model output stays non-authoritative. |
| `import-jeff-weights` | Installs one pinned optional shadow weight from an explicit source. It does not enable control authority. |

Executable checks, native permissions, policy, and explicit human approval remain authoritative.
Experimental model output cannot close G4 or G6.

## Unavailable interfaces

The top-level `run`, `demo`, `inspect`, and `runs` commands were removed with no compatibility
aliases or deprecated paths. `run` could not enforce pinned loopback inference. The other three
were legacy test and diagnostic scaffolding, not supported product interfaces. `FactoryRuntime`
remains an internal experimental library implementation for tests and historical investigation;
the root package does not export it.

## Artifact qualification

Build and qualify the wheel and source archive, and preserve the JSON receipt:

```sh
uv build --offline --out-dir dist
.venv/bin/python tools/check_release.py \
  dist/veyro_factory-0.4.0-py3-none-any.whl dist/veyro_factory-0.4.0.tar.gz \
  > dist/release-receipt.json
```

The checker validates these artifact properties:

- safe wheel member names, regular sdist members, and exclusion of private `.audit` content;
- wheel and sdist name, version, and console entry-point metadata;
- one canonical pure-Python `WHEEL` file for the `py3-none-any` tag;
- complete `RECORD` coverage, URL-safe SHA-256 hashes, and byte sizes for wheel members;
- a parseable `veyro/cli.py` that defines the console entry-point symbol without importing it;
- required gate, license, documentation, test, and release-tool inputs;
- every local link and anchor in the materialized sdist documentation;
- byte identity for packaged Python modules, integration modules, and installed JSON profiles; and
- a canonical manifest digest over every sdist path, byte length, and content digest.

The deterministic receipt binds the hashes of the supplied wheel and sdist and the complete source
manifest. It uses `authority: artifact_only` and `production_qualified: false`. A passing receipt
establishes only the checked artifact identity, structure, and wheel-to-sdist consistency. It does not
identify who produced the artifacts or bind them to a repository, commit, or build process. Such a
claim requires an independently trusted source outside this checker. The receipt grants no semantic,
model, provider, or deployment authority.

## Gate status

G4 remains open for strict-local autonomous task reliability. G6 remains open for independent
evaluator calibration and qualification. Historical negative and partial results remain evidence of
what happened; packaging does not convert them into current support claims.

## Historical qualified-supervision record

Commit `c1a21d4` and PR #1 recorded an earlier qualified-supervision subset. That package predated the
current autonomous task, local evaluator, and Selene surfaces. Its scope remains historical evidence,
not the taxonomy of the current package. Reproduce it only from that commit and interpret its recorded
results under the limits documented there.
