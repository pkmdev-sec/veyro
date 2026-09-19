# Observe an existing native session

For current procedures and recovery, use the [operator guide](supervision-operator-guide.md).
For the dated cross-provider checks, see [verification results](supervision-verification.md).

`foreman sessions` discovers metadata. `foreman attach` connects a read-only
observer. Neither command starts or resumes a native session, sends a prompt,
or enables controls. Run the native agent separately with its normal interface.
These commands do not call the semantic supervisor or change its authority.
Pinned LocalJev/Qwen3 remains the sole authoritative semantic supervisor.

## Provider support

| Provider | Discovery | Observation | History |
| --- | --- | --- | --- |
| Prime Agent `0.9.5` | Loaded sessions on an explicit daemon | Live structured daemon events | Snapshot counts and replay status only; no transcript replay requested |
| OpenCode `1.18.30` | Sessions on an explicit authenticated server | Live SSE after connection | No history or replay fetched |
| Codex hooks `0.154.0` | Existing private Foreman hook journals | Local reads and optional follow | Local receipt order, not complete native history |
| Pi, Claude Code | Outside the supervision roadmap | None | None |

A Codex journal does not prove Codex or its listener is running. Native liveness
stays `unknown`, including after a recorded `SessionEnd`. An OpenCode record does
not prove a native TUI is attached. Prime `loaded` means its daemon lists the session.

## Discover and select

Use an exact repository and endpoint. Discovery emits one JSON document.
Copy a `selector` from its `sessions` array into `--session`.

```sh
foreman sessions --agent prime-agent --repo /path/to/repo \
  --socket /path/to/existing/daemon.sock

foreman sessions --agent opencode --repo /path/to/repo \
  --server http://127.0.0.1:4096

foreman sessions --agent codex --repo /path/to/repo
```

OpenCode requires `OPENCODE_SERVER_PASSWORD` in the environment.
`OPENCODE_SERVER_USERNAME` defaults to `opencode`. Use the existing server's
credentials. Do not put credentials in the URL or command arguments.
Only numeric-loopback HTTP endpoints are accepted; redirects are refused.
Loopback credentials never go through environment-configured HTTP proxies.
Prime requires a current-user-owned private Unix socket and the pinned daemon
protocol `7`, schema revision `29`, and schema identity.

Discovery omits titles, prompts, summaries, transcripts, native configuration,
and credentials. `--limit` defaults to 100 and accepts 1–1000. `truncated` means
more entries may exist; `skipped` counts inspected entries that failed validation.
Codex scans at most 1000 directory entries and returns a sorted subset, not a
pagination cursor or a guarantee of global ordering.

## Attach without control

```sh
foreman attach --agent prime-agent --repo /path/to/repo \
  --socket /path/to/existing/daemon.sock --session ACTIVE_ID --watch-seconds 30

foreman attach --agent opencode --repo /path/to/repo \
  --server http://127.0.0.1:4096 --session SESSION_ID --watch-seconds 30

foreman attach --agent codex --repo /path/to/repo \
  --session FOREMAN_JOURNAL_ID --after-sequence 25 --watch-seconds 30
```

The command emits newline-delimited JSON:

1. `attachment`: identity, provider capabilities, history limits, observation mode,
   and `controls_enabled: false`.
2. `event`: normalized metadata records, when available.
3. `attachment_end`: event count, last emitted sequence, stop reason, and whether
   the local reader observed an incomplete final record.

Provider capabilities describe the adapter, not authorization to use controls.
This command exposes no control channel, even when the adapter supports queueing,
interruption, steering, or stop. It never changes a recorded queue opt-in.

Without `--watch-seconds`, Prime and OpenCode connect, report, and detach. Codex
reads existing journal records. A positive duration follows metadata for at most
300 seconds. `--max-events` defaults to 100 and accepts 1–1000. Connection and
cleanup have separate timeouts. Output goes only to stdout; no new journal is
created. Protect redirected output: session IDs and repository paths are metadata,
not anonymous data.

Prime and OpenCode use a fresh Foreman attachment ID. Their local output sequence
is not a resumable native cursor. Codex alone accepts `--after-sequence`, validated
from the start of its local journal, with a five-second cursor scan budget and
cancellation points between bounded pages. A cursor beyond complete local records is
rejected. Partial trailing records are withheld until their newline arrives.
Missing native events cannot be reconstructed.

All reports set `native_history_complete: false`. An event limit, deadline, or
clean detach does not imply task completion. Source failures produce an error
record and exit code 2. Ctrl-C detaches the observer, not the native session.
Native TUIs, commands, configuration, and credentials remain intact.

## Read-only storage checks

Codex discovery never opens native transcripts. It reads only existing Foreman
`identity.json` and `events.jsonl` files. It does not create storage, change
permissions, acquire writer locks, or open token, capability, or control files.

Broker-owned paths must be private and current-user-owned. The reader rejects
symlinks, hard-linked files, nonregular files, mismatched identities, noncontiguous
sequences, unsupported versions, and nonmetadata records. Observed replacement
or truncation closes the reader. A truncate-and-regrow operation completed between
reads cannot be detected without rereading prior bytes. Access times can change;
bytes, permissions, and modification times are not changed.

## Native verification

For a selected existing Prime session:

```sh
python -m foreman.supervision.attachment_canary --agent prime-agent \
  --repo /path/to/repo --socket /path/to/existing/daemon.sock --session ACTIVE_ID
```

For an isolated OpenCode fixture:

```sh
python -m foreman.supervision.opencode_canary --attachment
```

The second canary starts a disposable authenticated server and creates one empty
session **before** testing discovery and attachment. HOME and XDG storage are
isolated. It sends no model prompt, closes its server, and removes temporary
storage. The attachment command itself never creates a session.

Both canaries drive the actual Foreman executable, then rediscover the original
session after detaching. They test read-only connection and session preservation,
not model execution, control delivery, or complete native event coverage.

### Verification evidence

The executable canaries passed against Prime Agent `0.9.5` and OpenCode `1.18.30`.
Both rediscovered the selected native session after detach, with no prompts or
controls. The OpenCode fixture used isolated HOME/XDG storage and its port closed
after cleanup. Codex executable tests used existing hook journals and verified
unchanged bytes, permissions, and modification times.

The full test run also exposed the existing
`test_noisy_events_are_coalesced` timing race (100 events instead of 50). The same
failure was reproduced from committed `43f33c2` in an isolated baseline tree.
No scheduler code or timing thresholds were changed for this task.
