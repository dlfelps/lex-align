# Getting Started

This guide walks through bringing up a single-user `lex-align`
deployment and wiring a project into it.

## 1. Run the server

```bash
cd docker
cp registry.example.yml registry.yml         # edit to taste
python ../tools/compile_registry.py registry.yml registry.json
docker compose up -d
```

The server binds to `127.0.0.1:8765` and Redis is internal to the
compose network. Single-user mode (`AUTH_ENABLED=false`) is the default.

```bash
curl http://127.0.0.1:8765/api/v1/health
```

## 2. Install the client

```bash
pip install "lex-align[client]"
# or, with uv:
uv add --dev "lex-align[client]"
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
- See the project [README](https://github.com/dlfelps/lex-align#readme)
  for deployment modes and the registry schema.
