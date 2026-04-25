"""`lex-align-server` CLI.

Currently exposes:
  * `serve`           — run uvicorn against the FastAPI app.
  * `admin keys ...`  — placeholders for the org-mode admin tooling.
"""

from __future__ import annotations

import click

from .config import get_settings


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
    settings = get_settings()
    uvicorn.run(
        "lex_align_server.main:app",
        host=host or settings.bind_host,
        port=port or settings.bind_port,
        reload=reload,
    )


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
