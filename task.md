# Agent-Neutral Software Factory Control Plane — Implementation Tasks

This document turns the revised plan into an ordered implementation backlog. It is intentionally
more detailed than a roadmap: every item must produce a reviewable artifact, test, or decision.
Tasks may only be marked complete when their acceptance criteria are met.

## Status legend

- `[ ]` not started
- `[~]` in progress
- `[x]` complete
- `[!]` blocked; add the blocker and owner next to the task
- `Gate` means later work must not begin until the gate is explicitly passed

## Guiding constraints

1. The system must support Prime Agent, Claude Code, OpenCode, Orvek, and Codex without making any
   one of them the architectural default.
2. Deterministic checks outrank model judgments. A model may never turn a failing hard check into a
   pass.
3. LocalJev begins as an advisory semantic supervisor. Its probabilities are not treated as
   calibrated until measured on representative internal tasks.
4. Workers operate in isolated worktrees and never write directly to a protected branch.
5. A structured task contract is required before implementation begins.
6. Cross-repository dependency edges must carry provenance, confidence, and freshness.
7. Raw agent events are retained; normalization must not destroy vendor-specific evidence.
8. Production deployment is out of scope for initial releases. The system produces a verified,
   release-ready change set.
9. Security-, authentication-, billing-, cryptography-, infrastructure-, and data-migration changes
   require explicit human approval until separately qualified.
10. Compactness is not itself quality. The system must not optimize blindly for fewer lines.

## Initial non-goals

- Fully autonomous production deployment.
- Replacing existing CI, test, build, lint, security, or contract-checking systems.
- Feeding entire large repositories into a language model.
- Assuming that a second model or agent is automatically an independent verifier.
- Inferring every runtime dependency from source imports.
- Supporting an agent by scraping unstable human-oriented terminal output without versioned
  compatibility tests.

## Definition of done for every implementation task

Unless a task explicitly produces only research or an ADR, it is complete only when:

- behavior has unit tests and relevant integration tests;
- failure, cancellation, timeout, and malformed-input paths are covered;
- user-facing behavior and configuration are documented;
- logs and fixtures contain no credentials or private source content;
- schemas and persisted data have explicit versions;
- observability identifies the task, run, repository, agent adapter, and failure category;
- formatting, linting, type checking, and repository tests pass;
- the change does not introduce unexplained dependencies, files, duplication, or complexity;
- backward compatibility or a migration path is documented;
- acceptance criteria below are demonstrably satisfied.

---

# 0. Existing prerequisites

- [x] **BASE-001 — Install and host LocalJev locally.**
  - LocalJev runs on `127.0.0.1:8080` as a persistent user service.
  - Acceptance: `/health` and `/ready` return successfully.
- [x] **BASE-002 — Install a local decision model.**
  - Ollama hosts `qwen3:14b` on `127.0.0.1:11434`.
  - Acceptance: LocalJev reports `qwen3:14b` as its ready upstream model.
- [x] **BASE-003 — Integrate Veyro with LocalJev through the TypeSafe SDK.**
  - Veyro uses `TYPESAFE_BASE_URL=http://127.0.0.1:8080` and `jev-latest`.
  - Acceptance: a live `JevVeyroModel` call returns all nine bounded assessment values.
- [x] **BASE-004 — Establish the Veyro repository baseline.**
  - Acceptance: all 75 tests and Ruff checks pass before control-plane work begins.

---

# 1. Program charter, terminology, and governance

- [~] **GOV-001 — Write the product charter.**
  - Define the problem, intended users, supported environments, desired outcomes, and explicit
    exclusions.
  - Artifact: [`docs/product-charter.md`](docs/product-charter.md); reviewer approval is pending.
  - Acceptance: reviewers agree that the goal is quality-controlled software change management,
    not merely multi-agent command execution.
- [ ] **GOV-002 — Define canonical terminology.**
  - Define task, task contract, workspace, project, repository, component, contract, agent,
    adapter, session, run, evidence, check, assessment, decision, intervention, impact plan,
    change set, verification, approval, and release readiness.
  - Acceptance: schemas and documentation use one term for each concept.
- [ ] **GOV-003 — Define the lifecycle state machine.**
  - Required states: `DRAFT`, `DISCOVERING`, `PLANNING`, `AWAITING_APPROVAL`, `IMPLEMENTING`,
    `VERIFYING`, `INTEGRATING`, `RELEASE_READY`, `FINISHED`, `FAILED`, `CANCELLED`, `ESCALATED`.
  - Define valid transitions and terminal-state rules.
  - Acceptance: invalid transitions are enumerated and testable.
- [ ] **GOV-004 — Define risk levels.**
  - At minimum: low, medium, high, and prohibited-for-autonomy.
  - Include criteria for security, privacy, billing, data loss, schema migration, infrastructure,
    customer impact, and rollback difficulty.
  - Acceptance: the same example change receives the same classification from deterministic rules.
- [ ] **GOV-005 — Define the approval matrix.**
  - Map risk levels and change types to required planning, implementation, verification, and release
    approvals.
  - Acceptance: no high-risk path can reach `RELEASE_READY` without the configured approval.
- [ ] **GOV-006 — Define system success metrics.**
  - Include escaped defects, rollbacks, incident correlation, review rounds, rework, lead time,
    duplicated code, complexity delta, unnecessary dependencies, test sufficiency, human override
    rate, LocalJev false positives/negatives, and agent failure rate.
  - Acceptance: every metric has a source, formula, owner, and reporting interval.
- [ ] **GOV-007 — Define data retention and deletion requirements.**
  - Classify raw transcripts, source excerpts, diffs, credentials, model inputs, assessments, and
    audit events.
  - Acceptance: each class has a retention period and deletion mechanism.
- [ ] **GOV-008 — Define model and agent usage policy.**
  - Record which repositories may be sent to remote agents and which must remain local.
  - Acceptance: policy can block an incompatible route before an agent starts.
- [ ] **GOV-009 — Create ADR templates and decision ownership.**
  - Required fields: context, alternatives, decision, consequences, rollback, evidence, reviewers.
  - Acceptance: all architecture gates below reference an ADR.
- [ ] **GOV-010 — Select representative pilots.**
  - Select at least one new project, one mature single repository, and one multi-service change.
  - Avoid production-critical first pilots.
  - Acceptance: each pilot has maintainers, historical data, deterministic checks, and measurable
    acceptance criteria.

## Gate G0 — Charter approved

- [ ] The charter, terminology, risk model, approvals, metrics, retention policy, and pilots are
  reviewed before schemas or orchestration behavior are finalized.

---

# 2. Discovery and baseline research

- [ ] **DISC-001 — Inventory supported agent installations.**
  - Record installed versions, executable paths, authentication mode, update channel, license, and
    supported machine architectures for Prime Agent, Claude Code, OpenCode, Orvek, and Codex.
  - Do not record tokens or session secrets.
- [ ] **DISC-002 — Probe Prime Agent interfaces.**
  - Identify machine-readable start, stream, cancel, resume, approval, and steering interfaces.
  - Capture sanitized protocol fixtures and version information.
- [ ] **DISC-003 — Probe Claude Code interfaces.**
  - Evaluate non-interactive mode, stream JSON, session identifiers, resume behavior, cancellation,
    permissions, and hooks.
  - Capture sanitized fixtures.
- [ ] **DISC-004 — Probe OpenCode interfaces.**
  - Evaluate CLI JSON, server/session API, event streaming, resume, cancellation, provider identity,
    and permission controls.
  - Capture sanitized fixtures.
- [ ] **DISC-005 — Probe Orvek interfaces.**
  - Identify task/session APIs, event records, review integration, cancellation, persistence, and
    protected verification behavior.
  - Capture sanitized fixtures.
- [ ] **DISC-006 — Probe Codex interfaces.**
  - Evaluate App Server protocol and non-steerable exec fallback, including version negotiation,
    turn steering, interrupt, and recovery.
  - Capture sanitized fixtures.
- [ ] **DISC-007 — Build the cross-agent capability matrix.**
  - Capabilities: structured events, live steering, continuation, resume after process restart,
    cancellation, tool events, approval requests, structured final result, model identity,
    token/usage reporting, worktree support, and configurable environment.
  - Mark capabilities as observed, documented, inferred, or unsupported.
- [ ] **DISC-008 — Build the cross-agent failure matrix.**
  - Cover process crash, malformed event, authentication expiry, rate limit, context overflow,
    denied tool, unavailable model, partial file write, network failure, hung command, and failed
    cancellation.
- [ ] **DISC-009 — Define agent version support policy.**
  - Define tested version ranges, minimum versions, deprecation policy, runtime probe behavior, and
    unsupported-version error handling.
- [ ] **DISC-010 — Inventory pilot repositories and languages.**
  - Record languages, package managers, build tools, test commands, linters, schema systems,
    deployment descriptors, generated code, ownership, and current CI checks.
- [ ] **DISC-011 — Inventory dependency sources.**
  - Include package manifests, source imports, OpenAPI, Protobuf, GraphQL, queues/topics, shared
    databases, Terraform, Kubernetes, feature flags, scheduled jobs, generated SDKs, runbooks, and
    ownership files.
- [ ] **DISC-012 — Create a historical change corpus.**
  - Sample accepted, rejected, reverted, incident-causing, over-engineered, under-tested, and
    successful minimal changes.
  - Redact or exclude data that policy does not permit.
- [ ] **DISC-013 — Label the historical corpus.**
  - At least two reviewers label affected components, correctness, test sufficiency, unnecessary
    code, duplication, architectural fit, risk, and final outcome.
  - Record disagreements instead of forcing false consensus.
- [ ] **DISC-014 — Establish current quality baselines.**
  - Measure normal diff size, files changed, complexity, duplication, test runtime, review rounds,
    defects, rollback frequency, and dependency additions by repository and task class.
- [ ] **DISC-015 — Baseline LocalJev on representative observations.**
  - Save model version, LocalJev version, prompt/question version, inputs, outputs, latency, retries,
    and human labels.
- [ ] **DISC-016 — Map Veyro extension points and assumptions.**
  - Document worker protocol, scheduler, policy, persistence, event model, CLI, repository coupling,
    single-worker assumptions, and changes required for each later milestone.
- [ ] **DISC-017 — Produce a discovery report.**
  - Summarize adapter feasibility, unsupported features, major risks, baseline metrics, and unknowns.

## Gate G1 — Discovery complete

- [ ] Every agent has a documented, tested automation path or is explicitly excluded with a reason.
- [ ] Pilot repositories and historical baselines are available.
- [ ] No architecture decision assumes an unobserved agent capability.

---

# 3. Versioned domain schemas

- [ ] **SCHEMA-001 — Define `TaskContract` v1.**
  - Fields: identifier, intent, requested outcome, allowed repositories, allowed paths, forbidden
    paths, acceptance criteria, invariants, forbidden changes, required checks, risk level,
    migration requirements, rollout requirements, rollback requirements, approvals, resource
    limits, data policy, and expiry.
- [ ] **SCHEMA-002 — Add acceptance-criterion identifiers.**
  - Each criterion must be independently addressable and linked to evidence.
- [ ] **SCHEMA-003 — Add contract ambiguity validation.**
  - Detect empty outcomes, contradictory criteria, missing rollback for applicable risks, unknown
    repositories, unbounded scope, and forbidden/allowed path overlap.
- [ ] **SCHEMA-004 — Add contract amendment records.**
  - Amendments must include reason, author, timestamp, affected criteria, and required reapproval.
- [ ] **SCHEMA-005 — Define `WorkspaceManifest` v1.**
  - Fields: projects, repositories, paths, languages, build/check commands, contracts, generated
    paths, owners, policy profiles, deployment units, and explicit dependency edges.
- [ ] **SCHEMA-006 — Define configuration inheritance.**
  - Establish precedence among global, workspace, repository, task, and command-line configuration.
  - Reject unsafe weakening unless an authorized exception exists.
- [ ] **SCHEMA-007 — Define `AgentDefinition` and `AgentCapabilities` v1.**
  - Include adapter type, executable/API endpoint, tested versions, local/remote classification,
    data policy, permissions, models, capabilities, concurrency, and resource limits.
- [ ] **SCHEMA-008 — Define `AgentEventEnvelope` v1.**
  - Include run/session IDs, sequence number, timestamp, adapter, raw event reference, normalized
    category, severity, repository/worktree, correlation ID, and redaction status.
- [ ] **SCHEMA-009 — Define immutable raw-event storage references.**
  - Raw events must be content-addressed and linked without forcing all consumers to parse them.
- [ ] **SCHEMA-010 — Define `CheckResult` v1.**
  - Include check identity/version, command/tool version, scope, hard/advisory status, result,
    severity, duration, artifacts, before/after metrics, and failure classification.
- [ ] **SCHEMA-011 — Define `EvidenceBundle` v1.**
  - Link task criteria to check results, diffs, dependency edges, test output, model assessments, and
    artifact hashes.
- [ ] **SCHEMA-012 — Define evidence provenance.**
  - Every value must identify collector, collector version, time, source revision, scope, and content
    digest.
- [ ] **SCHEMA-013 — Define `ImpactPlan` v1.**
  - Fields: affected repositories/components/contracts, rationale, edge provenance, implementation
    order, compatibility strategy, tests, integration environment, migration, rollout, rollback,
    risks, unknowns, and required approvals.
- [ ] **SCHEMA-014 — Define `ChangeSet` v1.**
  - Include pinned base revisions, per-repository worktrees/patches, dependency DAG, current phase,
    checks, approvals, retry history, partial failures, and release-readiness result.
- [ ] **SCHEMA-015 — Define `PolicyDecision` v1.**
  - Include action, deterministic reasons, advisory assessments, cited evidence, violated rules,
    required approvals, and next permitted transitions.
- [ ] **SCHEMA-016 — Define schema versioning and migration rules.**
  - Add compatibility tests and a migration registry for persisted records.
- [ ] **SCHEMA-017 — Publish JSON Schema and typed-language bindings.**
  - Python is required initially; generated or hand-maintained bindings for additional components
    must pass shared conformance fixtures.
- [ ] **SCHEMA-018 — Add schema conformance fixtures.**
  - Include valid, invalid, old-version, unknown-field, oversized, and malicious samples.

## Gate G2 — Schema contract stable

- [ ] Schemas are reviewed before persistence, adapter, or orchestration code depends on them.

---

# 4. Security and trust model

- [ ] **SEC-001 — Write the threat model.**
  - Cover malicious repository content, prompt injection, compromised dependencies, hostile tool
    output, agent compromise, credential theft, path traversal, symlink attacks, command injection,
    data exfiltration, event tampering, and approval spoofing.
- [ ] **SEC-002 — Define trust boundaries.**
  - Separate control plane, adapters, agents, repositories, LocalJev, remote providers, check tools,
    artifact store, human approvers, and CI.
- [ ] **SEC-003 — Define repository-instruction policy.**
  - Inventory `AGENTS.md`, `CLAUDE.md`, tool-specific rules, hooks, and nested instructions.
  - Record which instructions are trusted, scoped, ignored, or surfaced for approval.
- [ ] **SEC-004 — Implement credential minimization.**
  - Each adapter receives only credentials it requires. TypeSafe/LocalJev variables and other agent
    credentials must not leak into worker processes.
- [ ] **SEC-005 — Implement log and event redaction.**
  - Redact configured secret patterns before persistence while retaining a verifiable redaction
    marker.
- [ ] **SEC-006 — Implement filesystem scope enforcement.**
  - Resolve real paths, reject traversal and unsafe symlinks, restrict writes to assigned worktrees,
    and protect configured files.
- [ ] **SEC-007 — Implement protected-path policy.**
  - Require approval for credentials, production configuration, CI trust settings, release keys,
    ownership rules, and policy files.
- [ ] **SEC-008 — Implement command policy.**
  - Classify read-only, workspace-write, network, privilege, destructive, and unknown commands.
  - Define approval behavior and deny unsafe escalation.
- [ ] **SEC-009 — Implement network policy.**
  - Allow per-agent and per-check endpoint rules, offline mode, and explicit remote-provider data
    classification.
- [ ] **SEC-010 — Implement environment-variable policy.**
  - Use allowlists and explicit adapter mappings rather than inheriting the full control-plane
    environment.
- [ ] **SEC-011 — Implement prompt-injection defenses.**
  - Mark repository and tool text as untrusted data, separate policy from evidence, and prevent
    evidence from changing control-plane permissions.
- [ ] **SEC-012 — Implement artifact integrity checks.**
  - Hash raw events, evidence, patches, reports, and approvals; detect post-hoc mutation.
- [ ] **SEC-013 — Integrate secret scanning.**
  - Scan patches, generated files, logs, and event fixtures before persistence/export.
- [ ] **SEC-014 — Integrate dependency provenance and license checks.**
  - New dependencies must include source, version, license, necessity, and policy result.
- [ ] **SEC-015 — Define approval identity and authorization.**
  - Approvals require an authenticated actor, permitted role, timestamp, scope, and artifact digest.
- [ ] **SEC-016 — Define security exception records.**
  - Exceptions require owner, reason, exact scope, expiry, and review status.
- [ ] **SEC-017 — Add adversarial security tests.**
  - Include malicious filenames, symlinks, prompt injection, fake approval events, output containing
    secrets, control characters, oversized events, and commands disguised as test output.
- [ ] **SEC-018 — Complete an external or independent security review.**
  - Resolve or explicitly accept findings before autonomous execution is enabled.

---

# 5. Deterministic evidence and multi-language checks

- [ ] **EVID-001 — Define the check-plugin SDK.**
  - Plugins declare inputs, supported languages/scopes, command, timeout, resource limits, parser,
    hard/advisory default, and tool-version probe.
- [ ] **EVID-002 — Implement safe process execution for checks.**
  - Bound time, output, CPU/memory where supported, environment, cwd, cancellation, and process
    groups.
- [ ] **EVID-003 — Implement repository baseline capture.**
  - Capture revision, branch, status, tracked/untracked files, submodules, and configured generated
    paths before work begins.
- [ ] **EVID-004 — Implement diff evidence collection.**
  - Produce bounded summary, full content-addressed patch, binary-file metadata, rename detection,
    and per-path statistics.
- [ ] **EVID-005 — Implement changed-surface metrics.**
  - Files, lines, public symbols, dependencies, configuration keys, schemas, database objects, and
    deployment resources added/removed/changed.
- [ ] **EVID-006 — Implement before/after metric comparison.**
  - Metrics must distinguish existing debt from debt introduced by the task.
- [ ] **EVID-007 — Implement generic build-command checks.**
  - Support workspace/repository configuration and structured exit/output evidence.
- [ ] **EVID-008 — Implement generic test-command checks.**
  - Record selected tests, pass/fail/skip counts, duration, retries, failures, and coverage artifacts
    where available.
- [ ] **EVID-009 — Implement lint and formatting checks.**
  - Distinguish new findings from baseline findings.
- [ ] **EVID-010 — Implement type-check checks.**
  - Normalize locations and severity without losing raw reports.
- [ ] **EVID-011 — Implement duplication analysis.**
  - Detect textual and available structural duplication; report only task-introduced deltas as hard
    candidates.
- [ ] **EVID-012 — Implement complexity analysis.**
  - Record function/module complexity and changed hot spots without treating low LOC as a goal.
- [ ] **EVID-013 — Implement dead-code and unused-dependency analysis.**
  - Require language-specific confidence labels to reduce false positives.
- [ ] **EVID-014 — Implement architectural-boundary checks.**
  - Enforce configured module/layer dependency rules and forbidden imports.
- [ ] **EVID-015 — Implement API compatibility checks.**
  - Initial formats: OpenAPI, Protobuf, and GraphQL.
- [ ] **EVID-016 — Implement database migration checks.**
  - Detect destructive operations, missing compatibility windows, unbounded rewrites, absent
    rollback notes, and ordering requirements.
- [ ] **EVID-017 — Implement infrastructure/configuration checks.**
  - Cover Terraform plans, Kubernetes manifests, IAM changes, environment variables, feature flags,
    and deployment descriptors when present.
- [ ] **EVID-018 — Implement security check ingestion.**
  - Normalize existing SAST, dependency, secret, and policy tools rather than inventing replacements.
- [ ] **EVID-019 — Implement performance evidence ingestion.**
  - Capture configured benchmark/budget deltas and mark missing comparable baselines.
- [ ] **EVID-020 — Implement flaky-test evidence.**
  - Record retries separately; a pass after retry must not appear identical to a first-pass success.
- [ ] **EVID-021 — Implement Python defaults.**
  - Discover configured pytest, Ruff, type checker, formatter, complexity, dead-code, and package
    checks without forcing every tool on every repository.
- [ ] **EVID-022 — Implement JavaScript/TypeScript defaults.**
  - Discover package manager, tests, ESLint, TypeScript, unused-code/dependency, build, and package
    boundary checks.
- [ ] **EVID-023 — Implement Go defaults.**
  - Discover format, test, vet, staticcheck, module, race, and build checks where configured.
- [ ] **EVID-024 — Implement Rust defaults.**
  - Discover fmt, clippy, test, build, audit, and unused-dependency checks where configured.
- [ ] **EVID-025 — Implement JVM defaults.**
  - Discover Maven/Gradle test and check tasks plus configured Checkstyle, SpotBugs, Detekt, or
    equivalent tools.
- [ ] **EVID-026 — Implement shell and infrastructure defaults.**
  - Discover ShellCheck, formatting, Terraform, YAML, container, and manifest validation.
- [ ] **EVID-027 — Add a language-agnostic fallback profile.**
  - Always provide git, file-surface, configured-command, secret, and basic duplication evidence.
- [ ] **EVID-028 — Implement generated/vendor path handling.**
  - Exclude configured generated/vendor code from maintainability scoring while still checking
    integrity and unexpected modifications.
- [ ] **EVID-029 — Implement evidence size limits and artifact offloading.**
  - Never silently truncate; include digest, original size, retained summary, and artifact reference.
- [ ] **EVID-030 — Build the final `EvidenceBundle`.**
  - Link every acceptance criterion and policy rule to supporting, missing, or contradictory
    evidence.

---

# 6. Read-only quality gateway

- [ ] **QUAL-001 — Implement the check DAG runner.**
  - Resolve dependencies, run independent checks concurrently within limits, and cancel dependent
    checks appropriately.
- [ ] **QUAL-002 — Implement hard versus advisory classification.**
  - Tests/build/contracts/security policy may be hard; heuristic maintainability signals begin as
    advisory unless configured otherwise.
- [ ] **QUAL-003 — Implement repository quality profiles.**
  - Profiles select checks, budgets, baselines, exclusions, and risk-dependent requirements.
- [ ] **QUAL-004 — Implement quality budgets.**
  - Cover new warnings, dependency additions, changed surface, duplication, complexity, public API,
    and generated files. Budgets flag suspicious growth rather than reward minimum LOC.
- [ ] **QUAL-005 — Implement exception workflow.**
  - Exceptions are scoped, justified, approved, expiring, and visible in reports.
- [ ] **QUAL-006 — Implement task-contract validation CLI.**
  - Commands: initialize, validate, explain errors, and render effective policy.
- [ ] **QUAL-007 — Implement read-only review CLI.**
  - Given a task contract and patch/worktree, produce evidence and decisions without modifying code.
- [ ] **QUAL-008 — Implement terminal report.**
  - Separate facts, hard failures, advisory findings, LocalJev assessments, missing evidence,
    approvals, and recommended next actions.
- [ ] **QUAL-009 — Implement machine-readable report.**
  - Stable JSON output conforming to versioned schemas.
- [ ] **QUAL-010 — Implement CI mode.**
  - Deterministic exit codes for pass, hard failure, approval required, invalid configuration, and
    internal error.
- [ ] **QUAL-011 — Prove read-only behavior.**
  - Snapshot repository state before/after and test that gateway execution cannot alter tracked or
    untracked workspace content.
- [ ] **QUAL-012 — Implement historical replay.**
  - Run evidence and policy against pinned historical revisions and patches.
- [ ] **QUAL-013 — Run shadow mode on active work.**
  - Reports are collected but cannot block or steer work.
- [ ] **QUAL-014 — Review false positives and missing checks.**
  - Record reviewer disposition and feed improvements back into profiles, not ad hoc prompt edits.

## Gate G3 — Read-only gateway qualified

- [ ] Historical and shadow runs produce stable reports.
- [ ] Hard checks match existing CI outcomes on pilots.
- [ ] No repository mutation, secret leakage, or unexplained blocking finding remains.

---

# 7. LocalJev semantic supervision and calibration

- [ ] **JEV-001 — Create a versioned question registry.**
  - Every question has identifier, text, purpose, applicable phase, evidence requirements, model
    compatibility, owner, and version.
- [ ] **JEV-002 — Define planning questions.**
  - Include scope completeness, affected-component completeness, missing contracts, migration need,
    rollback adequacy, ambiguity, and human-input need.
- [ ] **JEV-003 — Define implementation-quality questions.**
  - Include minimality, unnecessary abstraction, duplication, convention fit, unrelated work,
    maintainability regression, and acceptance-criterion satisfaction.
- [ ] **JEV-004 — Define verification questions.**
  - Include test sufficiency, failure-mode coverage, compatibility evidence, independent verification
    need, and release-readiness uncertainty.
- [ ] **JEV-005 — Define cross-repository questions.**
  - Include missed consumers, ordering safety, compatibility window, integration coverage, partial
    failure, and rollback viability.
- [ ] **JEV-006 — Implement phase-specific evidence assembly.**
  - Send compact structured summaries and cited artifact references, never an uncontrolled full
    repository dump.
- [ ] **JEV-007 — Implement evidence coverage reporting.**
  - Mark which evidence each question received and what was absent.
- [ ] **JEV-008 — Record model identity and inference configuration.**
  - Model name/digest, runner, LocalJev version, question version, settings, seed when supported,
    retries, latency, and token usage.
- [ ] **JEV-009 — Implement timeout, retry, and malformed-response policy.**
  - Distinguish unavailable assessment from a negative answer.
- [ ] **JEV-010 — Implement uncertainty and abstention.**
  - Define ambiguous bands and insufficient-evidence behavior; do not force binary action from every
    probability.
- [ ] **JEV-011 — Implement logical consistency checks.**
  - Flag contradictory combinations such as high readiness with failed hard checks or high
    completeness with missing required repositories.
- [ ] **JEV-012 — Enforce hard-check precedence.**
  - Tests prove that model output cannot override failed hard checks, missing approvals, or invalid
    contracts.
- [ ] **JEV-013 — Build the calibration dataset.**
  - Use labeled historical and shadow-mode examples stratified by project, task type, and risk.
- [ ] **JEV-014 — Implement repeatable evaluation.**
  - Metrics: false-negative rate for critical findings, precision/recall, Brier score, calibration
    error, malformed output, abstention, retries, and latency.
- [ ] **JEV-015 — Evaluate Qwen3 14B.**
  - Publish strengths, failure cases, supported evidence sizes, and per-question calibration.
- [ ] **JEV-016 — Compare candidate local and permitted remote models.**
  - Use identical frozen evidence and labels; do not select solely on aggregate accuracy.
- [ ] **JEV-017 — Set project/risk-specific thresholds.**
  - Thresholds require evaluation evidence and must be versioned.
- [ ] **JEV-018 — Add drift detection.**
  - Re-run frozen cases after model, runner, prompt, question, or evidence-schema changes.
- [ ] **JEV-019 — Add live-service diagnostics.**
  - Verify LocalJev health, readiness, configured model, response shape, and latency before a run.
- [ ] **JEV-020 — Publish the calibration report.**
  - State where LocalJev is advisory, where it may trigger verification, and where it is prohibited
    from autonomous decisions.

## Jeff integration assessment (JEV-021)

- [x] **JEV-021 — Evaluate jeff as a second semantic supervisor.**
  - Assessment date: 2026-09-19.
  - jeff revision: [`230d85d29e5df3454f7eb1aad374da01808191a2`](https://github.com/logan-markewich/jeff/tree/230d85d29e5df3454f7eb1aad374da01808191a2).
  - Decision: keep LocalJev with `qwen3:14b` as the only active supervisor. Add jeff only as an
    opt-in shadow provider during `JEV-013` through `JEV-016`. Do not average, vote, route, or fail
    over between their answers until a Veyro-specific evaluation supports that policy.

### Decision summary

The phrase "multi-model support including jeff" mixes two levels of the system. jeff is not a model
and it is not an Ollama or OpenAI-compatible provider. It is a separate System One server around
`knowledgator/gliformer-large-v1`, a 400M-parameter classification model. The accurate topology is:

```text
Veyro
├── LocalJev -> Ollama -> qwen3:14b
└── jeff     -> PyTorch/MPS -> gliformer-large-v1
```

Veyro should eventually support multiple named semantic providers for evaluation and explicit
routing. It should not add active multi-model decision logic now. The current Qwen path has not been
calibrated on representative Veyro tasks, and jeff has not been compared with this local Qwen
stack. Two uncalibrated answers do not become reliable because they disagree or are averaged.

### Evidence from the current installation

The following observations describe the running instance, not only repository documentation:

| Item | Observed state |
|---|---|
| LocalJev service | `GET http://127.0.0.1:8080/health` returned `{"status":"ok"}`. |
| LocalJev readiness | `GET /ready` returned `{"status":"ready","upstream_model":"qwen3:14b"}`. |
| Ollama model | `qwen3:14b`, Q4_K_M, digest `bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8`. |
| Service process | LaunchAgent `com.githubnext.localjev`, serving from `~/localjev`. |
| LocalJev source | Package `0.2.0`, pinned at commit `0a2d1b889ce1a056e13feddce8fd04532c116d78` with the verified Qwen 3/Ollama compatibility patch. |
| Veyro client | `typesafe-sdk==0.7.0`; [`JevVeyroModel`](src/veyro/veyro/jev.py) sends all nine Noul questions in one `system_one` request. |
| Live Veyro smoke call | The current stack returned all nine bounded values in 14.149 seconds. This proves wire compatibility and availability. One synthetic case does not establish accuracy or calibration. |

The pinned LocalJev checkout passed all 20 Bun tests and `tsc --noEmit`. Veyro's
[`localjev-qwen3-14b-v1`](config/baselines/localjev-qwen3-14b.json) manifest records the service
revision, Ollama and Qwen identities, inference settings, TypeSafe client version, and assessment
question digest. A test fails when the question definitions drift without a new baseline.

### What jeff provides

jeff `0.1.0` is a Python 3.12 FastAPI service with a TypeSafe-compatible
`POST /v1/systemone` endpoint. It converts Noul, Choice, and Score questions into GLiFormer
classification groups, scores labels, normalizes the scores, and returns the usual System One answer
shape. The core flow is visible in
[`Engine.run_batch`](https://github.com/logan-markewich/jeff/blob/230d85d29e5df3454f7eb1aad374da01808191a2/src/jeff/core/engine.py#L31-L71).

Relevant capabilities and constraints are:

- The official TypeSafe SDK can call jeff without a custom wire adapter. jeff includes an SDK
  round-trip test in
  [`tests/test_sdk_live.py`](https://github.com/logan-markewich/jeff/blob/230d85d29e5df3454f7eb1aad374da01808191a2/tests/test_sdk_live.py#L39-L58).
- The server has PyTorch and ONNX backends. The ONNX path replaces only the encoder; the RNN and
  classification head still use PyTorch.
- Device selection supports CUDA, Apple MPS, and CPU. The project recommends MPS on a Mac.
- The server implements request batching, queue limits, optional API-key authentication, optional
  rate limiting, request limits, `/v1/models`, `/healthz`, and `/stats`.
- One jeff process loads one GLiFormer checkpoint. `jev-latest` and `jev` are aliases for that
  checkpoint, not model-selection values. See
  [`server/config.py`](https://github.com/logan-markewich/jeff/blob/230d85d29e5df3454f7eb1aad374da01808191a2/src/jeff/server/config.py#L15-L20).
- jeff isolates Noul questions by default. Veyro's nine questions therefore require nine encoder
  passes, though jeff may batch those passes. See
  [`core/groups.py`](https://github.com/logan-markewich/jeff/blob/230d85d29e5df3454f7eb1aad374da01808191a2/src/jeff/core/groups.py#L27-L41).
- The default request limit is 20,000 state characters. A Veyro observation can exceed that because
  its configured diff limit alone is 20,000 characters and worker history adds more content. Veyro
  needs an endpoint-aware size check or a smaller shared evidence envelope before a jeff canary.
- jeff's probabilities are normalized GLiFormer sigmoid scores with global temperature scaling.
  The source explicitly says they are not calibrated posteriors. See
  [`core/answers.py`](https://github.com/logan-markewich/jeff/blob/230d85d29e5df3454f7eb1aad374da01808191a2/src/jeff/core/answers.py#L24-L35).

### Compatibility with LocalJev and Qwen

| Question | Result |
|---|---|
| Can Veyro call jeff through `typesafe-sdk`? | Yes. The wire shape used by `JevVeyroModel` is compatible. |
| Can the current CLI select jeff and LocalJev at the same time? | No. It relies on one process-wide `TYPESAFE_BASE_URL` and creates one `JevVeyroModel`. |
| Can jeff run Qwen? | No. Its backend expects GLiFormer-specific model internals. |
| Can LocalJev use jeff as its OpenAI upstream? | No. LocalJev calls `/v1/chat/completions`; jeff exposes `/v1/systemone`. |
| Can jeff replace LocalJev for one run? | Yes, by pointing an endpoint-scoped TypeSafe client at jeff, subject to the 20,000-character request limit. |
| Can both services run on this Mac? | Probably, but this needs measurement. jeff can use MPS while Ollama runs Qwen, so both will compete for unified memory and GPU time. |
| Are outputs directly comparable? | No. The services derive probabilities differently and require separate calibration and provenance. |

The clean integration point is the existing [`VeyroModel`](src/veyro/veyro/base.py) protocol.
Do not put jeff behind LocalJev. Do not hide both providers behind the ambiguous `jev-latest` alias.
Record the service, implementation revision, actual checkpoint and digest, backend, precision, and
inference settings with every assessment.

### Quality and maturity assessment

The jeff repository contains useful tests and unusually detailed benchmark artifacts, but it is too
new to treat as a production dependency:

- GitHub reported seven commits from one contributor, no tags, no releases, and no CI workflow at
  assessment time.
- The package version is `0.1.0`. The repository and the referenced large checkpoint were created or
  updated within the previous day.
- A fresh, model-free protocol test run passed: 24 tests passed. The full upstream suite also has six
  model-dependent tests that require downloaded weights.
- The project is MIT-licensed. The referenced GLiFormer checkpoint reports Apache-2.0.
- The server defaults to `0.0.0.0` and disables authentication when `JEFF_API_KEYS` is empty. `/stats`
  is unauthenticated and exposes backend paths and active settings. A local trial must bind to
  `127.0.0.1`, require a key, and avoid exposing the port.

The project's own 1,600-item benchmark reports weaker results than hosted Jev on reasoning-heavy
tasks. Its aggregate Noul AUROC was 0.844 versus 0.975 for hosted Jev, with BoolQ at 0.751 versus
0.954. It was close on binary sentiment. See
[`bench/RESULTS.md`](https://github.com/logan-markewich/jeff/blob/230d85d29e5df3454f7eb1aad374da01808191a2/bench/RESULTS.md#L555-L599).
These numbers do not compare jeff with this machine's LocalJev and `qwen3:14b`. They support a
Veyro-specific trial, not a replacement decision.

### Recommended development sequence

Use the existing roadmap rather than starting active multi-model orchestration early:

1. **Pin the current baseline — completed 2026-09-19.** The deployed Qwen compatibility change is
   commit `0a2d1b889ce1a056e13feddce8fd04532c116d78`. The machine-readable
   [`localjev-qwen3-14b-v1`](config/baselines/localjev-qwen3-14b.json) manifest records LocalJev,
   Ollama, Qwen, inference, client, and question revisions as required by `JEV-008`.
2. **Make providers explicit — completed 2026-09-19.** `JevProviderConfig` now validates an
   endpoint-scoped base URL, API-key environment reference, request model, provider ID, checkpoint
   identity, and timeout. `JevVeyroModel` passes the endpoint and key explicitly to the SDK and
   refuses to fall back to process-global credentials. The authoritative provider remains singular;
   shadow-provider fan-out belongs to step 4.
3. **Persist separate assessments — completed 2026-09-19.** Every built-in model now attaches
   typed provenance with provider ID, authoritative/shadow role, implementation, endpoint, request
   model, checkpoint, question version, timeout/retry settings, and measured latency. State and event
   records retain each provider's complete nine-value assessment as a separate history entry.
4. **Add shadow fan-out only — completed 2026-09-19, disabled by default.** The optional
   `VEYRO_JEV_SHADOW_*` provider receives the same observation concurrently with LocalJev/Qwen.
   Typed batches keep the authoritative policy input separate from shadow results. Independent
   provider timeouts bound both calls, and shadow errors produce non-fatal partial-failure events.
   The jeff canary remains disabled until step 5 can verify and harden its checkpoint.
5. **Harden a local jeff canary — pinned configuration; activation blocked.** The disabled
   `jeff-gliformer-large-v1-shadow-v1` manifest pins the jeff commit, Hugging Face revision, expected
   weight digest, loopback port 8081, required authentication, MPS/float32 torch settings,
   temperature, isolation, and request limits. Veyro now reproduces jeff's state serialization and
   rejects observations above 20,000 characters before network I/O. The 2.3 GB weights are still
   unavailable through the approved corporate CDN path, so their digest and a live hardened service
   remain unverified.
6. **Run the shared evaluation.** Under `JEV-013` through `JEV-016`, compare critical false-negative
   rate, precision and recall, Brier score, calibration error, abstention, malformed responses,
   latency, memory use, and Qwen contention. Use representative Veyro observations and human
   labels. Public news and sentiment benchmarks are not enough.
7. **Choose a narrow policy from evidence.** Promote jeff only if it meets a predeclared threshold
   for a specific question class or provides a measured latency or resource benefit. If it does not,
   remove the canary configuration. Do not retain an unused multi-provider path.

### Promotion criteria

jeff integration is warranted beyond shadow mode only when all of these conditions hold:

- its critical false-negative rate meets the accepted bound on representative Veyro cases;
- its scores are calibrated separately for the questions it will answer;
- the routing rule names those questions and cannot lower risk or override hard checks;
- simultaneous Qwen and GLiFormer execution stays within latency and memory budgets;
- the provider passes health, timeout, overload, malformed-response, restart, and drift tests; and
- model and service identity remain visible in evidence, policy decisions, and audit exports.

Until then, the supported runtime remains single-supervisor: LocalJev with `qwen3:14b`.

## Gate G4 — LocalJev use approved

- [ ] Thresholds and permitted actions are backed by measured results.
- [ ] High-risk false negatives are reviewed and within an explicitly accepted bound.
- [ ] LocalJev remains advisory wherever evidence is insufficient.

---

# 8. Agent adapter protocol and feasibility spike

- [ ] **ADAPT-001 — Define the minimal adapter protocol.**
  - Operations: probe, start, stream events, request approval response, steer if supported, cancel,
    await completion, resume if supported, and collect result.
- [ ] **ADAPT-002 — Define capability negotiation.**
  - Capabilities are probed at runtime and include stability/source evidence.
- [ ] **ADAPT-003 — Define adapter error taxonomy.**
  - Configuration, unsupported version, authentication, permission, rate limit, transport, protocol,
    malformed event, context limit, tool failure, cancellation, timeout, and internal error.
- [ ] **ADAPT-004 — Implement bounded process supervision.**
  - Process groups, stdout/stderr readers, output limits, heartbeats, cancellation escalation, and
    orphan cleanup.
- [ ] **ADAPT-005 — Implement raw plus normalized event persistence.**
  - Preserve ordering, raw payload digest, adapter parser version, and normalization errors.
- [ ] **ADAPT-006 — Implement a deterministic fake adapter.**
  - Support all lifecycle states and injectable failures for runtime tests.
- [ ] **ADAPT-007 — Build the shared adapter conformance suite.**
  - Tests probe, start, stream, sequence, complete, cancel, timeout, malformed output, process crash,
    resume/steer capability claims, environment isolation, and redaction.
- [ ] **ADAPT-008 — Implement Prime Agent spike adapter.**
  - Map only observed stable interfaces.
  - Pass applicable conformance tests.
  - Document unsupported and unstable capabilities.
- [ ] **ADAPT-009 — Implement Claude Code spike adapter.**
  - Prefer machine-readable streaming interfaces.
  - Validate session identity, permissions, cancellation, and continuation behavior.
- [ ] **ADAPT-010 — Implement OpenCode spike adapter.**
  - Prefer server/session APIs where more stable than terminal output.
  - Validate configured provider/model identity and cancellation.
- [ ] **ADAPT-011 — Implement Orvek spike adapter.**
  - Integrate through authorized task/session interfaces and preserve Orvek review/protected-check
    semantics.
- [ ] **ADAPT-012 — Extract Codex into a conforming adapter.**
  - Preserve App Server steering while making Codex optional.
- [ ] **ADAPT-013 — Add adapter protocol fixture tests.**
  - Sanitized recorded fixtures for every tested version must parse without live credentials.
- [ ] **ADAPT-014 — Test cancellation safety for every adapter.**
  - Confirm no worker or child process remains and partial files remain isolated in the worktree.
- [ ] **ADAPT-015 — Test unavailable capability behavior.**
  - The runtime must choose a valid alternative instead of pretending steering/resume succeeded.
- [ ] **ADAPT-016 — Test adapter credential isolation.**
  - Each adapter sees only its explicit environment allowlist.
- [ ] **ADAPT-017 — Benchmark adapter overhead and event fidelity.**
  - Measure startup, event latency, lost/unknown events, cancellation time, and resource use.
- [ ] **ADAPT-018 — Publish the adapter feasibility report.**
  - Decide which adapters qualify for initial execution, read-only observation, or exclusion.

## Gate G5 — Adapter feasibility approved

- [ ] At least two non-Codex adapters pass the lifecycle conformance suite.
- [ ] Unsupported capabilities are explicit and safely handled.
- [ ] No adapter depends solely on unversioned, human-formatted output without a containment plan.

---

# 9. Veyro-versus-new-core architecture decision

- [ ] **ARCH-001 — Define decision criteria.**
  - Criteria: schema fit, agent neutrality, multi-repository support, state-machine fit, persistence,
    recovery, scheduler, security boundaries, testing cost, migration cost, and long-term ownership.
- [ ] **ARCH-002 — Prototype schema integration in Veyro.**
  - Load a task contract, evidence bundle, generic adapter, and policy decision without Codex-specific
    branching.
- [ ] **ARCH-003 — Prototype multi-session state in Veyro.**
  - Demonstrate two generic sessions and independent event streams without implementing full
    orchestration.
- [ ] **ARCH-004 — Assess Veyro persistence.**
  - Determine whether existing per-repository JSON state can support event replay, migrations,
    leases, and cross-repository change sets.
- [ ] **ARCH-005 — Assess Veyro policy coupling.**
  - Determine whether policy can consume hard checks, approvals, abstentions, and graph evidence
    without replacement.
- [ ] **ARCH-006 — Assess Veyro scheduler coupling.**
  - Identify assumptions about one worker, one repository, in-process runtime, and Codex steering.
- [ ] **ARCH-007 — Prototype a minimal clean core alternative.**
  - Implement only schema loading, event append/replay, fake adapter lifecycle, and policy transition
    to estimate complexity; do not build two products.
- [ ] **ARCH-008 — Compare prototypes using recorded scenarios.**
  - Include crash recovery, cancellation, two agents, two repositories, failed hard check, approval,
    and LocalJev outage.
- [ ] **ARCH-009 — Write the architecture ADR.**
  - Choose: extend Veyro, extract/refactor Veyro components, or build a new core.
  - Include migration and repository strategy.
- [ ] **ARCH-010 — Delete the rejected prototype or clearly archive it.**
  - Avoid maintaining accidental parallel runtimes.

## Gate G6 — Runtime architecture selected

- [ ] No production implementation proceeds without the approved ADR.

---

# 10. Agent-neutral single-repository runtime

- [ ] **RUN-001 — Implement durable run creation from `TaskContract`.**
  - Pin repository revision, effective policy, adapter versions, model versions, and configuration.
- [ ] **RUN-002 — Implement isolated worktree creation.**
  - Unique path/branch, clean base, no protected-branch writes, safe cleanup, and collision handling.
- [ ] **RUN-003 — Implement mission construction.**
  - Provide task intent, allowed scope, acceptance criteria, invariants, forbidden changes, required
    checks, and relevant context without leaking control-plane policy as editable instructions.
- [ ] **RUN-004 — Implement explicit agent selection.**
  - Validate data policy, adapter availability, capabilities, model identity, concurrency, and risk.
- [ ] **RUN-005 — Implement session lifecycle orchestration.**
  - Start, observe, persist, complete, fail, cancel, and clean up through the generic adapter.
- [ ] **RUN-006 — Implement event sequence validation.**
  - Detect duplicates, gaps, reordered events, clock issues, unknown events, and parser failures.
- [ ] **RUN-007 — Implement periodic and event-triggered evidence snapshots.**
  - Debounce safely while guaranteeing a final snapshot.
- [ ] **RUN-008 — Implement deterministic policy engine.**
  - Hard checks, risk, approvals, limits, and state transitions first; LocalJev only contributes
    configured advisory signals.
- [ ] **RUN-009 — Implement capability-aware interventions.**
  - Live steer where supported; otherwise pause/cancel and continue a new/resumed session according
    to policy.
- [ ] **RUN-010 — Implement approval queue.**
  - Pause safely, present exact command/change/evidence, validate response identity, and resume
    idempotently.
- [ ] **RUN-011 — Implement iteration, time, cost, and resource limits.**
  - Limits must survive restart and cannot be reset by opening a new session.
- [ ] **RUN-012 — Implement check execution checkpoints.**
  - At minimum: pre-work baseline, post-agent, pre-verifier, and final.
- [ ] **RUN-013 — Implement acceptance-criterion evidence mapping.**
  - No criterion may silently disappear; unresolved criteria block readiness.
- [ ] **RUN-014 — Implement blind verification mission.**
  - Verifier receives task contract, patch, and evidence, but not the implementer’s hidden reasoning
    or self-assessment.
- [ ] **RUN-015 — Implement verifier selection policy.**
  - Prefer a different qualified adapter/model for configured risk levels; deterministic checks
    remain primary.
- [ ] **RUN-016 — Implement verifier patch isolation.**
  - Verification is read-only by default. Proposed fixes require a separate authorized session.
- [ ] **RUN-017 — Implement patch export.**
  - Produce patch/bundle/branch metadata and evidence; do not merge protected branches automatically.
- [ ] **RUN-018 — Implement run resume.**
  - Reconstruct from durable events, reconcile live processes/worktrees, and avoid duplicate agent
    starts or checks.
- [ ] **RUN-019 — Implement idempotent cancellation.**
  - Safe when requested repeatedly, during startup, checks, approval wait, or shutdown.
- [ ] **RUN-020 — Implement CLI commands.**
  - `contract`, `plan`, `run`, `status`, `inspect`, `approve`, `cancel`, `resume`, `evidence`, and
    `export`.
- [ ] **RUN-021 — Implement concise live terminal rendering.**
  - Show facts and decisions without animation noise or leaking hidden/raw sensitive content.
- [ ] **RUN-022 — Implement agent-neutral terminology migration.**
  - Remove user-facing Codex assumptions while retaining a Codex adapter.
- [ ] **RUN-023 — Add adapter matrix end-to-end tests.**
  - Run the same fake repository task with every qualified adapter using deterministic or authorized
    fixtures.
- [ ] **RUN-024 — Add recovery and failure end-to-end tests.**
  - Crash during write, check, approval, verification, cancellation, and finalization.

## Gate G7 — Single-repository execution qualified

- [ ] At least two agent adapters complete the same pilot task through the generic runtime.
- [ ] Hard checks, approvals, cancellation, resume, and blind verification behave correctly.
- [ ] Changes remain isolated and require explicit export/merge.

---

# 11. Workspace inventory and dependency graph

- [ ] **GRAPH-001 — Implement workspace manifest loading.**
  - Resolve paths safely and reject duplicate identities, missing repositories, or policy conflicts.
- [ ] **GRAPH-002 — Implement opt-in repository discovery.**
  - Discovery proposes entries; it must not silently add repositories to trusted scope.
- [ ] **GRAPH-003 — Define typed graph nodes.**
  - Repository, package, service, deployable, API, schema, queue/topic, database/table, job, feature
    flag, infrastructure resource, owner, runbook, test suite, and environment.
- [ ] **GRAPH-004 — Define typed graph edges.**
  - Compile, import, runtime call, API contract, event publish/consume, data read/write, generation,
    deployment order, ownership, operational dependency, and manual override.
- [ ] **GRAPH-005 — Add edge provenance, confidence, and freshness.**
  - Explicit manifest edges are distinguished from parser, heuristic, and model-inferred edges.
- [ ] **GRAPH-006 — Ingest explicit manifest edges.**
  - Validate targets and report cycles without assuming every cycle is invalid.
- [ ] **GRAPH-007 — Ingest package-manager dependencies.**
  - Initial support aligned with pilot languages and monorepo/workspace formats.
- [ ] **GRAPH-008 — Ingest source import/module dependencies.**
  - Use language-specific parsers or indexes; attach parser version and unresolved references.
- [ ] **GRAPH-009 — Ingest OpenAPI relationships.**
  - Producers, generated clients, consumers where configured, versions, and compatibility metadata.
- [ ] **GRAPH-010 — Ingest Protobuf relationships.**
  - Definitions, generated outputs, producers/consumers, and breaking-change tool results.
- [ ] **GRAPH-011 — Ingest GraphQL relationships.**
  - Schemas, operations, generated clients, services, and validation results.
- [ ] **GRAPH-012 — Ingest event/queue relationships.**
  - Topic ownership, publishers, consumers, schemas, and delivery assumptions from explicit/configured
    sources.
- [ ] **GRAPH-013 — Ingest database relationships.**
  - Services, migrations, schemas/tables, reads/writes where explicit or statically discoverable.
- [ ] **GRAPH-014 — Ingest infrastructure relationships.**
  - Terraform/Kubernetes modules, deployables, IAM, configuration, secrets references, and ordering.
- [ ] **GRAPH-015 — Ingest feature flags and scheduled jobs.**
  - Flag owners/consumers and job triggers/dependencies where configured.
- [ ] **GRAPH-016 — Ingest ownership and operational relationships.**
  - CODEOWNERS, service catalog, dashboards, alerts, and runbooks.
- [ ] **GRAPH-017 — Implement manual edge overrides.**
  - Add, suppress, or correct edges with owner, reason, source, and expiry/review date.
- [ ] **GRAPH-018 — Implement incremental refresh.**
  - Refresh affected repositories/artifacts and invalidate stale derived edges.
- [ ] **GRAPH-019 — Implement graph storage and migration.**
  - Deterministic identifiers, transaction safety, schema version, backup, and rebuild path.
- [ ] **GRAPH-020 — Implement graph query API/CLI.**
  - Neighbors, paths, affected dependents, edge evidence, confidence, freshness, cycles, and unknowns.
- [ ] **GRAPH-021 — Implement stale/low-confidence reporting.**
  - Plans must surface weak graph regions rather than silently treating them as complete.
- [ ] **GRAPH-022 — Validate graph against historical changes.**
  - Measure affected-repository recall, false expansion, missing edge classes, and stale-edge impact.

## Gate G8 — Graph fit established

- [ ] Historical impact analysis reaches an accepted recall for pilot changes.
- [ ] Missing and low-confidence regions are explicit and approval-gated.

---

# 12. Cross-repository impact planning

- [ ] **PLAN-001 — Extract task entities and seed repositories deterministically.**
  - Use explicit contract scope, paths, component names, schemas, and known owners first.
- [ ] **PLAN-002 — Expand affected components through typed graph rules.**
  - Rules vary by edge type, direction, change type, compatibility, confidence, and risk.
- [ ] **PLAN-003 — Add semantic retrieval only after deterministic expansion.**
  - Semantic results are candidates with provenance, never silent graph facts.
- [ ] **PLAN-004 — Build bounded planning context.**
  - Include task contract, graph evidence, relevant contracts, repository summaries, historical
    patterns, policies, and explicit unknowns.
- [ ] **PLAN-005 — Generate a schema-valid impact plan.**
  - Use a selected planning agent, but reject invented repositories/components not present as
    declared candidates.
- [ ] **PLAN-006 — Determine compatibility strategy.**
  - Same-release, backward-compatible, expand/migrate/contract, feature-flagged, or manually
    coordinated.
- [ ] **PLAN-007 — Determine implementation order.**
  - Produce a DAG with rationale, preconditions, and parallelizable groups.
- [ ] **PLAN-008 — Determine required checks and tests.**
  - Unit, integration, contract, end-to-end, migration, security, performance, and operational
    checks as applicable.
- [ ] **PLAN-009 — Determine migration requirements.**
  - Data, schema, configuration, generated clients, backfill, compatibility window, and cleanup.
- [ ] **PLAN-010 — Determine rollout and rollback requirements.**
  - No-op where out of scope, but omission must be explicit.
- [ ] **PLAN-011 — Compute deterministic risk classification.**
  - Use contract and graph facts; model output may recommend escalation but not lower risk.
- [ ] **PLAN-012 — Run LocalJev planning assessment.**
  - Evaluate completeness, unknowns, contradictions, human need, and verification requirements.
- [ ] **PLAN-013 — Validate plan against task contract and policy.**
  - Every criterion, invariant, repository, approval, and hard check must map to plan steps/evidence.
- [ ] **PLAN-014 — Implement plan review and amendment.**
  - Human edits are versioned and invalidate affected approvals.
- [ ] **PLAN-015 — Implement plan diffing.**
  - Show added/removed repositories, edges, steps, checks, risks, and compatibility assumptions.
- [ ] **PLAN-016 — Implement `plan` CLI/API.**
  - Support read-only generation, validation, explanation, approval, and export.
- [ ] **PLAN-017 — Evaluate plans on historical corpus.**
  - Measure affected-repository/component recall, unnecessary expansion, ordering correctness,
    migration/rollback omissions, and reviewer agreement.
- [ ] **PLAN-018 — Run live planning in shadow mode.**
  - Compare plans with actual engineer changes without controlling execution.

## Gate G9 — Impact planning qualified

- [ ] Historical and live-shadow plans meet approved recall and safety criteria.
- [ ] Low-confidence or high-risk plans require human approval.

---

# 13. Controlled multi-repository execution

- [ ] **XREPO-001 — Implement durable change-set creation.**
  - Pin approved impact-plan version and every repository base revision.
- [ ] **XREPO-002 — Create per-repository isolated worktrees.**
  - Validate bases, branch naming, disk limits, and cleanup independently.
- [ ] **XREPO-003 — Implement dependency-DAG scheduler.**
  - Run eligible steps concurrently while respecting ordering, limits, and approvals.
- [ ] **XREPO-004 — Implement repository and shared-resource leases.**
  - Prevent conflicting active change sets and recover expired leases safely.
- [ ] **XREPO-005 — Detect base revision drift.**
  - Pause/replan when upstream changes invalidate graph, patches, checks, or approvals.
- [ ] **XREPO-006 — Implement compatibility phases.**
  - Support expand, migrate, switch consumers, verify, and contract/cleanup as explicit phases.
- [ ] **XREPO-007 — Build repository-specific missions.**
  - Each agent receives only its plan slice plus required cross-repository contracts and invariants.
- [ ] **XREPO-008 — Execute per-repository checks.**
  - Preserve evidence and prohibit downstream scheduling when required predecessors fail.
- [ ] **XREPO-009 — Execute cross-repository contract checks.**
  - Validate schemas/clients/consumers against the intended compatibility phase.
- [ ] **XREPO-010 — Execute configured integration environment.**
  - Provisioning is plugin-based, bounded, observable, cancellable, and cleaned up.
- [ ] **XREPO-011 — Implement partial-failure handling.**
  - Record completed steps, isolate failed work, cancel unsafe dependents, and recommend retry,
    replan, rollback, or escalation.
- [ ] **XREPO-012 — Implement idempotent retries.**
  - A retry cannot duplicate commits, agents, migrations, environments, or approval requests.
- [ ] **XREPO-013 — Implement cross-repository evidence aggregation.**
  - Preserve repository details while producing a global contract/check view.
- [ ] **XREPO-014 — Implement global blind verification.**
  - Verify task criteria, dependency coverage, compatibility, ordering, rollback, and integration
    results without implementer reasoning.
- [ ] **XREPO-015 — Run LocalJev global assessment.**
  - Assess completeness and risk only after hard evidence aggregation.
- [ ] **XREPO-016 — Implement release-readiness policy.**
  - Require all hard checks, criteria evidence, approvals, compatibility, and verifier result.
- [ ] **XREPO-017 — Export per-repository patch/branch/PR metadata.**
  - Link all outputs with one change-set ID and exact base/head revisions.
- [ ] **XREPO-018 — Export deployment-order and rollback artifacts.**
  - The system does not deploy, but it must provide verified ordering and preconditions.
- [ ] **XREPO-019 — Implement multi-repository resume and recovery.**
  - Reconcile worktrees, sessions, checks, integration environments, leases, and approvals.
- [ ] **XREPO-020 — Implement multi-repository cancellation.**
  - Stop active workers/checks, clean transient environments, retain evidence, and preserve patches.
- [ ] **XREPO-021 — Add end-to-end expand/migrate/contract fixture.**
  - Use multiple repositories, schema producer, generated client, consumer, and rollback scenario.
- [ ] **XREPO-022 — Add concurrency/conflict fixtures.**
  - Competing change sets, shared repository, stale base, failed predecessor, and cancellation.

## Gate G10 — Multi-repository execution qualified

- [ ] Low-risk pilot change sets complete with correct ordering, recovery, checks, and export.
- [ ] No autonomous production deployment is enabled.

---

# 14. Reliability, persistence, and observability

- [ ] **OPS-001 — Select and document durable storage.**
  - Compare existing JSON files, SQLite, and other justified options for transactions, migrations,
    replay, concurrency, backup, and portability.
- [ ] **OPS-002 — Implement append-only event recording.**
  - Monotonic sequence, atomic append, digest, actor, schema version, correlation, and redaction.
- [ ] **OPS-003 — Implement derived state snapshots.**
  - Snapshots are rebuildable from events and include source sequence/digest.
- [ ] **OPS-004 — Implement persistence migrations.**
  - Forward migration, backup, failure recovery, and compatibility tests.
- [ ] **OPS-005 — Implement crash replay.**
  - Rebuild state and reconcile external processes/resources without repeating completed actions.
- [ ] **OPS-006 — Implement idempotency keys.**
  - Agent starts, checks, approvals, integration environments, exports, and interventions.
- [ ] **OPS-007 — Implement heartbeat and lease recovery.**
  - Detect crashed runtime versus slow worker and avoid dual ownership.
- [ ] **OPS-008 — Implement queueing and backpressure.**
  - Global, workspace, repository, adapter/provider, and LocalJev concurrency limits.
- [ ] **OPS-009 — Implement resource accounting.**
  - Time, local memory/CPU, disk, model occupancy, remote usage where available, and artifact growth.
- [ ] **OPS-010 — Implement output and artifact retention.**
  - Rotation, quotas, content addressing, deletion, and references that survive log cleanup.
- [ ] **OPS-011 — Implement structured logs.**
  - Run/session/task/change-set IDs, no secrets, stable event categories, and actionable errors.
- [ ] **OPS-012 — Implement metrics.**
  - Throughput, latency, queue depth, adapter failures, check failures, interventions, retries,
    approvals, model availability, assessment drift, and recovery.
- [ ] **OPS-013 — Implement tracing/correlation.**
  - Link contract, plan, agents, checks, LocalJev calls, approvals, and exported patches.
- [ ] **OPS-014 — Implement health and diagnostics commands.**
  - Storage, worktree, agent probes, LocalJev, model, disk, configuration, graph freshness, and policy.
- [ ] **OPS-015 — Implement safe shutdown.**
  - Stop accepting work, persist state, cancel or hand off active operations, and release leases.
- [ ] **OPS-016 — Add chaos tests.**
  - Kill runtime/agent/check/model, fill disk, corrupt event tail, expire credentials, lose network,
    and interrupt during approval/export.
- [ ] **OPS-017 — Add performance/load tests.**
  - Large event streams, large diffs, many repositories, graph refresh, concurrent checks, and model
    queueing.
- [ ] **OPS-018 — Define backup and restore procedure.**
  - Test restore into a clean installation and verify artifact/event integrity.
- [ ] **OPS-019 — Define upgrade and rollback procedure.**
  - Control plane, adapters, schema, LocalJev, model, and check-tool changes.

---

# 15. Routing and multi-agent strategy

- [ ] **ROUTE-001 — Define routing inputs.**
  - Task type, languages, repository, risk, data policy, required capabilities, context size,
    historical results, availability, latency, and resource/cost budgets.
- [ ] **ROUTE-002 — Implement policy-first eligibility filtering.**
  - Exclude agents that violate data, capability, version, authentication, or risk policy.
- [ ] **ROUTE-003 — Implement explicit routing rules before learned routing.**
  - Deterministic and explainable defaults with operator override.
- [ ] **ROUTE-004 — Build agent benchmark tasks.**
  - Representative planning, implementation, debugging, refactoring, testing, and review tasks by
    language/project class.
- [ ] **ROUTE-005 — Measure agent outcomes.**
  - Correctness, hard-check pass, unnecessary change, review findings, recovery, latency, and human
    effort—not self-reported success.
- [ ] **ROUTE-006 — Implement routing history without sensitive transcripts.**
  - Store outcome summaries linked to task class and version.
- [ ] **ROUTE-007 — Implement verifier diversity rules.**
  - Different adapter/model where policy requires, while acknowledging shared-model correlation.
- [ ] **ROUTE-008 — Prevent implementer self-verification.**
  - Self-review may be evidence but cannot satisfy independent verification requirements.
- [ ] **ROUTE-009 — Implement fallback routing.**
  - Handle unavailable/failed agents without silently changing data policy or lowering required
    capabilities.
- [ ] **ROUTE-010 — Explain every routing decision.**
  - Include eligible/ineligible agents, rule, evidence, override, and selected versions.
- [ ] **ROUTE-011 — Add routing canary mode.**
  - Compare suggested versus actual agent without controlling selection.
- [ ] **ROUTE-012 — Defer learned routing until sufficient data exists.**
  - Write an explicit readiness criterion; do not train on sparse or biased outcomes.

---

# 16. Human experience and governance controls

- [ ] **UX-001 — Design the task-contract authoring flow.**
  - Templates, examples, validation, risk prompts, and acceptance-criterion identifiers.
- [ ] **UX-002 — Design plan review.**
  - Show affected repositories, evidence paths, weak graph edges, unknowns, ordering, risks, checks,
    migration, and rollback.
- [ ] **UX-003 — Design approval prompts.**
  - Exact requested action, reason, risk, affected artifacts, expiry, alternatives, and consequences.
- [ ] **UX-004 — Design live run view.**
  - Separate agent activity, deterministic evidence, semantic assessment, policy decision, approvals,
    and resource use.
- [ ] **UX-005 — Design failure and recovery guidance.**
  - Explain retry, resume, replan, rollback, export partial work, or escalate.
- [ ] **UX-006 — Design evidence-centric final report.**
  - Every completion claim links to checks/artifacts; model probabilities are labeled advisory or
    calibrated according to policy.
- [ ] **UX-007 — Implement overrides with reasons.**
  - Authorized, scoped, expiring where appropriate, and immutable in audit history.
- [ ] **UX-008 — Implement notification integration points.**
  - Approval required, blocked, failed, release ready, and drift/health warnings; avoid noisy raw
    event notifications.
- [ ] **UX-009 — Add alert-fatigue controls.**
  - Deduplication, aggregation, severity, quiet periods, and ownership routing.
- [ ] **UX-010 — Implement policy explanation.**
  - Show the exact rule/evidence that permitted or blocked a transition.
- [ ] **UX-011 — Implement audit export.**
  - Task contract, amendments, plans, events, evidence, model metadata, decisions, approvals,
    patches, and digests subject to redaction policy.
- [ ] **UX-012 — Write operator, maintainer, adapter-author, and security documentation.**
- [ ] **UX-013 — Create incident runbooks.**
  - Stuck worker, leaked secret, compromised adapter, corrupted store, incorrect approval, runaway
    resource use, bad model update, and unsafe exported change.

---

# 17. Evaluation, pilots, and rollout

- [ ] **PILOT-001 — Freeze evaluation protocols before pilot results.**
  - Prevent moving metrics/thresholds to make results look successful.
- [ ] **PILOT-002 — Replay the historical corpus through the quality gateway.**
  - Compare findings to labels and actual outcomes.
- [ ] **PILOT-003 — Run read-only shadow mode on current tasks.**
  - No blocking, steering, or code changes.
- [ ] **PILOT-004 — Review all critical false negatives.**
  - Classify missing evidence, graph failure, check failure, model failure, threshold failure, or
    ambiguous contract.
- [ ] **PILOT-005 — Review false positives and operator burden.**
  - Measure ignored findings, approval volume, repeated warnings, and review time.
- [ ] **PILOT-006 — Run agent adapter canaries.**
  - Observe qualified agents on comparable low-risk tasks without automatic routing.
- [ ] **PILOT-007 — Run controlled single-repository pilots.**
  - Isolated branches/worktrees, required human merge, and predefined rollback.
- [ ] **PILOT-008 — Compare against a control baseline.**
  - Similar tasks using the existing workflow; compare quality and human effort, not only speed.
- [ ] **PILOT-009 — Run cross-repository planning-only pilots.**
  - Compare predicted affected components and ordering to engineer-reviewed plans.
- [ ] **PILOT-010 — Run a low-risk multi-repository execution pilot.**
  - Human-approved plan, no production deploy, explicit compatibility strategy, global verification.
- [ ] **PILOT-011 — Measure post-change outcomes.**
  - Review findings, rework, regressions, rollback, incidents, and maintenance follow-up over the
    agreed observation period.
- [ ] **PILOT-012 — Test platform rollback.**
  - Disable orchestration and return to existing agent workflows without losing patches/evidence.
- [ ] **PILOT-013 — Hold the go/no-go review.**
  - Review security, reliability, quality metrics, operator burden, LocalJev calibration, adapter
    stability, and unresolved risks.
- [ ] **PILOT-014 — Define scope expansion criteria.**
  - Additional repositories, agents, risk levels, and automated actions require explicit measured
    gates.
- [ ] **PILOT-015 — Publish known limitations.**
  - Include unsupported agents/versions, graph blind spots, uncalibrated questions, prohibited task
    types, and manual responsibilities.

## Gate G11 — Production-use decision

- [ ] Independent security review is accepted.
- [ ] Reliability and recovery tests pass.
- [ ] Pilot metrics meet predeclared criteria.
- [ ] Operators can disable or roll back the platform safely.
- [ ] High-risk autonomy remains disabled unless separately approved.

---

# 18. Packaging, maintenance, and lifecycle

- [ ] **MAINT-001 — Define repository layout.**
  - Decide core, schemas, adapters, checks, graph, CLI/UI, fixtures, docs, and examples boundaries.
- [ ] **MAINT-002 — Define plugin compatibility policy.**
  - Version handshake, deprecation, conformance tests, and unsupported-version behavior.
- [ ] **MAINT-003 — Package the control plane reproducibly.**
  - Pin build metadata, publish checksums/SBOM, and test clean-machine installation.
- [ ] **MAINT-004 — Package adapters independently where appropriate.**
  - Minimize credential and dependency exposure.
- [ ] **MAINT-005 — Add CI for supported platforms and language versions.**
  - Offline fixtures for agent protocols; live credential tests remain controlled and separate.
- [ ] **MAINT-006 — Add dependency update policy.**
  - Automated update proposals still pass adapter fixtures, schema compatibility, security, and
    LocalJev drift evaluation.
- [ ] **MAINT-007 — Add model update procedure.**
  - Frozen calibration replay and explicit approval before changing the supervising model.
- [ ] **MAINT-008 — Add question/prompt update procedure.**
  - Version bump, calibration replay, threshold review, and audit visibility.
- [ ] **MAINT-009 — Add adapter update procedure.**
  - Probe new version, run recorded/live conformance, canary, and support-matrix update.
- [ ] **MAINT-010 — Add check-tool update procedure.**
  - Baseline drift analysis and false-positive review.
- [ ] **MAINT-011 — Define issue severity and response policy.**
  - Unsafe execution, secret leak, corrupted evidence, false approval, agent incompatibility, and
    ordinary defects.
- [ ] **MAINT-012 — Schedule recurring architecture and policy review.**
  - Revisit assumptions, non-goals, graph quality, model calibration, metrics, and operator burden.

---

# Milestone dependency summary

1. `G0` charter before final schemas.
2. `G1` discovery before promising adapter capabilities.
3. `G2` schemas before persistence and orchestration.
4. Security and deterministic evidence begin before execution privileges.
5. `G3` read-only quality gateway before agent-controlled code changes.
6. `G4` LocalJev calibration before model output can trigger autonomous interventions.
7. `G5` adapter feasibility before selecting the runtime architecture.
8. `G6` architecture ADR before full runtime implementation.
9. `G7` single-repository qualification before cross-repository execution.
10. `G8` graph validation and `G9` planning qualification before multi-repository execution.
11. `G10` controlled execution before broader pilots.
12. `G11` explicit go/no-go before production use or higher-risk autonomy.

# Immediate next tasks

The first implementation sequence is deliberately small and evidence-driven:

1. [ ] Complete `GOV-001` through `GOV-010`.
2. [ ] Complete agent probes `DISC-001` through `DISC-009`.
3. [ ] Inventory pilot repositories and build the historical corpus (`DISC-010` through `DISC-015`).
4. [ ] Map Veyro assumptions (`DISC-016`) and publish the discovery report (`DISC-017`).
5. [ ] Only then finalize the schemas and begin the read-only quality gateway.

Do **not** begin multi-agent orchestration or cross-repository mutation before these tasks and gates
are complete.
