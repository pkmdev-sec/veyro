# Historical Prime Agent supervision canary

For current procedures and recovery, use the [operator guide](supervision-operator-guide.md).
For current read-only checks, use the [verification guide](supervision-verification.md).

This document records the intended approved-stop canary. It is not runnable as
current verification. The intended path created a disposable Prime Agent session,
classified a stop as `review_required`, assessed it with LocalJev, bound approval
to the exact request, dispatched the stop, and required a terminal native event.

Current HEAD does not complete that path. `veyro.supervision.prime_canary`
constructs a `CheckpointAssessmentService`, but `SupervisionControlLoop.run_control()`
does not consume the injected service. The stop therefore returns
`semantic_evidence_required` before approval or delivery. The separate
`veyro.supervision.rollout_canary` uses public `veyro supervise`, which constructs
no assessor and fails at the same gate.

Do not run either module as qualification evidence. The previously recorded
`control: executed` result describes an earlier implementation and does not prove
that current HEAD can assess, authorize, or deliver an approved stop. Current
Prime verification is limited to read-only discovery, attachment, and
observe-only evaluation that leaves the native session intact.
