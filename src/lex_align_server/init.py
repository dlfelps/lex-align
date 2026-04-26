"""`lex-align-server init` — materialize the operator bundle into a directory.

Mirrors the design of `lex-align-client init`: copies the docker-compose
stack, Dockerfile, example registry, and `.env.example` from the wheel's
bundled assets into a target directory, then compiles the registry to
JSON so `docker compose up` works on the first try.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Iterable

from .registry_schema import validate_registry

MARKER_FILENAME = ".lexalign-server.toml"
_ASSETS_PACKAGE = "lex_align_server._assets"

# Files copied verbatim into the target directory.
_VERBATIM_ASSETS: tuple[str, ...] = (
    "README.md",
)
# Files where `{LEX_ALIGN_VERSION}` is substituted with the installed version.
_TEMPLATED_ASSETS: tuple[str, ...] = (
    "Dockerfile",
    "docker-compose.yml",
)
# Source name in the wheel → destination name in the target directory.
# (Bundled names avoid leading dots so the wheel build does not have to
# special-case dotfiles.)
_RENAMED_ASSETS: tuple[tuple[str, str], ...] = (
    ("registry.example.yml", "registry.yml"),
    ("env.example", ".env.example"),
)


@dataclass
class InitResult:
    target: Path
    written: list[Path]
    skipped: list[Path]
    compiled_registry: Path | None


def _installed_version() -> str:
    try:
        return _pkg_version("lex-align")
    except PackageNotFoundError:
        # Editable install / running from a source checkout without a wheel.
        from . import __version__ as fallback
        return fallback


def _read_asset(name: str) -> bytes:
    return resources.files(_ASSETS_PACKAGE).joinpath(name).read_bytes()


def _write(
    dest: Path,
    payload: bytes,
    *,
    force: bool,
    written: list[Path],
    skipped: list[Path],
) -> None:
    if dest.exists() and not force:
        skipped.append(dest)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)
    written.append(dest)


def _compile_registry(yaml_path: Path, json_path: Path) -> None:
    import json

    import yaml

    doc = yaml.safe_load(yaml_path.read_text())
    compiled = validate_registry(doc)
    json_path.write_text(json.dumps(compiled, indent=2, sort_keys=True) + "\n")


def asset_names() -> Iterable[str]:
    """All asset basenames as they appear in the wheel. Useful for tests."""
    return (
        *_VERBATIM_ASSETS,
        *_TEMPLATED_ASSETS,
        *(src for src, _ in _RENAMED_ASSETS),
    )


def init_target(target: Path, *, force: bool = False) -> InitResult:
    """Materialize the operator bundle into ``target``.

    Idempotent by default: if the marker file exists, raises FileExistsError
    unless ``force`` is set.
    """
    target = target.resolve()
    marker = target / MARKER_FILENAME
    if marker.exists() and not force:
        raise FileExistsError(
            f"{marker} exists; this directory was already initialized. "
            "Use --force to overwrite."
        )

    target.mkdir(parents=True, exist_ok=True)
    version = _installed_version()
    written: list[Path] = []
    skipped: list[Path] = []

    for name in _VERBATIM_ASSETS:
        _write(target / name, _read_asset(name), force=force, written=written, skipped=skipped)

    for name in _TEMPLATED_ASSETS:
        body = _read_asset(name).decode("utf-8").replace("{LEX_ALIGN_VERSION}", version)
        _write(target / name, body.encode("utf-8"), force=force, written=written, skipped=skipped)

    for src, dest in _RENAMED_ASSETS:
        _write(target / dest, _read_asset(src), force=force, written=written, skipped=skipped)

    yaml_path = target / "registry.yml"
    json_path = target / "registry.json"
    compiled: Path | None = None
    if yaml_path.exists() and (force or not json_path.exists()):
        _compile_registry(yaml_path, json_path)
        compiled = json_path
        if json_path not in written:
            written.append(json_path)

    marker.write_text(f'version = "{version}"\n')
    if marker not in written:
        written.append(marker)

    return InitResult(
        target=target,
        written=written,
        skipped=skipped,
        compiled_registry=compiled,
    )
