## lex-align (v2.2)

This repo is governed by lex-align — every change to `[project].dependencies`
is checked against the centrally-managed registry, the OSV CVE feed, and a
license policy. Dev-only deps under `[dependency-groups]` and
`[project.optional-dependencies]` are out of scope. The server is the source
of truth; the client is a thin CLI and a set of hooks.

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

### Automatic enforcement

`lex-align-client init` wires three Claude Code hooks plus a git hook:

* **`SessionStart`** prints a session brief (server URL, mode, agent
  identity, dep count) at every Claude Code session start.
* **`PreToolUse`** on `Edit|Write|MultiEdit` intercepts every edit to
  `pyproject.toml` and re-runs the check before bytes hit disk; a `DENIED`
  verdict blocks the write.
* **`SessionEnd`** is reserved for future use.
* The **git pre-commit hook** re-checks every runtime dep on every commit,
  so a freshly-published critical CVE on an already-installed package will
  block the commit and force you to replan.

If a check returns DENIED, do not bypass the hook. Replace the package or
choose a different version. Never use `git commit --no-verify`.

### Agent identity

`check` and `request-approval` accept `--agent-model` and `--agent-version`,
which tag audit rows in the server's dashboard. They default to the
`LEXALIGN_AGENT_MODEL` and `LEXALIGN_AGENT_VERSION` environment variables.
The `SessionStart` hook auto-detects the Claude model and exports both
vars for the rest of the session.

### IMPORTANT for AI agents

* `lex-align-client check` and `request-approval` are non-interactive when
  given all required flags — never call them without explicit `--package`
  and (for `request-approval`) `--rationale`.
* `lex-align-client init` is a one-shot setup command. If `.lexalign.toml`
  is already present, do not re-run it; the project is already configured.
* `lex-align-client uninstall` removes the Claude hooks and the git
  pre-commit shim but preserves `.lexalign.toml`. Do not invoke it unless
  explicitly asked to disable governance.
