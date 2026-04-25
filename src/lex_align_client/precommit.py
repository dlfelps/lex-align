"""Git pre-commit hook entry point.

Behavior:
  1. Read the staged contents of `pyproject.toml` (if it's in the index).
  2. Re-evaluate **every** runtime dependency against the server. Reading
     existing deps catches new CVEs published since the last commit.
  3. If any verdict is DENIED, exit non-zero with a structured stderr
     message that an AI agent can parse to self-correct.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Iterable

from .api import LexAlignClient, ServerError, ServerUnreachable, Verdict
from .config import ClientConfig, find_project_root, load_config
from .pyproject_utils import (
    extract_pinned_version,
    get_runtime_deps,
    parse_deps_from_content,
)


def _staged_pyproject(project_root: Path) -> str | None:
    """Return the staged contents of pyproject.toml, or None if not staged."""
    try:
        result = subprocess.run(
            ["git", "show", ":pyproject.toml"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _format_verdict_lines(verdicts: Iterable[Verdict]) -> list[str]:
    lines: list[str] = []
    for v in verdicts:
        if not v.denied:
            continue
        spec = v.package + (f" {v.version}" if v.version else "")
        lines.append(f"  ✗ {spec} — {v.reason}")
        if v.replacement:
            lines.append(f"      use instead: {v.replacement}")
        if v.cve_ids:
            lines.append(f"      CVEs: {', '.join(v.cve_ids[:5])}")
        if v.license:
            lines.append(f"      license: {v.license}")
    return lines


def run(argv: list[str] | None = None) -> int:
    project_root = find_project_root()
    config = load_config(project_root)
    if config is None:
        # No .lexalign.toml — likely a fresh checkout. Don't block; print a
        # clear hint so the developer can finish setup.
        print(
            "[lex-align] no .lexalign.toml found. Run `lex-align-client init` "
            "to enable enforcement.",
            file=sys.stderr,
        )
        return 0

    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        return 0

    staged = _staged_pyproject(project_root)
    if staged is not None:
        deps = parse_deps_from_content(staged)
    else:
        deps = get_runtime_deps(pyproject)

    if not deps:
        return 0

    verdicts: list[Verdict] = []
    try:
        with LexAlignClient(config) as client:
            for name, spec in sorted(deps.items()):
                version = extract_pinned_version(spec)
                v = client.check(name, version)
                verdicts.append(v)
    except ServerUnreachable as exc:
        print(
            f"[lex-align] pre-commit blocked: server unreachable at "
            f"{config.server_url} ({exc}).",
            file=sys.stderr,
        )
        return 1
    except ServerError as exc:
        print(f"[lex-align] pre-commit blocked: server error: {exc}", file=sys.stderr)
        return 1

    denied = [v for v in verdicts if v.denied]
    transport_errors = [v for v in verdicts if v.transport_error]

    if not denied:
        if transport_errors:
            print(
                f"[lex-align] WARNING: {len(transport_errors)} dependenc"
                f"{'y' if len(transport_errors) == 1 else 'ies'} could not "
                "be checked (server unreachable, fail_open=true).",
                file=sys.stderr,
            )
        return 0

    print("[lex-align] commit blocked — non-compliant dependencies:", file=sys.stderr)
    for line in _format_verdict_lines(denied):
        print(line, file=sys.stderr)
    print(
        "\nFix the listed packages (replace, pin a different version, or remove) "
        "and try again.",
        file=sys.stderr,
    )
    return 1


def main() -> None:
    sys.exit(run(sys.argv[1:]))
