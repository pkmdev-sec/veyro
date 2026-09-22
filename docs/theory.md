# Assessor-free public supervision and separate semantic assessment

Veyro keeps native execution with the coding agent and its own tools, terminal,
and permission system. The public existing-session `veyro supervise` command
evaluates one proposal without calling LocalJev or another assessor. LocalJev
assessment remains available to the standalone synthetic example, direct library
integrations, and the internal legacy factory runtime. Scores in those separate
paths never grant control authority.

## Model sizes and release status

**Qwen3 14B** is the configured LocalJev checkpoint for those explicit assessment
paths, not for the public existing-session command. A recorded strict-local
autonomous example on a pre-provisioned maintainer machine paired **Qwen3 Coder
30B** through OpenCode with optional **Laya typed-decisions** evaluation. This
repository has no reproducible public Laya installation or checkpoint procedure.
G4 native-task qualification and G6 calibration remain open. See the
[model-role guide](qwen-models.md) for tags, boundaries, and limits.

## Public existing-session data flow

```text
Native provider events
    -> version-pinned bridge
    -> metadata normalization
    -> SessionReducer
    -> deterministic boundary, policy, and capability checks
    -> no assessor
        -> observe-only or advisory: record non-delivery
        -> review-required in an executing mode:
             semantic_evidence_required before approval, then stop
        -> deterministically permitted in an executing mode:
             applicable approval and fresh-state checks
             -> durable no-retry claim
             -> recheck, then supported native control
```

`SessionReducer` maintains bounded typed state rather than a transcript. Events
have contiguous local sequence numbers; missing native history remains unknown.
The current CLI evaluates one operator-supplied proposal. It is not a background
proposal generator or a judge invoked on every event. `CheckpointSelector` and
the semantic checkpoint schema remain available to the separate assessment paths;
the public command does not invoke them.

## Models estimate; policy does not manufacture evidence

`LocalJevCheckpointAssessor` is a library component. The standalone example uses
it to score a checked-in synthetic fixture, and direct library callers can use it
to ask seven named questions about reduced state after validating the configured
identity. Its scores express uncertainty. They do not establish task completion
or replace evidence from the native provider. The historical Prime canary still
constructs this component, but the current control loop does not consume it.

The public command constructs `SupervisionControlLoop` without an assessment
service. Observe-only and advisory modes neither assess nor deliver. In
`approval_required` and `automatic` modes, a review-required proposal fails with
`semantic_evidence_required` before human approval. No policy setting supplies
semantic evidence or connects LocalJev. Deterministically permitted proposals
still pass the applicable capability, approval, freshness, and delivery gates.
Forbidden actions and explicit human denial cannot be overridden by a model score.

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

The top-level `run` command is unavailable because its workers cannot enforce pinned
loopback-only inference. The retained internal factory architecture historically started workers
and assessed their progress while they executed.
`FactoryRuntime` coalesces worker events, `ObservationBuilder` gathers bounded
state, and `FactoryPolicy` interprets nine model scores with worker/retry limits.
This loop can persist task text and agent output. It does not have the same
approval protocol or privacy contract as existing-session supervision.

See [factory runtime](runtime.md) for that implementation. Do not infer that its
simulation results prove live native controls or calibrated Qwen3-14B decisions.
