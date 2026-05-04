# Single-user quickstart

The fastest path from `pip install lex-align` to a governed project,
on one laptop, with no Docker.

The single-user path is for evaluation, solo developers, and writing
docs about lex-align. It is fully functional: every gate (registry,
license, CVE) runs the same way, and the audit log is durable on disk.
What you give up by skipping Docker is the Redis cache (license/CVE
lookups hit upstream every time) and the multi-user defaults (auth,
non-loopback bind).

## 1. Install lex-align

```bash
pip install lex-align
# or, isolated, recommended for `lex-align-client`:
uv tool install lex-align
```

## 2. Start the server

```bash
lex-align-server quickstart
```

That command:

* lays down `~/.lexalign/registry.yml` (the bundled example you can edit later)
* lays down `~/.lexalign/lexalign.sqlite` (the audit DB)
* starts the FastAPI server in-process on `http://127.0.0.1:8765`
* skips Redis — the cache layer silently degrades

Stop with Ctrl-C. Re-running `quickstart` is idempotent: the registry
isn't overwritten unless you pass `--force`.

??? tip "Materialize without serving"
    `lex-align-server quickstart --no-serve` writes the bundle and
    prints the env vars you'd need to start the server manually
    later. Useful in CI or when you want to run the server under
    `systemd` / `launchctl`.

## 3. Initialize a project

In another terminal, point any Python project at the server:

```bash
cd /path/to/your/project
lex-align-client init --yes
```

`init --yes` is non-interactive: it writes `.lexalign.toml`, installs
the Claude Code hooks (`SessionStart`, `PreToolUse`, `SessionEnd`),
installs the git pre-commit shim, and creates or extends `CLAUDE.md`.

In single-user mode `init` also turns on **`auto_request_approval`**
(default `true`). When the `PreToolUse` hook intercepts an edit that
adds a `PROVISIONALLY_ALLOWED` package, the client immediately POSTs a
`request-approval` with an auto-generated rationale, so the user-as-
reviewer flow stays a single tool call from the agent's perspective.

## 4. Use it

| Action | Command |
|---|---|
| Vet a single package | `lex-align-client check --package httpx` |
| Vet the whole project | `lex-align-client audit` |
| One-screen overview | `lex-align-client status` |
| Manual approval request | `lex-align-client request-approval --package <p> --rationale "<why>"` |
| Pre-commit guardrail | runs automatically on every `git commit` |
| Claude Code hook | intercepts every edit to `pyproject.toml` |
| Tear down hooks | `lex-align-client uninstall` |

`audit` re-evaluates every dep currently in `[project].dependencies`
without needing a commit. It exits non-zero if any verdict is `DENIED`,
and is the recommended way to vet a project you're adopting:

```bash
$ lex-align-client audit
Audited 14 runtime deps for project 'myapp'.

  ALLOWED              : 12
  PROVISIONALLY_ALLOWED: 1
  DENIED               : 1

DENIED:
  ✗ requests — replaced by httpx
      use instead: httpx

PROVISIONALLY_ALLOWED:
  ◎ orjson — not in registry; license + CVE pass
  → run `lex-align-client request-approval --package <name> ...`
```

`status` is the at-a-glance dashboard for the terminal:

```bash
$ lex-align-client status
# lex-align status — project: myapp
  server_url           : http://127.0.0.1:8765
  mode                 : single-user
  auto_request_approval: on

Server: reachable — registry=loaded redis=down db=ok

Runtime dependencies in pyproject.toml: 14

Pending approvals for this project: 1
  · orjson

Recent CVE-driven denials: critical=0 high=0 medium=0 low=0

Hooks:
  Claude SessionStart    : installed
  Claude PreToolUse      : installed
  Claude SessionEnd      : installed
  git pre-commit         : installed
```

## 5. Editing the registry

`~/.lexalign/registry.yml` is the human-authored source of truth.
Edit it, then either restart the server (Ctrl-C and re-run
`quickstart`) or let the file-watch reloader pick it up — the polling
interval defaults to 5 minutes and is configurable via
`REGISTRY_RELOAD_INTERVAL`.

## 6. Background CVE re-scan

In addition to the per-`/evaluate` CVE check, the server runs an
in-process asyncio scheduler that walks every package in the live
registry on a fixed cadence and re-queries OSV. Anything whose max
CVSS now crosses `global_policies.cve_threshold` gets a `CVE_ALERT`
row in the audit log — the alert surfaces in the security dashboard
and in `lex-align-client status` automatically.

| Env var | Default | Behaviour |
| --- | --- | --- |
| `LEXALIGN_CVE_SCAN_INTERVAL_HOURS` | `24` | Cadence between scans, in hours. `0` disables the scheduler. |

The scanner is alert-only: it never auto-flips an approved package to
`banned`. Treat a `CVE_ALERT` the same as the
[hot-package triage steps in the security dashboard](dashboards.md#acting-on-a-hot-registry-package)
— pin to a safe version, ban with a `reason`, or replace.

## When you need more

The quickstart binds `127.0.0.1` and runs without authentication. If
you need multi-user access, persistent Redis caching, or a managed
service deployment, a Docker Compose stack is the next step. Migration
is straightforward: copy `~/.lexalign/registry.yml` into
`./lexalign/registry.yml` after `lex-align-server init`. The audit DB
is SQLite either way, so you can carry history across too.
