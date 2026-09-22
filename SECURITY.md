# Security policy

## Supported versions

Veyro is pre-release software. Security fixes target the latest commit on `main`. Older commits and locally modified builds are not supported.

The repository's supported commands are listed in [`release-status.json`](release-status.json). `production_qualified: false` is intentional while G4 and G6 remain open.

## Report a vulnerability

Use GitHub's private vulnerability reporting flow:

1. Open the repository's **Security** tab.
2. Select **Report a vulnerability**.
3. Include the affected commit or version, reproduction steps, impact, and any known mitigation.

Do not include credentials, private source code, model prompts, or live session transcripts unless they are required to reproduce the issue. Redact tokens and operator paths.

Please do not open a public issue for an undisclosed vulnerability. We will coordinate investigation and disclosure through the private report.

## Security boundaries

Veyro does not replace native agent permissions. Model output cannot grant control authority. Approved controls require deterministic policy, request-bound human approval, fresh observations, and a durable delivery claim. Read [`docs/supervision-reference.md`](docs/supervision-reference.md) for the complete privacy and control boundary.
