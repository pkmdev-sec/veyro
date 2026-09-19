# Product charter: Foreman control plane

## Document control

| Field | Value |
| --- | --- |
| Charter ID | `GOV-001` |
| Version | `0.1` |
| Status | Proposed; Gate G0 approval pending |
| Owner | Foreman maintainers |
| Approval authority | Maintainers of the pilot repositories and the Foreman product owner |
| Applies to | The initial control-plane releases through verified, release-ready change sets |

Changes to the problem, users, product boundary, or mandatory outcomes require a reviewed charter
amendment. The approval record at the end of this document is the authoritative record for Gate G0;
a merged draft alone is not approval.

## Problem

Software agents can produce changes, but invoking several agents is not the same as controlling the
quality of a software change. Requests are often ambiguous, evidence is fragmented across agent
sessions and repositories, and a successful process exit says little about acceptance criteria,
risk, regressions, or release readiness. Cross-repository effects and the reason behind supervisory
decisions can also be lost.

Foreman needs to turn a structured request into an auditable, quality-controlled change set. It must
coordinate heterogeneous agents without trusting any one agent to define success, preserve the
source evidence behind decisions, enforce deterministic checks and approvals, and stop short of
autonomous production deployment.

## Product vision

Foreman is an agent-neutral control plane for software change management. Given an approved task
contract, it plans and supervises work in isolated worktrees, gathers evidence from coding agents
and existing engineering tools, applies deterministic policy, and produces a verified,
release-ready change set for human-controlled release.

The product is successful when a reviewer can answer all of these questions from retained evidence:

1. What change was authorized, and which acceptance criteria governed it?
2. Which repositories, components, and contracts were affected, and why?
3. Which agents and tools acted, under which versions and policies?
4. Which deterministic checks ran, and what were their unaltered results?
5. Which model assessments or human decisions influenced the run?
6. What remains unverified, risky, or subject to approval?

This is quality-controlled software change management, not a multi-agent command launcher.

## Intended users

| User | Need from Foreman |
| --- | --- |
| Change requester | Turn a ticket or specification into explicit, traceable acceptance criteria and receive a clear outcome. |
| Repository maintainer | Review a bounded change, impact plan, deterministic evidence, and unresolved risks without reconstructing agent history. |
| Engineering or release lead | Apply approval and release-readiness policy consistently across repositories and teams. |
| Platform operator | Configure agents, repositories, checks, credentials, limits, retention, and observability without coupling the system to one vendor. |
| Agent-adapter developer | Integrate an agent through a versioned capability and event contract with compatibility fixtures. |
| Security, compliance, or audit reviewer | Trace actions and decisions to immutable raw evidence, provenance, policy, and human approvals. |

Foreman does not replace the accountable maintainer, security approver, release owner, or incident
owner.

## Supported operating environments

The initial product targets local developer workstations and self-hosted CI or automation runners
that provide:

- Git repositories with isolated worktree support;
- a protected integration branch that workers cannot write directly;
- machine-readable access to configured build, test, lint, type, security, and contract checks;
- explicit filesystem, command, environment, credential, and network policy; and
- at least one supported agent adapter.

The architecture must support Prime Agent, Claude Code, OpenCode, Orvek, and Codex as peers. An
installation may enable a subset, but no one adapter may define the domain schema or lifecycle. The
exact supported agent versions, operating systems, machine architectures, and protocol capabilities
are discovery outputs and must be published before an adapter is declared supported.

LocalJev is the initial semantic supervisor and runs locally by default. Its assessments are
advisory until representative calibration demonstrates otherwise. Foreman must continue to expose
the underlying evidence and deterministic result when LocalJev or any other model is unavailable.

The unit of delivery is a change set that is ready for an existing human-controlled release
process. Foreman is not a production deployment environment.

## Mandatory outcomes

An initial release must provide these outcomes:

1. **Contract before implementation.** Work does not enter implementation without a versioned task
   contract containing identifiable acceptance criteria, scope, constraints, and approval state.
2. **Agent-neutral execution.** Selection and replacement of an agent happen through explicit,
   versioned adapter capabilities rather than vendor-specific assumptions in core policy.
3. **Isolated work.** Workers modify isolated worktrees and cannot write directly to protected
   branches.
4. **Evidence-preserving supervision.** Raw vendor events are retained alongside normalized events;
   normalization never destroys the source evidence.
5. **Deterministic quality authority.** Existing tests, builds, linters, type checkers, security
   scanners, contract checks, and policy gates remain authoritative. A model cannot turn a failed
   hard check into a pass.
6. **Risk-based human control.** Security, authentication, billing, cryptography, infrastructure,
   data migration, and other prohibited-for-autonomy work requires explicit human approval.
7. **Cross-repository awareness.** Dependency and impact edges record provenance, confidence, and
   freshness instead of presenting inference as fact.
8. **Auditable decisions.** Runs identify the task, repository, adapter, evidence, checks,
   assessments, policy decisions, interventions, approvals, and failure category.
9. **Bounded integration.** Only reviewed, policy-compliant changes with passing required checks can
   become release-ready; failed, cancelled, or unresolved work remains distinguishable.
10. **Honest uncertainty.** Unsupported capabilities, stale evidence, ambiguity, partial
    verification, and supervisory uncertainty are visible and may force escalation.

Detailed service-level targets and quality measures belong to `GOV-006`; this charter establishes
which outcomes those metrics must evaluate.

## Product scope

### In scope for initial releases

- intake and validation of structured task contracts;
- workspace and multi-repository manifests;
- discovery, impact planning, risk classification, and approval gates;
- adapter-based execution, continuation, steering, cancellation, and recovery;
- isolated worktree creation and protected-path enforcement;
- preservation and normalization of agent events with provenance;
- orchestration of existing deterministic engineering checks;
- LocalJev assessment as one advisory signal to deterministic policy;
- evidence bundles, policy decisions, audit history, and run observability;
- verification, bounded integration, and generation of a release-ready change set; and
- local and self-hosted operation suitable for representative pilots.

### Explicit exclusions

Initial releases do not:

- deploy to production or bypass an existing release process;
- replace CI, test, build, lint, type, security, or contract-checking systems;
- allow a model to waive a hard-check failure or mandatory approval;
- infer every runtime dependency from imports or claim inferred edges as observed facts;
- send whole large repositories to a model by default;
- treat a second model or agent as inherently independent verification;
- support an agent by scraping unstable human-oriented terminal output without a versioned
  compatibility contract;
- optimize for fewer lines, fewer files, or one global quality score at the expense of behavior;
- autonomously approve high-risk or prohibited-for-autonomy changes; or
- guarantee defect-free code, complete dependency discovery, or calibrated model probabilities.

## Product principles and authority boundaries

- **Evidence before assertion:** every material status and decision links to its source.
- **Hard checks outrank judgment:** deterministic failures remain failures until the authoritative
  check passes or an authorized human changes the governing contract or policy.
- **Policy owns control:** models estimate semantic state; versioned deterministic policy decides
  legal transitions and interventions.
- **Least privilege:** each worker receives only the repository, credentials, commands, and network
  access required by the approved task.
- **Human accountability:** required approvals identify their scope, approver, time, and evidence.
- **No silent degradation:** unavailable checks, malformed events, timeouts, failed cancellation,
  unsupported adapter capabilities, and stale dependency data produce explicit states.
- **Reproducible records:** schemas, policy, adapters, models, configurations, and persisted events
  carry versions sufficient to interpret a historical run.
- **Quality over compactness:** duplication, complexity, and dependency growth are evaluated in
  context and are not collapsed into a line-count objective.

A requester defines the desired result. Maintainers own repository acceptance. Security and other
designated approvers own restricted-risk approvals. Deterministic tools own their reported result.
Policy owns legal lifecycle transitions. Models advise; they do not acquire any of those authorities.

## Product-level workflow

1. Capture the request as a draft task contract and reject or escalate material ambiguity.
2. Discover the workspace, applicable policies, dependency evidence, and adapter capabilities.
3. Produce an impact plan, risk classification, required checks, and approval plan.
4. Obtain required approval before implementation begins.
5. Execute approved work through adapters in isolated worktrees while retaining raw events.
6. Run required deterministic checks and gather a versioned evidence bundle.
7. Use advisory assessment and deterministic policy to continue, intervene, retry, verify, fail, or
   escalate without overriding hard evidence.
8. Integrate only eligible changes and emit a release-ready change set with residual risks and
   approval history.
9. Leave deployment and final production authorization to the existing release owner and process.

Cancellation, timeout, malformed input, unavailable agents, and failed verification are normal,
observable outcomes; none may be represented as successful completion.

## Constraints and assumptions

- Pilot repositories expose deterministic checks that Foreman can invoke and whose result it can
  preserve.
- Repository owners define protected branches, paths, release policy, and authoritative checks.
- Credentials are supplied by the operating environment and are neither persisted in task records
  nor included in model observations.
- Raw events can contain private source content and therefore follow an explicit retention and
  deletion policy before real repository use.
- Cross-repository discovery is necessarily incomplete; freshness and confidence are part of the
  data model and user presentation.
- Backward compatibility with the current Foreman experiment requires an explicit migration path;
  this charter by itself changes no runtime behavior or configuration.

## Initial release completion condition

The initial program is ready to leave pilot status only when representative new-project,
mature-single-repository, and multi-service changes demonstrate that Foreman can enforce the
mandatory outcomes above; required deterministic checks pass; mandatory approvals are traceable;
failure and cancellation remain safe; and known limitations are documented. Production deployment
remains outside that completion condition.

## Related governance work

This charter intentionally does not pre-empt the detailed artifacts that follow it:

- `GOV-002` owns canonical terminology.
- `GOV-003` owns lifecycle states and transitions.
- `GOV-004` and `GOV-005` own risk classification and approvals.
- `GOV-006` owns metric formulas, sources, owners, and reporting intervals.
- `GOV-007` and `GOV-008` own data and agent/model usage policy.
- `GOV-009` owns architecture-decision records.
- `GOV-010` owns pilot selection.

If one of those artifacts conflicts with this charter, the conflict must be reviewed as a charter
amendment rather than resolved implicitly in implementation.

## Gate G0 review record

Reviewers should explicitly answer **yes** to both acceptance questions:

1. Does this charter define quality-controlled software change management rather than merely
   multi-agent command execution?
2. Does it state the problem, intended users, supported environments, desired outcomes, and explicit
   exclusions clearly enough to constrain subsequent design?

| Role | Reviewer | Decision | Date | Notes or evidence |
| --- | --- | --- | --- | --- |
| Foreman product owner | Pending | Pending | — | — |
| Pilot repository maintainer(s) | Pending | Pending | — | — |

`GOV-001` remains in progress until these decisions are recorded. Gate G0 additionally requires the
other governance artifacts listed in the implementation backlog.
