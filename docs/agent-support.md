---
title: Agent support
description: Which lex-align features work with which AI coding agents. Claude Code is the primary, first-class target; Cursor and Aider are second-class.
---

# Agent support

`lex-align` is built primarily around **Claude Code**, but the
guardrail that matters most — the **git pre-commit hook** — is a
plain Git hook that fires for any agent (or human) committing to a
governed repo. Other agents trade away the soft, edit-time guardrails
but keep the hard one.

## Support matrix

| Capability | [Claude Code][cc] | [Cursor][cursor] | [Aider][aider] |
|---|:---:|:---:|:---:|
| **Git pre-commit guardrail** (hard block on `DENIED`) | :material-check-circle: first-class | :material-check-circle: first-class | :material-check-circle: first-class |
| **`lex-align-client check` / `request-approval` CLI** (works from any shell) | :material-check-circle: | :material-check-circle: | :material-check-circle: |
| **Edit-time `pyproject.toml` intercept** (block before bytes hit disk) | :material-check-circle: via `.claude/settings.json` `PreToolUse` | :material-close-circle: no equivalent hook | :material-close-circle: no equivalent hook |
| **Auto-prompted to run `check` before adding a dep** | :material-check-circle: via `CLAUDE.md` (written by `lex-align-client init`) | :material-alert-circle-outline: user-provided `.cursorrules` | :material-alert-circle-outline: user-provided `CONVENTIONS.md` |
| **Auto-installed by `lex-align-client init`** | :material-check-circle: hooks + `CLAUDE.md` | :material-close-circle: bring your own rules file | :material-close-circle: bring your own conventions file |

[cc]: https://claude.com/claude-code
[cursor]: https://cursor.com/
[aider]: https://aider.chat/

## Reading the matrix

There are two layers of protection:

- **Hard guardrail — the pre-commit hook.** A vanilla `.git/hooks/pre-commit`
  shim that re-checks every runtime dep at commit time. It runs no matter
  who or what triggered the commit, so a denied package is blocked even
  if the agent never called `check` itself. This is the layer you can
  rely on across all agents.
- **Soft guardrails — plan-time advisor + edit-time intercept.** These
  catch bad packages *before* they ever reach the staging area, so the
  agent can self-correct without rolling back. They are wired up
  natively for Claude Code and require manual setup for others.

If you only have the pre-commit hook, the worst case is that an agent
writes a denied dependency into `pyproject.toml`, tries to commit, gets
blocked, and has to back out the edit. With the soft guardrails on top,
that round-trip never happens.

## Per-agent notes

### Claude Code (primary, first-class)

`lex-align-client init` configures everything automatically:

- Writes the `lex-align` section into `CLAUDE.md` so every Claude Code
  session knows to call `check` and `request-approval` with the right
  flags.
- Wires three Claude Code hooks into `.claude/settings.json`:
  `SessionStart` prints a session brief (server URL, agent identity,
  dep count) at the start of every session; `PreToolUse` on
  `Edit|Write|MultiEdit` intercepts every `pyproject.toml` edit and
  either allows, advises, or hard-blocks based on the server's verdict
  — *before* the file is written; `SessionEnd` is reserved for future
  use.
- Installs the standard git pre-commit hook.

This is the configuration the project is tested against. The
[For Agents](for-agents.md) page is written to be loaded into a Claude
Code session.

### Cursor

You get the pre-commit guardrail and the CLI commands. To match
Claude Code's auto-prompted advisor, drop a rules file at
`.cursor/rules/lex-align.mdc` (or the legacy `.cursorrules`) with the
same contract that lives in [For Agents](for-agents.md): always run
`lex-align-client check --package <name>` before editing
`pyproject.toml`, never use `--no-verify`, etc.

There is no Cursor equivalent of the `PreToolUse` intercept — Cursor
doesn't expose a hook that can block a tool call before it runs — so
denied packages are caught at commit time rather than edit time.

### Aider

Same shape as Cursor. The pre-commit guardrail and CLI commands
work without any setup. For auto-prompting, point Aider at a
`CONVENTIONS.md` (via `--read CONVENTIONS.md` or the `read:` key in
`.aider.conf.yml`) that mirrors the [For Agents](for-agents.md)
playbook.

Aider runs `git commit` on its own after each successful edit, so the
pre-commit hook is the natural backstop: a denied dep simply causes
the commit to fail, and Aider will see the error in its loop.

## What about other agents?

Anything that ultimately invokes `git commit` — Codex CLI, Windsurf,
GitHub Copilot in VS Code, Cline, an internal harness, a human at the
terminal — gets the **pre-commit guardrail and the CLI commands** for
free. The soft guardrails follow the same pattern as Cursor and Aider:
add the [For Agents](for-agents.md) playbook to whatever rules file
that agent reads, and you have plan-time parity with Claude Code on
everything except the edit-time intercept.

The edit-time intercept is intentionally Claude-Code-specific because
it depends on Claude Code's `PreToolUse` hook protocol; broader
coverage (e.g. via MCP) is not on the current roadmap.
