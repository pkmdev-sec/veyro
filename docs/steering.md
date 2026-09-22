# Internal factory App Server steering

This is an internal experimental factory feature with explicit
`VEYRO_CODEX_BACKEND=app-server` opt-in. It is not exposed as a top-level factory command and is
not the existing-session approval protocol. Its observations can include task text and worker output.

Veyro can send localjev-informed guidance into an active Codex turn before resorting to termination.
The feature uses Codex App Server because the non-interactive `codex exec` transport has no channel
for additional input during a turn.

## Decision flow

1. Veyro builds the same bounded factory observation used for every assessment.
2. localjev asks Qwen3-14B to score `worker_stuck`, `work_off_track`, `meaningful_progress`, and the other dimensions.
3. The deterministic policy checks safety limits and the worker's steering history.
4. The first stuck or off-track result at the configured threshold selects `STEER_WORKER`.
5. Veyro translates the scores into a bounded instruction and calls App Server `turn/steer` with
   the recorded thread and turn identifiers.
6. The worker receives a grace period. If the warning remains high afterward, policy stops it and
   uses the existing retry path.

localjev does not directly write the steering prompt or control the process. It supplies probabilities;
ordinary Python selects an allowed action and deterministically formats the guidance.

## State and observability

Each worker records its Codex thread ID, turn ID, steering count, last steering time, and steering
history. Successful and rejected attempts become `WORKER_STEERED` or `WORKER_STEER_FAILED` events.
The internal event timeline records accepted guidance and persisted steering events. No supported
root command exposes the legacy factory timeline.

## Configuration

The default backend is stable `exec`, which has no active-turn steering channel.
Set `VEYRO_CODEX_BACKEND=app-server` to opt into experimental App Server controls.
With that backend, steering is enabled, one attempt is allowed per worker, and the grace
period is 30 seconds. Disable steering with `VEYRO_STEERING_ENABLED=false`.
The [Codex hooks bridge](codex-hooks-bridge.md) never opens an App Server connection.

## Boundaries

- App Server is currently experimental and its protocol may evolve.
- A successful `turn/steer` response means Codex accepted the input, not that it followed it.
- Veyro suppresses repeated guidance with per-worker limits and a grace period.
- Human need and hard iteration limits still outrank steering.
- Stop, retry, timeouts, and escalation remain available when steering does not restore progress.
