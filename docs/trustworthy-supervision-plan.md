# Trustworthy multi-provider supervision plan

## Outcome

Give Codex, Prime Agent, and OpenCode one local supervision and policy layer while each provider keeps its native interactive interface.

## Invariants

- Keep LocalJev with the pinned `qwen3:14b` model as the only authoritative semantic assessor.
- Preserve each provider's native terminal, credentials, configuration, session storage, and direct CLI command.
- Use supported hooks, extensions, daemons, or server APIs; never scrape terminal output.
- Negotiate capabilities at runtime and reject unsupported controls instead of approximating them silently.
- Store minimal local evidence by default; do not persist prompts, transcripts, file contents, credentials, or environment values without explicit opt-in.
- Put deterministic safety rules before semantic assessment and require human approval for irreversible or unsupported actions.
- Keep provider-specific schemas inside adapters so the Foreman policy core stays provider-neutral.

## Tasks

- [x] **SUP-001 — Define supervision contracts:** Add canonical session identity, event provenance, capability, control-intent, and control-result types with versioned serialization.
- [x] **SUP-002 — Add adapter conformance tests:** Build one reusable contract suite that every provider bridge must pass for ordering, capability honesty, cancellation, and unsupported controls.
- [x] **SUP-003 — Build the secure session broker:** Add a private local event journal and authenticated per-session transport with bounded messages, replay cursors, and idempotent command IDs.
- [x] **SUP-004 — Reduce events into session state:** Convert ordered provider events into one replayable provider-neutral snapshot of task progress, tools, changes, checks, approvals, and completion claims.
- [x] **SUP-005 — Add deterministic boundary policy:** Classify low-risk, review-required, and forbidden actions before any semantic model call.
- [x] **SUP-006 — Assess meaningful checkpoints:** Invoke authoritative LocalJev only for reduced-state checkpoints such as failed verification, risky changes, idle sessions, and completion claims.
- [x] **SUP-007 — Gate every control action:** Authorize proposed controls against policy, provider capabilities, evidence quality, and human-approval requirements before dispatch.
- [x] **SUP-008 — Integrate Prime Agent daemon v7:** Connect a managed native Prime Agent TUI to version-pinned daemon observation, replay, steer, follow-up, abort, and stop operations.
- [x] **SUP-009 — Prove the Prime Agent loop:** Run an end-to-end canary from native event through LocalJev assessment, policy authorization, provider control, and verified result.
- [x] **SUP-010 — Integrate OpenCode server mode:** Connect an authenticated loopback OpenCode server, native attached TUI, SSE observation, async prompt, approval, and abort endpoints.
- [x] **SUP-012 — Integrate Codex hooks:** Publish stable Codex hook events and queue controls while keeping experimental App Server capabilities explicitly opt-in. Verified Codex 0.154.0 native no-prompt `SessionEnd` delivery with native trust approval; queue execution remains opt-in and was not exercised live.
- [x] **SUP-014 — Attach to existing sessions:** Added executable read-only discovery and bounded attachment for Prime Agent, OpenCode, and existing Codex hook journals. Reports distinguish partial history, live versus local-only observation, and disabled controls. Native no-prompt canaries preserved the selected Prime/OpenCode sessions; see [existing-session attachment](existing-session-attachment.md).
- [x] **SUP-015 — Roll out automatic intervention safely:** Added default observe-only, advisory, approval-required, and allowlisted automatic policies through the executable `foreman supervise` command. Exact approval, fresh observations, and durable no-retry claims gate delivery. Current native capabilities still require approval; approved no-prompt Prime CLI control and Prime/OpenCode observe-only canaries passed. See [supervision rollout](supervision-rollout.md).
- [x] **SUP-016 — Document and verify the control plane:** Published the [operator guide](supervision-operator-guide.md), [capability and privacy reference](supervision-reference.md), and [dated version-pinned verification](supervision-verification.md). Four native checks passed; documentation tests guard capabilities, pins, CLI flags, and recovery coverage. Model checkpoint provenance is a configured identity, not per-response weight attestation; deployment prerequisites and unverified controls are explicit.

## Execution order

Implement the remaining tasks in numeric order. SUP-011 (Pi) and SUP-013 (Claude Code) were removed from this roadmap at user request; keep existing task IDs unchanged. A task is complete only when its focused tests, adapter conformance checks, Ruff, and `git diff --check` pass. Provider tasks also require a live canary against the installed CLI version. Preserve unrelated worktree changes and do not push without explicit approval.
