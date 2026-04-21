---
alternatives:
- name: argparse
  outcome: not-chosen
  reason: stdlib but lacks built-in interactive prompts and confirmation dialogs;
    reimplementing click.prompt/click.confirm cleanly is non-trivial
  reversible: costly
- name: typer
  outcome: not-chosen
  reason: typer is built on Click but adds type-annotation auto-inference that provides
    no benefit here since prompts are used extensively and are more naturally expressed
    in Click's explicit style
  reversible: cheap
confidence: high
created: '2026-04-21'
id: ADR-0002
observed_via: seed
scope:
  paths:
  - src/adr_agent/cli.py
  tags:
  - click
  - cli
status: accepted
title: Uses click
---

## Context

adr-agent exposes a multi-command CLI (`init`, `propose`, `promote`, `show`, `plan`, `history`, and several hook subcommands) with interactive prompts, confirmation dialogs, `Choice`-constrained inputs, and structured help text. The CLI is the primary interface for both end-users and the Claude Code hooks that run it non-interactively.

## Decision

Use Click (`>=8.0`) as the CLI framework. All commands are declared with `@click.command()` decorators, interactive collection uses `click.prompt()` and `click.confirm()`, and output uses `click.echo()` for consistent stream handling.

## Consequences

Click's built-in prompt and confirm primitives directly implement the interactive ADR collection flow with minimal code. The `@click.group()` / `@click.command()` structure makes the multi-command surface readable and self-documenting with auto-generated `--help`. Click has strong cross-platform support including Windows, which matters because hooks run inside Claude Code on Windows, macOS, and Linux. The library is heavier than argparse for simple cases, but the interactive-prompt use case justifies it.
