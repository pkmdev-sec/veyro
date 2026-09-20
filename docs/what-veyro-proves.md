# What the evidence establishes

Veyro's checks cover software contracts and specific native integrations. They do
not establish that Qwen3-14B is calibrated for arbitrary software tasks or that
supervision improves task success, cost, latency, or safety.

## Evidence by interface

| Check | Establishes | Does not establish |
| --- | --- | --- |
| Contract and policy tests | Typed reduction, capability checks, approval binding, rejection, claim retention | A real provider accepted a control |
| localjev synthetic example | Qwen3-14B deployment connectivity and valid checkpoint score shape | Calibration, general accuracy, per-response weight identity |
| Prime read-only canary | Existing-session observation without prompts or controls | Complete history or access to another client's session |
| Approved disposable Prime stop | CLI approval input, one durable claim, and a later terminal event | Automatic control, active-turn steering, or task completion |
| OpenCode observation canary | Authenticated HTTP/SSE and preservation of an empty fixture session | Live follow-up, approval reply, or interrupt delivery |
| Codex native hook canary | Native-approved `SessionEnd` to a metadata helper | Live queue execution or synchronous tool authorization |
| Codex helper-process tests | Helper publication to the authenticated broker | Native hook loading in every installation |
| Factory simulation | Worker/model orchestration and local persistence | Live model quality or agent isolation |

Use [verification procedures](supervision-verification.md) to reproduce each check.
The linked JSON reports contain dated observations and explicit limits. A report
from one pinned installation is not a compatibility claim for another version.

## Model identity and privacy

The configured assessor is `localjev-qwen3-14b` with Ollama tag `qwen3:14b`.
Readiness and the installed digest check deployment configuration. They do not
attest the weights behind every response. Model-generated probabilities remain
uncertain even when their JSON shape is valid.

The existing-session path records normalized metadata and digests, not native
transcripts or tool contents. Paths and IDs can still be sensitive. The factory
loop has a separate content-retention contract. Neither interface isolates code
running as the same OS user.

## Known limits

- No pinned native adapter qualifies for automatic delivery.
- The CLI evaluates one proposal and does not silently refresh stale evidence.
- At-most-once reservation is not exactly-once native execution.
- Native history is incomplete; a clean exit is not proof of task completion.
- The factory test `test_noisy_events_are_coalesced` has a reproduced timing race.
  Report it; do not hide it with exclusions or changed thresholds.

To evaluate model quality, use representative tasks, independent outcome labels,
calibration measurements, and a no-supervision baseline. Veyro does not publish
an accuracy or performance claim without that evidence.
