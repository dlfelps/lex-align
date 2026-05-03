"""`lex-align-server` CLI.

Exposes:
  * `serve`              — run uvicorn against the FastAPI app.
  * `quickstart`         — Docker-free single-user bring-up: materialize a
                            local registry + sqlite, then run the server
                            in-process on 127.0.0.1:8765.
  * `init`               — materialize the operator bundle (compose stack,
                            Dockerfile, registry, .env) into a target dir.
  * `check-config`       — pre-flight checks for a single-team install:
                            REGISTRY_PATH, audit DB, cache, proposer, auth.
  * `registry compile`   — compile a YAML registry to the JSON form the
                            server consumes.
  * `selftest`           — hit `/api/v1/health` to confirm a stack is up.
  * `admin keys ...`     — placeholders for the org-mode admin tooling.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from .check_config import FAIL, OK, WARN, run_checks_sync
from .init import MARKER_FILENAME, init_target
from .quickstart import apply_env as quickstart_apply_env
from .quickstart import default_target as quickstart_default_target
from .quickstart import materialize as quickstart_materialize
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


# ── quickstart ────────────────────────────────────────────────────────────


@main.command()
@click.option(
    "--target",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory for the registry + audit DB. Defaults to ~/.lexalign.",
)
@click.option("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
@click.option("--port", default=8765, type=int, help="Bind port (default 8765).")
@click.option(
    "--no-serve",
    is_flag=True,
    help="Materialize the directory and exit without starting uvicorn.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing registry.yml in the target directory.",
)
def quickstart(
    target: Path | None,
    host: str,
    port: int,
    no_serve: bool,
    force: bool,
) -> None:
    """Docker-free single-user bring-up.

    Lays down a local registry + sqlite under ``--target`` (default
    ``~/.lexalign``), then runs the server in-process on
    ``http://127.0.0.1:8765``. Redis is skipped (the cache layer
    silently degrades). Stop with Ctrl-C.

    For multi-user deployments use `lex-align-server init` + Docker
    Compose instead.
    """
    target = target or quickstart_default_target()
    try:
        result = quickstart_materialize(
            target, bind_host=host, bind_port=port, force=force
        )
    except ValidationError as exc:
        raise click.ClickException(f"Registry validation failed: {exc}")

    for p in result.written:
        click.echo(f"  + {p}")
    for p in result.skipped:
        click.echo(f"  · skipped (exists) {p}")

    click.echo("")
    click.echo(f"Quickstart bundle ready under {result.target}.")
    click.echo(f"  registry: {result.registry_yml}")
    click.echo(f"  audit DB: {result.database_path}")
    click.echo("")

    if no_serve:
        click.echo("Skipping `serve` (--no-serve). Start later with:")
        click.echo(
            "  REGISTRY_PATH={reg} DATABASE_PATH={db} BIND_HOST={h} BIND_PORT={p} "
            "lex-align-server serve".format(
                reg=result.registry_yml, db=result.database_path,
                h=result.bind_host, p=result.bind_port,
            )
        )
        return

    os.environ.update(quickstart_apply_env(result))

    click.echo(f"Starting server on http://{result.bind_host}:{result.bind_port} (Ctrl-C to stop)")
    click.echo("Run `lex-align-client init` in another terminal to point a project at it.")
    click.echo("")
    import uvicorn
    uvicorn.run(
        "lex_align_server.main:app",
        host=result.bind_host,
        port=result.bind_port,
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


# ── check-config ─────────────────────────────────────────────────────────


_STATUS_GLYPH = {OK: "OK ", WARN: "WRN", FAIL: "FAIL"}


@main.command("check-config")
def check_config() -> None:
    """Validate the server's configuration for a single-team install.

    Verifies REGISTRY_PATH, audit DB writability, cache reachability, the
    auto-detected proposer, and auth posture. Exits non-zero if any
    check fails. Warnings (e.g. anonymous auth on a non-loopback bind,
    Redis unreachable) do not fail the run.
    """
    from .config import get_settings
    settings = get_settings()
    results = run_checks_sync(settings)

    label_width = max((len(r.label) for r in results), default=10)
    for r in results:
        click.echo(f"{_STATUS_GLYPH[r.status]}  {r.label.ljust(label_width)}  {r.detail}")

    fails = sum(1 for r in results if r.status == FAIL)
    warns = sum(1 for r in results if r.status == WARN)
    click.echo("")
    if fails:
        click.echo(f"{fails} failure(s), {warns} warning(s).", err=True)
        sys.exit(1)
    if warns:
        click.echo(f"All checks passed with {warns} warning(s).")
    else:
        click.echo("All checks passed.")


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
