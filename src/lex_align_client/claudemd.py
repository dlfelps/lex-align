"""CLAUDE.md integration: write lex-align usage instructions into the project's CLAUDE.md."""

from __future__ import annotations

from pathlib import Path

_SECTION_HEADER = "## lex-align dependency governance"

_SECTION = """\
## lex-align dependency governance

This project uses **lex-align** to govern runtime dependencies. Every package
in `[project].dependencies` is checked against a central registry, the OSV
CVE feed, and a license policy before it may be added. Dev-only deps under
`[dependency-groups]` and `[project.optional-dependencies]` are out of scope.

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
  identity, dep count) so you know the project is governed before you
  touch anything.
* **`PreToolUse`** on `Edit|Write|MultiEdit` intercepts every edit to
  `pyproject.toml` and re-runs the registry/CVE/license check before the
  bytes hit disk. A `DENIED` verdict blocks the write.
* **`SessionEnd`** is reserved for future use.
* The **git pre-commit hook** re-checks every runtime dep on every commit —
  a freshly-published CVE on an already-installed package will block the
  commit even if nothing in the diff touched it.

If a check returns DENIED, do not bypass the hook. Replace the package or
choose a different version. Never use `git commit --no-verify`.

### Agent identity

`check` and `request-approval` accept `--agent-model` and `--agent-version`,
which tag audit rows in the server's dashboard. They default to the
`LEXALIGN_AGENT_MODEL` and `LEXALIGN_AGENT_VERSION` environment variables.
The `SessionStart` hook auto-detects the Claude model and exports both
vars for the rest of the session, so you usually do not need to set them
by hand.

### IMPORTANT for AI agents

* `lex-align-client check` and `request-approval` are non-interactive when
  given all required flags — never call them without explicit `--package`
  and (for `request-approval`) `--rationale`. There is no interactive
  fallback.
* `lex-align-client init` is a one-shot setup command. If `.lexalign.toml`
  is already present, do not re-run it; the project is already configured.
* `lex-align-client uninstall` removes the Claude hooks and the git
  pre-commit shim but leaves `.lexalign.toml` in place. Do not invoke it
  unless the user explicitly asks to disable governance.
"""


def install_claude_md(project_root: Path) -> tuple[Path, bool]:
    """Create CLAUDE.md or append the lex-align section if not already present.

    Returns (path, created_or_updated). Returns False for the second element
    when the section was already present and no write was performed.
    """
    path = project_root / "CLAUDE.md"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if _SECTION_HEADER in existing:
            return path, False
        path.write_text(existing.rstrip("\n") + "\n\n" + _SECTION + "\n", encoding="utf-8")
    else:
        path.write_text(_SECTION + "\n", encoding="utf-8")
    return path, True
