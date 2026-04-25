## lex-align (v2.2)

This repo is governed by lex-align — every dependency change is checked
against the centrally-managed registry, the OSV CVE feed, and a license
policy. The server is the source of truth; the client is a thin CLI and a
set of hooks.

### Before adding or bumping a runtime dependency

Run:

```
lex-align-client check --package <name> [--version <v>]
```

The verdict will be one of:

* `ALLOWED` — proceed.
* `PROVISIONALLY_ALLOWED` — proceed, then run
  `lex-align-client request-approval --package <name> --rationale "<why>"`
  to enqueue formal addition to the registry. Do not wait for review.
* `DENIED` — do not add the package. The `reason` field explains whether
  it was the registry, a critical CVE, or the license. If a `replacement`
  is provided, prefer it.

### Hard guarantees

* The git **pre-commit hook** re-checks every runtime dep on every commit,
  so a freshly-published critical CVE on an already-installed package will
  block the commit and force you to replan.
* The Claude Code **PreToolUse** hook intercepts every edit to
  `pyproject.toml` and applies the same logic before the bytes hit disk.

If a check returns DENIED, do not bypass the hook. Replace the package or
choose a different version.

### IMPORTANT for AI agents

* `lex-align-client check` and `request-approval` are non-interactive when
  given all required flags — never call them without explicit `--package`
  and (for `request-approval`) `--rationale`.
* `lex-align-client init` is a one-shot setup command. If `.lexalign.toml`
  is already present, do not re-run it; the project is already configured.
