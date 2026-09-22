# Contributing to Veyro

Veyro keeps native coding agents in their own terminals and permission systems. It adds local assessment, deterministic policy, executable checks, and explicit approval boundaries. Contributions must preserve those boundaries.

## Set up a development checkout

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/pkmdev-sec/veyro.git
cd veyro
uv sync --frozen --extra dev
uv run veyro agents
```

`veyro agents` inspects local provider commands. It does not start an agent or inference service.

## Make a change

1. Create a focused branch from `main`.
2. Keep model output advisory. Executable checks, native permissions, policy, and exact human approval remain authoritative.
3. Update public documentation when a command, capability, model role, or trust boundary changes.
4. Add a regression test when a plausible future bug could break observable behavior.
5. Run the relevant focused checks, then the full verification set.

```sh
uv run ruff check src tests tools examples
uv run pytest -q
uv run python tools/generate_header_logo.py --check
uv run --script tools/generate_supervision_diagram.py --check
rm -rf dist && uv build --out-dir dist
uv run python tools/check_release.py dist/*.whl dist/*.tar.gz
```

## Open a pull request

A pull request should explain:

- the problem and resulting behavior;
- any change to privacy, permissions, control delivery, or model authority;
- the exact commands used to verify the change;
- gates that remain open or evidence that did not improve.

Do not present passing tests, model scores, or artifact checks as production qualification. [`GATES.md`](GATES.md) and [`release-status.json`](release-status.json) are the authoritative status records.

For defects, use the [bug report form](https://github.com/pkmdev-sec/veyro/issues/new?template=bug_report.yml). For security issues, follow [`SECURITY.md`](SECURITY.md) instead of opening a public issue.
