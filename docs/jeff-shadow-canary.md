# Run the jeff shadow canary

This procedure installs the pinned GLiFormer weight file and starts jeff as a local,
authenticated shadow provider. jeff remains optional and non-authoritative. LocalJev with
`qwen3:14b` remains the only provider that can affect Veyro policy decisions.

## Pinned artifacts

| Artifact | Required identity |
|---|---|
| jeff | commit `230d85d29e5df3454f7eb1aad374da01808191a2` |
| GLiFormer | `knowledgator/gliformer-large-v1@d0a4e53d09cebe6bc963dd9be319d4279084bb2d` |
| `pytorch_model.bin` | 2,302,735,855 bytes |
| `pytorch_model.bin` SHA-256 | `f80b29199d66f878669f283703e4dba9fd726755dcc20aba1ed0d24fce4a23f1` |

The full canary record is in
[`config/baselines/jeff-gliformer-large-v1-shadow.json`](../config/baselines/jeff-gliformer-large-v1-shadow.json).

## 1. Acquire the weight file

Use an approved internal artifact repository or a security-approved file transfer. Do not disable
TLS verification, bypass network security controls, or put a credential in a URL.
If a download returns HTML, a truncated object, or the wrong digest, stop. The
recorded baseline did not obtain the weights and does not claim live inference.

The import command accepts either a local file or an approved HTTPS URL. It streams the source into
a temporary file in the model directory, rejects HTML and size or digest mismatches, then publishes
the verified file atomically without overwriting a destination that appears concurrently. It
refuses to replace any existing invalid file.

From an approved local transfer:

```console
$ veyro import-jeff-weights --source /approved/path/pytorch_model.bin
```

From an approved internal HTTPS artifact service:

```console
$ veyro import-jeff-weights \
    --source https://artifacts.example.internal/gliformer-large-v1/pytorch_model.bin \
    --ca-bundle ~/.config/huggingface/ca-bundle.pem
```

Success must report this exact identity:

```text
Bytes: 2302735855
SHA-256: f80b29199d66f878669f283703e4dba9fd726755dcc20aba1ed0d24fce4a23f1
```

Running the same command again is safe. It verifies the installed file and reports
`Already verified`. If an invalid destination exists, inspect and remove it manually before a new
import. The command never overwrites it.

## 2. Prepare the pinned jeff checkout

Use an approved checkout of `logan-markewich/jeff`. Verify the source before installing its locked
dependencies:

```bash
JEFF_DIR=/approved/path/to/jeff
test "$(git -C "$JEFF_DIR" rev-parse HEAD)" = \
  230d85d29e5df3454f7eb1aad374da01808191a2
test -z "$(git -C "$JEFF_DIR" status --porcelain)"
uv sync --project "$JEFF_DIR" --frozen
```

Stop if either `test` command fails. A different commit or a dirty checkout is not the pinned
canary.

## 3. Start the loopback-only authenticated service

The commands in this section target macOS and the supplied MPS configuration.
On another platform, use its approved secret manager and validate a separate
backend configuration; do not assume the macOS baseline applies.

Create a fresh key in macOS Keychain. Do not save it in the repository or shell history:

```bash
JEFF_KEYCHAIN_SERVICE=veyro-jeff-shadow
security add-generic-password -U -a "$USER" -s "$JEFF_KEYCHAIN_SERVICE" \
  -w "$(openssl rand -hex 32)"
JEFF_API_KEYS="$(security find-generic-password \
  -a "$USER" -s "$JEFF_KEYCHAIN_SERVICE" -w)"
export JEFF_API_KEYS
set -a
. config/canaries/jeff-gliformer-large-v1.env
set +a
uv run --project "$JEFF_DIR" jeff
```

The pinned settings bind jeff to `127.0.0.1:8081`, require the separately supplied bearer key, use
MPS with float32 and eager attention, preserve the recorded GLiFormer calibration settings, and
enforce the 20,000-character state limit. Do not bind this canary to a non-loopback address.

## 4. Verify the live canary

In another shell, load the same key as the client key. Check health without authentication, then
check the authenticated model endpoint:

```bash
JEFF_KEYCHAIN_SERVICE=veyro-jeff-shadow
JEFF_API_KEY="$(security find-generic-password \
  -a "$USER" -s "$JEFF_KEYCHAIN_SERVICE" -w)"
export JEFF_API_KEY
curl --noproxy '*' --fail --silent --show-error http://127.0.0.1:8081/healthz
python - <<'PY'
import os
import urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:8081/v1/models",
    headers={"Authorization": f"Bearer {os.environ['JEFF_API_KEY']}"},
)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(request, timeout=10) as response:
    print(response.read().decode())
PY
```

Before enabling Veyro shadow traffic, send one live nine-question System One request and record
its provider, checkpoint, question version, and inference metadata. A successful health check alone
is not proof that model inference works.

## 5. Enable shadow traffic only after live verification

Set the shadow endpoint without changing the authoritative LocalJev settings:

```bash
export VEYRO_JEV_SHADOW_PROVIDER_ID=jeff-gliformer-large-v1
export VEYRO_JEV_SHADOW_BASE_URL=http://127.0.0.1:8081
export VEYRO_JEV_SHADOW_API_KEY_ENV=JEFF_API_KEY
export VEYRO_JEV_SHADOW_MODEL=jev-latest
export VEYRO_JEV_SHADOW_CHECKPOINT='knowledgator/gliformer-large-v1@d0a4e53d09cebe6bc963dd9be319d4279084bb2d#sha256:f80b29199d66f878669f283703e4dba9fd726755dcc20aba1ed0d24fce4a23f1'
export VEYRO_JEV_SHADOW_TIMEOUT_SECONDS=10
export VEYRO_JEV_SHADOW_MAX_STATE_CHARS=20000
export VEYRO_JEV_SHADOW_STATE_FORMAT=kv
```

Keep `VEYRO_JEV_BASE_URL` pointed at LocalJev. Shadow failures are recorded separately and must
not fail, steer, stop, route, escalate, or finish a Veyro run. Do not promote jeff until both
providers have run against the same labeled Veyro observations and the recorded promotion gates
pass.
