# Getting Started

This guide walks through bringing up a single-user `lex-align`
deployment and wiring a project into it. Both halves install from
PyPI — there is nothing to clone.

## 1. Bring up the server

`lex-align-server init` materializes the docker-compose stack,
Dockerfile, registry example, and env template into a directory you
choose. The bundled `Dockerfile` pulls `lex-align[server]` from PyPI,
so a single `docker compose up -d` is enough.

```bash
pip install "lex-align[server]"
lex-align-server init                              # writes ./lexalign/
cd lexalign
$EDITOR registry.yml                               # tune package policies (optional)
lex-align-server registry compile registry.yml registry.json
docker compose up -d
lex-align-server selftest                          # GETs /api/v1/health
```

The server binds to `127.0.0.1:8765` and Redis is internal to the
compose network. Single-user mode (`AUTH_ENABLED=false`) is the
default; edit `.env` (copied from `.env.example`) to flip to
organization mode.

!!! warning "One-shot command"
    `lex-align-server init` is meant to be run **once** per server
    deployment. If `.lexalign-server.toml` already exists in the target
    directory, the command refuses unless `--force` is passed.

### What `init` writes

| File | Purpose |
|---|---|
| `Dockerfile` | Pulls `lex-align[server]` from PyPI, pinned to the version that ran `init`. |
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

```bash
pip install "lex-align"
# or, with uv:
uv add --dev "lex-align"
```

## 3. Initialize a project

```bash
cd /path/to/your/project
lex-align-client init
```

`init` writes `.lexalign.toml`, installs the Claude Code session hooks
under `.claude/settings.json`, and adds a git pre-commit shim under
`.git/hooks/pre-commit`.

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
- The [For Agents](for-agents.md) page is the concise playbook for AI
  coding agents working in a `lex-align`-governed repo.
- See the project [README](https://github.com/dlfelps/lex-align#readme)
  for deployment modes and the registry schema.
