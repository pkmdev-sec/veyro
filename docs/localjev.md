# Deploy localjev with Qwen3-14B

Veyro uses [localjev](https://github.com/githubnext/localjev) as its local decision
API. localjev translates typed questions into an inference request to Ollama's
`qwen3:14b`, validates the response, and returns Jev-compatible scores.
The existing-session control plane accepts only the configured
`localjev-qwen3-14b` assessor. A missing or failed assessor does not select a fallback.

## Deployment contract

| Component | Required setting |
| --- | --- |
| Veyro endpoint | `http://127.0.0.1:8080` |
| localjev API | `POST /v1/systemone`, `GET /health`, `GET /ready` |
| SDK model alias | `jev-latest` |
| Ollama endpoint | `http://127.0.0.1:11434` |
| Ollama model tag | `qwen3:14b` |
| Recorded weights | Qwen3-14B, 14.8B parameters, `Q4_K_M`, GGUF |
| Checkpoint | `qwen3:14b@sha256:bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8` |

The [baseline manifest](../config/baselines/localjev-qwen3-14b.json) records the
service revision, model digest, inference limits, and client settings. It is a
record of a tested deployment, not an installer or a model-weight attestation.

## Prepare compatible services

You need Python 3.11+ for Veyro, Bun for localjev, and an Ollama installation with
hardware that can serve `qwen3:14b`. Veyro does not bundle these services or weights.

With your Ollama service running, obtain the model through its normal interface:

```sh
ollama pull qwen3:14b
```

Tags can change. Verify the installed digest below before relying on the captured
baseline. Do not rewrite the configured digest merely to accept a different model.

Use an operator-reviewed localjev checkout with Qwen-compatible non-thinking JSON
inference. The tested deployment sets `chat_template_kwargs.enable_thinking=false`
and `reasoning_effort=none`. Public localjev defaults target DiffusionGemma, not
this model. **The recorded service revision
`0a2d1b889ce1a056e13feddce8fd04532c116d78` is not currently fetchable from public
upstream.** Do not assume that cloning upstream reproduces the tested deployment.
Review your build's Qwen support, run its own tests, then run Veyro's live example.
Publishing or installing a different localjev build is outside Veyro's installer.

In that reviewed localjev checkout, configure the service with these exact names:

```sh
export LOCALJEV_HOST=127.0.0.1
export LOCALJEV_PORT=8080
export LOCALJEV_UPSTREAM=http://127.0.0.1:11434
export LOCALJEV_UPSTREAM_MODEL=qwen3:14b
export LOCALJEV_TIMEOUT=180
export LOCALJEV_MAX_INFLIGHT=2
export LOCALJEV_MAX_QUEUE=64
export LOCALJEV_MALFORMED_RETRIES=2
export LOCALJEV_MAX_OUTPUT_TOKENS=2048
export LOCALJEV_QUESTIONS_PER_CALL=16
export LOCALJEV_OUTCOMES_PER_CALL=128
bun install --frozen-lockfile
bun run start
```

These are localjev settings, not Veyro settings. Preserve any upstream credentials
and deployment controls. Do not expose the service on a public interface or disable
an existing authentication policy to make an example pass.

## Check readiness and the installed tag

From the Veyro checkout:

```sh
curl --noproxy '*' --fail --silent --show-error --max-time 5 \
  http://127.0.0.1:8080/ready

curl --noproxy '*' --fail --silent --show-error --max-time 5 \
  http://127.0.0.1:11434/api/tags | .venv/bin/python -c '
import json, sys
models = json.load(sys.stdin)["models"]
print(json.dumps([
    {"name": m["name"], "digest": m["digest"]}
    for m in models if m["name"] == "qwen3:14b"
]))'
```

Require `status: ready`, `upstream_model: qwen3:14b`, and exactly one matching tag
with digest `bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8`.
These GET requests do not run inference. Empty inventory or a different digest is
not a successful check. Confirm the effective upstream configuration with the
service owner; readiness alone cannot establish that trust.

Then send only the checked-in synthetic fixture:

```sh
.venv/bin/python examples/assess_localjev.py --live
```

The [example guide](../examples/README.md) explains its output. A successful call
establishes API connectivity, valid score shape, and configured provenance. It does
not establish model calibration, task correctness, or per-response weight identity.
Do not connect a new deployment to consequential controls based on this one example.

## Which settings Veyro reads

`veyro supervise` and the example use the same pinned assessor constructor in
`src/veyro/supervision/supervisor.py`. They use `jev-latest`, fixed loopback port
8080, a 120-second assessment timeout, and a 50,000-character state limit.
They do not use `.env` to select an alternate assessor or change the checkpoint.

The current control-plane client sends a fixed SDK placeholder bearer value,
`loopback-localjev`. It does not accept an operator-supplied localjev API key.
A service requiring a different key is incompatible with this CLI. Keep that
service's authentication enabled and use read-only observation instead; do not
replace a real secret with the placeholder. Loopback access is not isolation from
other programs running as the same OS user.

The separate `veyro run` factory loads the `VEYRO_JEV_*` settings in
[`.env.example`](../.env.example), including `VEYRO_JEV_API_KEY_ENV`. Its default
provider is also localjev/Qwen3-14B, but it has a separate runtime and privacy
contract. Do not interpret those environment settings as control-plane overrides.

## Interpret the scores

localjev generates probability estimates with a chat model. They are not direct
logit measurements and have not been calibrated for arbitrary software tasks.
Veyro validates their shape and range, then applies deterministic authorization.
A score never substitutes for native permissions, exact human approval, or fresh
evidence. See [typed assessment](why-jev.md) and [evidence limits](what-veyro-proves.md).
