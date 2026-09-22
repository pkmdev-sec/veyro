# Internal experimental factory runtime

This reference describes the legacy `FactoryRuntime` library implementation. The top-level
`veyro run` command is unavailable. This runtime is separate from the supported existing-session
`supervise` command and cannot enforce pinned loopback inference for its native workers. Its
observations and logs can include task text, Git diffs, and worker output. See
[the control-plane architecture](theory.md) for metadata-only existing-session supervision.

## Components

```text
NativeCliWorker / Codex exec -> FactoryRuntime -> ObservationBuilder
                                  ^                    |
                                  |                    v
                            FactoryPolicy <- localjev / Qwen3-14B
                                  |
                                  v
                              RunStore
                       state.json + events.jsonl
```

- `FactoryRuntime` owns lifecycle state, the event queue, and worker tasks.
- `ObservationBuilder` gathers bounded worker, event, and Git evidence.
- `JevVeyroModel` owns TypeSafe SDK requests and score/provenance validation.
- `FactoryPolicy` applies deterministic thresholds and resource limits.
- `RunStore` atomically replaces state and appends events.
- `CodexAppServerWorker` is selected only with `VEYRO_CODEX_BACKEND=app-server`.
  That experimental backend adds active-turn steering; `exec` is not a fallback.

Library tests can exercise this runtime with simulation classes and no model service or agent
credentials. Those tests validate lifecycle mechanics, not a supported CLI or production behavior.

## One assessment cycle

1. Worker events enter the `asyncio.Queue`.
2. The watcher coalesces adjacent events and applies the minimum assessment interval.
3. Completion, failure, and stop events force an immediate cycle.
4. Repository evidence and current state form one bounded `FactoryObservation`.
5. localjev returns nine named model scores.
6. Veyro validates and normalizes the assessment.
7. `FactoryPolicy` selects an `Intervention` within lifecycle and resource limits.
8. Runtime applies the supported worker action and persists the result.

Veyro-generated events do not feed back into the queue. Periodic assessment can
continue while a worker is active. These mechanics differ from evaluating one
operator proposal through the existing-session control plane.

## Shutdown and steering

Workers have bounded execution and process-group cancellation. With the opt-in
App Server backend, shutdown first requests `turn/interrupt`; after the grace
period it can terminate the subprocess group. `exec` does not support live steering.
See [steering](steering.md) for the exact backend and failure behavior.

Final state and events are persisted before the model client closes. Local atomic
file replacement is not a claim of production durability across every filesystem,
hardware failure, or restore scenario.

## Source map

| File | Responsibility |
| --- | --- |
| `src/veyro/runtime.py` | Concurrency and lifecycle |
| `src/veyro/policy.py` | Decisions and ordering |
| `src/veyro/observation.py` | Evidence collection and size bounds |
| `src/veyro/veyro/jev.py` | localjev SDK integration |
| `src/veyro/workers/codex.py` | Default non-steerable Codex exec |
| `src/veyro/workers/codex_app_server.py` | Opt-in steerable App Server |

The factory's historical environment reference is [`.env.example`](../.env.example).
`VEYRO_JEV_*` configures this internal experiment. Public `veyro supervise` does
not construct an assessor or read these settings.
