"""`lex-align-server` CLI.

Exposes:
  * `serve`              — run uvicorn against the FastAPI app.
  * `init`               — materialize the operator bundle (compose stack,
                            Dockerfile, registry, .env) into a target dir.
  * `registry compile`   — compile a YAML registry to the JSON form the
                            server consumes.
  * `selftest`           — hit `/api/v1/health` to confirm a stack is up.
  * `admin keys ...`     — placeholders for the org-mode admin tooling.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from .init import MARKER_FILENAME, init_target
from .registry_schema import ValidationError, validate_registry


@click.group()
def main() -> None:
    """lex-align server CLI."""


@main.command()
@click.option("--host", default=None, help="Override BIND_HOST.")
@click.option("--port", default=None, type=int, help="Override BIND_PORT.")
@click.option("--reload", is_flag=True, help="Enable hot reload (development only).")
def serve(host: str | None, port: int | None, reload: bool) -> None:
    """Start the lex-align FastAPI server via uvicorn."""
    import uvicorn
    from .config import get_settings
    settings = get_settings()
    uvicorn.run(
        "lex_align_server.main:app",
        host=host or settings.bind_host,
        port=port or settings.bind_port,
        reload=reload,
    )


# ── init ──────────────────────────────────────────────────────────────────


@main.command()
@click.option(
    "--target",
    default="./lexalign",
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory to write the operator bundle into. Created if missing.",
)
@click.option(
    "--force",
    is_flag=True,
    help=f"Overwrite existing files (and the {MARKER_FILENAME} marker).",
)
def init(target: Path, force: bool) -> None:
    """Materialize the docker-compose stack, Dockerfile, registry, and .env
    template into TARGET so the server can be built and run with a single
    `docker compose up -d`.
    """
    try:
        result = init_target(target, force=force)
    except FileExistsError as exc:
        raise click.ClickException(str(exc))
    except ImportError as exc:
        raise click.ClickException(str(exc))
    except ValidationError as exc:
        raise click.ClickException(f"Registry validation failed: {exc}")

    for path in result.written:
        click.echo(f"  + {path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path}")
    for path in result.skipped:
        click.echo(f"  · skipped (exists) {path}")

    click.echo("")
    click.echo(f"Operator bundle written to {result.target}.")
    click.echo("")
    click.echo("Next steps:")
    click.echo(f"  cd {result.target.relative_to(Path.cwd()) if result.target.is_relative_to(Path.cwd()) else result.target}")
    click.echo("  cp .env.example .env       # edit if you need to change defaults")
    click.echo("  docker compose up -d")
    click.echo("  lex-align-server selftest  # confirm the stack is alive")


# ── registry compile ─────────────────────────────────────────────────────


@main.group()
def registry() -> None:
    """Manage the enterprise package registry."""


@registry.command("compile")
@click.argument("source", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("destination", type=click.Path(dir_okay=False, path_type=Path))
def registry_compile(source: Path, destination: Path) -> None:
    """Compile a YAML registry SOURCE to the JSON form at DESTINATION."""
    import yaml

    try:
        doc = yaml.safe_load(source.read_text())
    except yaml.YAMLError as exc:
        raise click.ClickException(f"YAML parse error: {exc}")
    try:
        compiled = validate_registry(doc)
    except ValidationError as exc:
        raise click.ClickException(f"Registry validation failed: {exc}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(compiled, indent=2, sort_keys=True) + "\n")
    click.echo(
        f"Compiled {len(compiled['packages'])} package rules from {source} → {destination}"
    )


# ── selftest ──────────────────────────────────────────────────────────────


@main.command()
@click.option(
    "--url",
    default="http://127.0.0.1:8765",
    help="Base URL of the server to probe.",
)
@click.option("--timeout", default=5.0, type=float, help="Per-request timeout (seconds).")
def selftest(url: str, timeout: float) -> None:
    """Probe `/api/v1/health` and report whether the stack is alive."""
    import httpx

    health = url.rstrip("/") + "/api/v1/health"
    try:
        response = httpx.get(health, timeout=timeout)
    except httpx.HTTPError as exc:
        raise click.ClickException(f"Server unreachable at {health}: {exc}")

    if response.status_code != 200:
        raise click.ClickException(
            f"Health check failed: HTTP {response.status_code} from {health}"
        )

    click.echo(f"OK  {health}")
    try:
        click.echo(json.dumps(response.json(), indent=2))
    except ValueError:
        click.echo(response.text)


# ── admin (deferred) ─────────────────────────────────────────────────────


@main.group()
def admin() -> None:
    """Administrative commands (organization mode)."""


@admin.group()
def keys() -> None:
    """Manage API keys (deferred — Phase 3+)."""


@keys.command("generate")
@click.option("--project", required=True, help="Project name to bind the key to.")
def keys_generate(project: str) -> None:  # pragma: no cover - stub
    raise click.ClickException(
        "API key management is not yet implemented; AUTH_ENABLED=true is a Phase-3 deliverable."
    )


@keys.command("list")
def keys_list() -> None:  # pragma: no cover - stub
    raise click.ClickException(
        "API key management is not yet implemented; AUTH_ENABLED=true is a Phase-3 deliverable."
    )
