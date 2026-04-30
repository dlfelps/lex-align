"""`lex-align-client` CLI: init, check, request-approval, hook, precommit."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from .api import LexAlignClient, ServerError, ServerUnreachable
from .claude_hooks import run_hook
from .claudemd import install_claude_md
from .config import (
    CONFIG_FILENAME,
    ClientConfig,
    config_path,
    find_project_root,
    load_config,
    save_config,
)
from .precommit import run as run_precommit
from .pyproject_utils import detect_project_name
from .settings import (
    install_claude_hooks,
    install_precommit,
    remove_claude_hooks,
    remove_precommit,
)


def _require_config(project_root: Path) -> ClientConfig:
    config = load_config(project_root)
    if config is None:
        raise click.ClickException(
            f"No {CONFIG_FILENAME} found. Run `lex-align-client init` first."
        )
    return config


@click.group()
def main() -> None:
    """lex-align client — talks to the lex-align server."""


# ── init ──────────────────────────────────────────────────────────────────


@main.command()
@click.option("--yes", "-y", is_flag=True, help="Accept defaults without prompting.")
@click.option("--server-url", default=None, help="Override server URL.")
@click.option("--project", "project_name", default=None, help="Override project name.")
@click.option("--mode", type=click.Choice(["single-user", "org"]), default=None)
@click.option("--no-claude-hooks", is_flag=True, help="Skip Claude Code hook install.")
@click.option("--no-precommit", is_flag=True, help="Skip git pre-commit install.")
@click.option("--no-claude-md", is_flag=True, help="Skip CLAUDE.md creation/update.")
def init(
    yes: bool,
    server_url: str | None,
    project_name: str | None,
    mode: str | None,
    no_claude_hooks: bool,
    no_precommit: bool,
    no_claude_md: bool,
) -> None:
    """One-time setup: write .lexalign.toml and install hooks."""
    project_root = Path.cwd()

    if config_path(project_root).exists() and not yes:
        if not click.confirm(
            f"{CONFIG_FILENAME} already exists. Overwrite?", default=False
        ):
            raise click.Abort()

    autodetected = detect_project_name(project_root / "pyproject.toml", project_root.name)
    if project_name is None:
        project_name = autodetected if yes else click.prompt(
            "Project name", default=autodetected
        )
    if server_url is None:
        server_url = "http://127.0.0.1:8765" if yes else click.prompt(
            "Server URL", default="http://127.0.0.1:8765"
        )
    if mode is None:
        mode = "single-user" if yes else click.prompt(
            "Mode", type=click.Choice(["single-user", "org"]), default="single-user"
        )

    config = ClientConfig(
        project=project_name,
        server_url=server_url,
        mode=mode,
        fail_open=(mode == "single-user"),
    )
    path = save_config(project_root, config)
    click.echo(f"Wrote {path}")

    if not no_claude_hooks:
        install_claude_hooks(project_root)
        click.echo("Installed Claude Code hooks in .claude/settings.json.")

    if not no_claude_md:
        md_existed = (project_root / "CLAUDE.md").exists()
        md_path, changed = install_claude_md(project_root)
        if changed:
            action = "Updated" if md_existed else "Created"
            click.echo(f"{action} {md_path.name} with lex-align usage instructions.")
        else:
            click.echo(f"{md_path.name} already contains lex-align section; skipped.")

    if not no_precommit:
        hook_path = install_precommit(project_root)
        if hook_path:
            click.echo(f"Installed git pre-commit hook at {hook_path}.")
        else:
            click.echo(
                "Skipped pre-commit install: this directory is not a git repo "
                "(run `git init` then `lex-align-client init` again)."
            )

    if mode == "org":
        click.echo("")
        click.echo(
            f"Organization mode: export your API key as ${config.api_key_env_var} "
            "before running checks."
        )


# ── check ─────────────────────────────────────────────────────────────────


@main.command()
@click.option("--package", required=True)
@click.option("--version", default=None)
@click.option("--json", "as_json", is_flag=True, default=True, help="Emit JSON (default).")
@click.option(
    "--agent-model",
    default=None,
    help="Override agent model tag (default: $LEXALIGN_AGENT_MODEL).",
)
@click.option(
    "--agent-version",
    default=None,
    help="Override agent version tag (default: $LEXALIGN_AGENT_VERSION).",
)
def check(
    package: str,
    version: str | None,
    as_json: bool,
    agent_model: str | None,
    agent_version: str | None,
) -> None:
    """Check a package against the server policy."""
    project_root = find_project_root()
    config = _require_config(project_root)
    try:
        with LexAlignClient(
            config, agent_model=agent_model, agent_version=agent_version
        ) as client:
            verdict = client.check(package, version)
    except ServerUnreachable as exc:
        raise click.ClickException(f"Server unreachable: {exc}")
    except ServerError as exc:
        raise click.ClickException(f"Server error: {exc}")
    click.echo(json.dumps(verdict.to_dict(), indent=2))
    if verdict.denied:
        sys.exit(2)


# ── request-approval ──────────────────────────────────────────────────────


@main.command("request-approval")
@click.option("--package", required=True)
@click.option("--rationale", required=True)
@click.option(
    "--agent-model",
    default=None,
    help="Override agent model tag (default: $LEXALIGN_AGENT_MODEL).",
)
@click.option(
    "--agent-version",
    default=None,
    help="Override agent version tag (default: $LEXALIGN_AGENT_VERSION).",
)
def request_approval(
    package: str,
    rationale: str,
    agent_model: str | None,
    agent_version: str | None,
) -> None:
    """Submit a non-blocking request to add `package` to the registry."""
    project_root = find_project_root()
    config = _require_config(project_root)
    try:
        with LexAlignClient(
            config, agent_model=agent_model, agent_version=agent_version
        ) as client:
            response = client.request_approval(package, rationale)
    except ServerUnreachable as exc:
        raise click.ClickException(f"Server unreachable: {exc}")
    except ServerError as exc:
        raise click.ClickException(f"Server error: {exc}")
    click.echo(json.dumps(response, indent=2))


# ── precommit (entry point for the git hook) ──────────────────────────────


@main.command()
def precommit() -> None:
    """Pre-commit hook entry point. Exits non-zero on any DENIED dependency."""
    sys.exit(run_precommit())


# ── hook (entry point for the Claude Code wrapper) ────────────────────────


@main.command(hidden=True)
@click.argument("name")
def hook(name: str) -> None:
    sys.exit(run_hook(name))


# ── uninstall ─────────────────────────────────────────────────────────────


@main.command()
@click.option("--yes", "-y", is_flag=True)
def uninstall(yes: bool) -> None:
    """Remove .claude hooks and the git pre-commit shim. Leaves .lexalign.toml."""
    project_root = find_project_root()
    if not yes and not click.confirm(
        "Remove lex-align hooks (Claude + git pre-commit)?", default=False
    ):
        raise click.Abort()
    remove_claude_hooks(project_root)
    remove_precommit(project_root)
    click.echo("Removed Claude hooks and git pre-commit shim.")
    click.echo(f"{CONFIG_FILENAME} preserved.")
