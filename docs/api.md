# API Reference

Docstrings on this page are pulled directly from the source under
`src/` by [mkdocstrings](https://mkdocstrings.github.io/). Edit the
docstring, rebuild, and the rendered page updates.

---

## `lex_align_client`

The thin client used by developers and AI agents. Provides the
`lex-align-client` CLI as well as the Claude Code and git pre-commit
hooks.

::: lex_align_client
    options:
      show_submodules: true

### CLI

::: lex_align_client.cli

### Server API client

::: lex_align_client.api

### Configuration

::: lex_align_client.config

### Settings

::: lex_align_client.settings

### `pyproject.toml` utilities

::: lex_align_client.pyproject_utils

### Pre-commit hook

::: lex_align_client.precommit

### Claude Code hooks

::: lex_align_client.claude_hooks

### `CLAUDE.md` rendering

::: lex_align_client.claudemd

---

## `lex_align_server`

The FastAPI service that owns the registry, runs license + CVE checks,
persists the audit log, and surfaces report endpoints.

::: lex_align_server
    options:
      show_submodules: true

### Application entrypoint

::: lex_align_server.main

### CLI

::: lex_align_server.cli

### Configuration

::: lex_align_server.config

### State management

::: lex_align_server.state

### Evaluation pipeline

::: lex_align_server.evaluate

### Registry

::: lex_align_server.registry

::: lex_align_server.registry_schema

### License checks

::: lex_align_server.licenses

### CVE checks (OSV)

::: lex_align_server.cve

### Audit log

::: lex_align_server.audit

### Caching

::: lex_align_server.cache

### Authentication

::: lex_align_server.auth
