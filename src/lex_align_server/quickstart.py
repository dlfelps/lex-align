"""`lex-align-server quickstart` — Docker-free single-user bring-up.

The Docker compose path stays the recommended deployment for anything
multi-user, but a single developer evaluating the tool on a laptop
benefits from a one-command path that:

* materializes a registry into a writable directory next to a sqlite
  audit DB,
* runs the FastAPI app in-process under uvicorn on 127.0.0.1:8765,
* skips Redis (the cache layer already silently degrades when the
  configured URL can't be reached, so the server still serves — license
  and CVE lookups just hit upstream every time).

This is *not* a substitute for the docker stack in production; it is a
shortcut for "I want to try this on my laptop without setting up
infrastructure." The marker file (``.lexalign-quickstart.toml``) keeps
the directory layout idempotent and distinct from ``init``'s docker
bundle.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .registry_schema import validate_registry

QUICKSTART_MARKER = ".lexalign-quickstart.toml"
_DEFAULT_TARGET = Path.home() / ".lexalign"
_REGISTRY_FILE = "registry.yml"
_DB_FILE = "lexalign.sqlite"
_ASSETS_PACKAGE = "lex_align_server._assets"


@dataclass
class QuickstartResult:
    target: Path
    registry_yml: Path
    registry_json: Path
    database_path: Path
    bind_host: str
    bind_port: int
    written: list[Path]
    skipped: list[Path]


def default_target() -> Path:
    return _DEFAULT_TARGET


def materialize(
    target: Path | None = None,
    *,
    bind_host: str = "127.0.0.1",
    bind_port: int = 8765,
    force: bool = False,
) -> QuickstartResult:
    """Lay down ``target`` so ``serve`` can boot against it.

    Idempotent: existing files are preserved unless ``force`` is set.
    The registry YAML is always (re)compiled to JSON because the
    server reads the JSON form.
    """
    import json

    import yaml

    target = (target or default_target()).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    skipped: list[Path] = []

    yml_path = target / _REGISTRY_FILE
    if yml_path.exists() and not force:
        skipped.append(yml_path)
    else:
        yml_path.write_bytes(
            resources.files(_ASSETS_PACKAGE).joinpath("registry.example.yml").read_bytes()
        )
        written.append(yml_path)

    # Always compile JSON so the server has a fresh artifact even when
    # the YAML was preserved (the user may have edited it).
    json_path = target / "registry.json"
    doc = yaml.safe_load(yml_path.read_text(encoding="utf-8")) or {}
    compiled = validate_registry(doc)
    json_path.write_text(json.dumps(compiled, indent=2, sort_keys=True) + "\n")
    if json_path not in written:
        written.append(json_path)

    db_path = target / _DB_FILE
    marker = target / QUICKSTART_MARKER
    marker.write_text(
        f'target = "{target}"\n'
        f'registry_path = "{yml_path}"\n'
        f'database_path = "{db_path}"\n'
        f'bind_host = "{bind_host}"\n'
        f'bind_port = {bind_port}\n'
    )
    if marker not in written:
        written.append(marker)

    return QuickstartResult(
        target=target,
        registry_yml=yml_path,
        registry_json=json_path,
        database_path=db_path,
        bind_host=bind_host,
        bind_port=bind_port,
        written=written,
        skipped=skipped,
    )


def apply_env(result: QuickstartResult) -> dict[str, str]:
    """Compute the environment overrides the in-process uvicorn run needs.

    Returned as a dict so callers can choose to ``os.environ.update`` it
    (the CLI does) or inspect it (tests do). The ``REDIS_URL`` is
    intentionally left at its default-ish value pointing at a port the
    quickstart user is unlikely to be running Redis on; the cache layer
    silently degrades when it can't connect.
    """
    return {
        "REGISTRY_PATH": str(result.registry_yml),
        "DATABASE_PATH": str(result.database_path),
        "BIND_HOST": result.bind_host,
        "BIND_PORT": str(result.bind_port),
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        # No proposer override: with REGISTRY_PATH set on a writable
        # directory the auto-detector picks ``local_file`` (the
        # recommended single-team backend).
    }
