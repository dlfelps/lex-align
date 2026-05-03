# Getting Started

This guide walks through bringing up a single-team `lex-align`
deployment and wiring a project into it. Both halves install from
PyPI as a single `lex-align` distribution — there is nothing to clone
and no extras to remember.

The default flow uses the `local_file` proposer: the server reads and
writes a single `registry.yml` on disk, and that file is the source of
truth. No tokens, no PR review, no webhooks. PR-based review is
[available for org-wide installs](git-backed-approvals.md) but not the
recommended starting point.

## 1. Bring up the server

`lex-align-server init` materializes the docker-compose stack,
Dockerfile, registry example, and env template into a directory you
choose. The bundled `Dockerfile` pulls `lex-align` from PyPI, so a
single `docker compose up -d` is enough.

```bash
pip install lex-align
lex-align-server init                              # writes ./lexalign/
cd lexalign
$EDITOR registry.yml                               # tune package policies (optional)
lex-align-server registry compile registry.yml registry.json
lex-align-server check-config                      # pre-flight: paths, cache, proposer
docker compose up -d
lex-align-server selftest                          # GETs /api/v1/health
```

The server binds to `127.0.0.1:8765` and Redis is internal to the
compose network. Single-user mode (`AUTH_ENABLED=false`) is the
default; edit `.env` (copied from `.env.example`) to flip to
organization mode.

!!! tip "`registry.yml` is durable across restarts"
    The `local_file` proposer writes atomically (temp-file + rename),
    so a server crash mid-write can never produce a corrupt YAML. On
    restart the server reads the same file from disk, picking up the
    most recent state with no in-memory drift.

### `lex-align-server check-config`

Before the first `docker compose up -d`, run `check-config` to
verify the install. It prints a one-line status for each of the
moving pieces a single-team install needs:

```
OK   REGISTRY_PATH   ./registry.yml exists (1842 bytes).
OK   registry YAML   ./registry.yml validates (12 package rules).
OK   audit DB        /var/lib/lexalign/lexalign.sqlite exists and is writable.
WRN  cache (redis)   unreachable at redis://localhost:6379/0; server will run without cache (license/CVE lookups hit upstream every time).
OK   proposer        local_file (auto-detected) — recommended for single-team installs.
OK   auth            disabled (anonymous), bound to 127.0.0.1 — fine for single-user / local evaluation.
```

Exit code is non-zero only on hard failures (missing
`REGISTRY_PATH`, an unparseable YAML, an unwritable audit
directory). Warnings — Redis unreachable, log-only fallback,
anonymous auth on a non-loopback bind — are surfaced for visibility
but don't block the run.

!!! warning "One-shot command"
    `lex-align-server init` is meant to be run **once** per server
    deployment. If `.lexalign-server.toml` already exists in the target
    directory, the command refuses unless `--force` is passed.

### What `init` writes

| File | Purpose |
|---|---|
| `Dockerfile` | Pulls `lex-align` from PyPI, pinned to the version that ran `init`. |
| `docker-compose.yml` | Server + Redis stack. Single-user mode by default. |
| `.env.example` | Copy to `.env`. Holds `AUTH_ENABLED`, `CVE_THRESHOLD`, etc. |
| `registry.yml` | Human-authored enterprise registry (starter content). Edit this. |
| `registry.json` | Compiled registry consumed by the server. |
| `README.md` | Operator quick-reference. |
| `.lexalign-server.toml` | Idempotency marker. |

### Updating the registry

```bash
$EDITOR registry.yml
lex-align-server registry compile registry.yml registry.json
docker compose restart lexalign-server
```

## 2. Install the client

The client is a CLI, so installing it into an isolated environment with
`uv tool install` (or `pipx`) keeps it off your project's dependency
graph:

```bash
uv tool install lex-align
# or:
pipx install lex-align
# or, plain pip:
pip install lex-align
```

## 3. Initialize a project

```bash
cd /path/to/your/project
lex-align-client init
```

`init` does four things in one shot:

- writes `.lexalign.toml` with the server URL and mode
- installs the Claude Code session hooks (`SessionStart`, `PreToolUse`
  on `Edit|Write|MultiEdit`, `SessionEnd`) into `.claude/settings.json`
- installs a git pre-commit shim at `.git/hooks/pre-commit`
- creates or extends `CLAUDE.md` with the agent contract

Flags worth knowing:

| Flag | Effect |
|---|---|
| `--yes` / `-y` | Accept defaults non-interactively (CI-friendly). |
| `--server-url URL` | Override the server URL (default `http://127.0.0.1:8765`). |
| `--project NAME` | Override the auto-detected project name. |
| `--mode {single-user,org}` | Pick the auth mode. Defaults to `single-user`. |
| `--no-claude-hooks` | Skip the Claude Code hook install. |
| `--no-precommit` | Skip the git pre-commit shim. |
| `--no-claude-md` | Skip the `CLAUDE.md` write. |

!!! warning "One-shot command"
    `lex-align-client init` is meant to be run **once** per project.
    If `.lexalign.toml` already exists, do not re-run it — the project
    is already configured.

## 4. Use it

| Action | Command |
|---|---|
| Plan-time advice | `lex-align-client check --package httpx` |
| Async approval | `lex-align-client request-approval --package httpx --rationale "standard async client"` |
| Pre-commit guardrail | runs automatically on every `git commit` |
| Claude Code hook | intercepts every edit to `pyproject.toml` |
| Tear down hooks | `lex-align-client uninstall` (preserves `.lexalign.toml`) |

Both hooks enforce against `[project].dependencies` only. Changes to
`[dependency-groups]` and `[project.optional-dependencies]` are not
checked — by design, since dev tooling does not ship to production.

### Agent identity

`check` and `request-approval` accept `--agent-model` and
`--agent-version` flags that tag audit rows in the server dashboard.
Both flags read the `LEXALIGN_AGENT_MODEL` and `LEXALIGN_AGENT_VERSION`
environment variables when not set explicitly, so exporting them once
in your shell or CI environment is usually enough. Inside Claude Code,
the `SessionStart` hook auto-detects the model and exports both
variables for the rest of the session.

### Org mode

If you ran `init --mode org`, export the API key before any `check`
or `request-approval` call. The variable name is whichever
`api_key_env_var` is set to in `.lexalign.toml` (default
`LEXALIGN_API_KEY`):

```bash
export LEXALIGN_API_KEY=...
lex-align-client check --package httpx
```

## 5. Reading verdicts

Every check returns one of three verdicts:

=== "ALLOWED"

    All gates passed. Add the dependency and move on.

=== "PROVISIONALLY_ALLOWED"

    Unknown to the registry but license + CVE passed. Add the
    dependency, then enqueue formal review:

    ```bash
    lex-align-client request-approval \
        --package <name> \
        --rationale "<why this package>"
    ```

=== "DENIED"

    Do **not** add the package. The `reason` field explains whether
    the registry, a critical CVE, or the license blocked it. If a
    `replacement` is provided, prefer it.

## Next

- Browse the [API Reference](api.md) for module-level docs.
- See [Agent Support](agent-support.md) for the per-agent matrix
  (Claude Code is first-class; Cursor and Aider get the pre-commit
  guardrail and the CLI commands but not the edit-time intercept).
- The [For Agents](for-agents.md) page is the concise playbook for AI
  coding agents working in a `lex-align`-governed repo.
- See the project [README](https://github.com/dlfelps/lex-align#readme)
  for deployment modes and the registry schema.
