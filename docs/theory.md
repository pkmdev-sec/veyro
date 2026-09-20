# Semantic assessment and deterministic control

Veyro separates native execution from local semantic assessment. A coding agent
keeps its tools, terminal, and permission system. localjev uses Qwen3-14B to assess
structured evidence at review checkpoints. Veyro, not the model, authorizes controls.

## Model sizes and release status

**Qwen3 14B** is the pinned assessor in the released path below. **Qwen3 4B Instruct**
is a lower-memory development profile; it is not merged into `main` and has no edge in
this control flow. The separate local GGUF readout experiments are unmerged for both
sizes. See the [model comparison](qwen-models.md) for tags, use cases, and limits.

## Existing-session data flow

```text
Native provider events
    -> version-pinned bridge
    -> metadata normalization
    -> SessionReducer
    -> deterministic policy and capability checks
    -> localjev / Qwen3-14B when review is needed
    -> exact approval and fresh-state checks
    -> durable no-retry claim
    -> recheck, then supported native control
```

`SessionReducer` maintains bounded typed state rather than a transcript. Events
have contiguous local sequence numbers; missing native history remains unknown.
`CheckpointSelector` recognizes failed verification, completion claims, idle
sessions, and risky actions. The current CLI evaluates one operator-supplied
proposal. It is not a background proposal generator or a judge invoked on every event.

## The model estimates; policy authorizes

`LocalJevCheckpointAssessor` validates the configured assessor identity and asks
seven named questions about reduced state. Scores express uncertainty. They do
not establish task completion or replace evidence from the native provider.

`SupervisionControlLoop` applies rollout policy. Observe-only neither assesses nor
delivers. Advisory may assess but cannot deliver. Executing modes require the
necessary capabilities, current evidence, and exact approval. Forbidden actions
and explicit human denial cannot be overridden by a high score.

`AuthorizedControlDispatcher` reserves a durable delivery claim before native
execution. It rechecks approval expiry and the observed cursor after persistence.
The claim survives failure, cancellation, and uncertainty. This prevents duplicate
dispatch in the same scope; it does not provide exactly-once native execution.
The provider does not offer an atomic compare-and-execute transaction.

## Native boundaries remain visible

Queue acceptance is not model execution. Interrupt is not session stop. Deletion
is not a substitute for stop. An unsupported capability stays unsupported rather
than being approximated through terminal scraping or another native operation.

No currently pinned adapter qualifies for automatic delivery. Native tools can
still act outside Veyro, subject to their own permissions. The
[operator guide](supervision-operator-guide.md) describes this boundary and recovery.

## The separate factory loop

`veyro run` starts workers and assesses their progress while they execute.
`FactoryRuntime` coalesces worker events, `ObservationBuilder` gathers bounded
state, and `FactoryPolicy` interprets nine model scores with worker/retry limits.
This loop can persist task text and agent output. It does not have the same
approval protocol or privacy contract as existing-session supervision.

See [factory runtime](runtime.md) for that implementation. Do not infer that its
simulation results prove live native controls or calibrated Qwen3-14B decisions.
