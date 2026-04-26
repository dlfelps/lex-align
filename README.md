<p align="center">
  <img src="./lex-align-logo.png" alt="lex-align" width="520">
</p>

<h1 align="center">lex-align</h1>

<p align="center">
  <a href="https://dlfelps.github.io/lex-align/">Docs</a> ·
  <a href="https://dlfelps.github.io/lex-align/getting-started/">Getting started</a> ·
  <a href="https://dlfelps.github.io/lex-align/agent-support/">Agent support</a> ·
  <a href="https://dlfelps.github.io/lex-align/for-agents/">For agents</a>
</p>

`lex-align` enforces your dependency policy before AI agents or developers can
commit it. Every package gets checked against your approved registry, OSV CVE
scores, and license rules — returning one of three deterministic verdicts so
agents can act without ambiguity. Full docs at
[dlfelps.github.io/lex-align](https://dlfelps.github.io/lex-align/).

---

## Why

Your AI coding agent adds packages to `pyproject.toml` faster than anyone can
review them. By the time legal notices the AGPL dep, or security spots the
critical CVE, it's already in `main`. You need the check to happen *before*
the bytes are written, not after the PR is open.

---

## How it works

A FastAPI server is the source of truth. The client is thin: a CLI plus hooks.

- **Three gates per check:** internal registry → OSV CVE → PyPI license.
- **Three-verdict vocabulary:** `ALLOWED`, `PROVISIONALLY_ALLOWED`, `DENIED` —
  small, deterministic, easy for an agent to branch on.
- **Two enforcement points:** a git `pre-commit` hook (universal backstop,
  fires for every agent and every human committing to a governed repo) and a
  Claude Code `PreToolUse` hook that intercepts `pyproject.toml` edits before
  the file is written.
- **Use first, approve in parallel:** an unknown package that passes CVE and
  license checks comes back `PROVISIONALLY_ALLOWED`. The agent uses it, calls
  `request-approval`, and keeps moving — formal review runs async.

Python and `pyproject.toml` only. Self-hosted via Docker Compose. Single-user
mode is the default.

---

## Quickstart

```bash
# Server (host you control)
pip install "lex-align[server]"
lex-align-server init && cd lexalign
lex-align-server registry compile registry.yml registry.json
docker compose up -d

# Client (per-project)
pip install lex-align
cd /path/to/your/project
lex-align-client init
lex-align-client check --package httpx
```

For server tuning, registry authoring, hook layout, and Claude Code wiring,
see the
[full Getting Started guide](https://dlfelps.github.io/lex-align/getting-started/).

---

## Agent support

Primary target is **Claude Code** — pre-commit hook, `PreToolUse` edit-time
intercept, and an auto-written `CLAUDE.md` so every session knows how to use
`check` and `request-approval`. Cursor, Aider, and anything else committing to
a governed repo are backstopped by the pre-commit hook. Full matrix:
[agent support](https://dlfelps.github.io/lex-align/agent-support/).

---

## Project status

| Phase | Status |
|---|---|
| **1.** Server core (registry, license, CVE, audit, evaluate) | ✅ shipped |
| **2.** Thin client (init, check, request-approval, pre-commit, Claude hooks) | ✅ shipped |
| **3.** Approval workflow UI + reporting endpoints | 🟡 stubbed |
| **4.** Dashboards, PR-creation workflow, org-mode auth | ⏸ deferred |

`request-approval` persists each submission today, but the reviewer UI and
the PR-creation workflow against the registry repo are not here yet. If you
need a polished triage dashboard, this isn't that tool — yet.

---

## Contributing

```bash
uv sync --all-extras --all-groups
uv run pytest
```

Tests live under `tests/client/` and `tests/server/`. PRs welcome — file
issues for bugs, license-policy edge cases, or agent-integration gaps.

---

## License

See [LICENSE](./LICENSE).
