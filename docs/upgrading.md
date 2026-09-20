# Upgrade to Veyro 0.4

Version 0.4 is a breaking name change. The command and import package are `veyro`.
The distribution is `veyro-factory`. There are no old command or import aliases.

## Install the new package

Use a dedicated environment for this checkout:

```sh
uv venv --python 3.12
uv pip install --python .venv/bin/python --editable '.[dev]'
.venv/bin/veyro --help
```

If you reuse an environment, remove the previous distribution before installing
this one. Otherwise its obsolete console script or editable import path can remain.
Do not change another checkout's environment or global native-agent installation.

## Update callers and configuration

- Use `veyro` for commands, imports, and generated hook module paths.
- Use the `VEYRO_*` environment-variable names shown in `.env.example`.
  Rename configuration keys, not credential values. Keep private `.env` files out
  of Git. Old variable names are not fallback aliases.
- Update code identifiers, serialized keys such as `veyro_session_id`, event names,
  application bridge identifiers, and client headers to the new names.
- Defaults now use `.veyro/` for local state. This does not migrate an old journal
  or prove that an existing native session has been attached.

Application records still use protocol version `1.0` within the new namespace.
That number does not make old serialized fields or client identifiers compatible.
The package namespace and required field names form a breaking boundary.

Native provider IDs, versions, protocol/schema pins, model checkpoint digests,
and upstream vendor commands have not changed. LocalJev remains the sole configured
authoritative assessor. Its configured checkpoint label is not per-response weight
attestation. See the [deployment checks](supervision-verification.md#check-the-assessor-deployment).

## Keep existing state safe

Old private journals, connection files, hook commands, and serialized state are
not automatically upgraded. Do not rewrite their identities or replace historical
records to make them load. Preserve them for inspection and keep them ignored by
Git, including any legacy private directory still present in your checkout.
Add the existing private state directory to `.git/info/exclude` before updating.
Do not rely on the new default ignore rule to cover old state.

Existing native hooks may still name the previous Python module or executable.
Retire only the hook definitions you own. Generate the new definitions and use
native trust review before enabling a new listener. Do not bypass managed policy
or overwrite unrelated hooks, credentials, configuration, or native transcripts.

Keep the same authoritative delivery ledger for an existing delivery scope.
An explicit `--ledger-dir` does not change just because the package name changed.
Claims still bind repository, provider, native session, and command ID. Do not
move to an empty or cloned ledger, delete claims, or mint IDs to retry uncertainty.
A new package name is not permission to repeat a native control. The experimental
Codex queue seam also has private intent records; do not enable it on a fresh
journal for an old session until the operator reconciles all prior queue attempts.

Observe-only is still the default. No pinned native adapter qualifies for automatic
delivery. Start with read-only discovery and attachment, inspect the reported
history limits, and follow the [operator guide](supervision-operator-guide.md).

## Verify the renamed build

```sh
uv build --offline
.venv/bin/python tools/check_release.py \
  dist/veyro_factory-0.4.0-py3-none-any.whl dist/veyro_factory-0.4.0.tar.gz
.venv/bin/python -m pytest -q
```

The release checker verifies names, entry points, source inputs, and private-file
exclusions. `--retired-name NAME` also scans both archives for a previous name.
[Recorded 0.4 release verification](rename-verification.json) records the source digest,
test results, clean wheel installation and replay, a wheel rebuilt from the source
archive, and four new native canary results. Historical reports remain labeled as
pre-rename evidence. The pre-existing noisy-events timing failure is not hidden.
