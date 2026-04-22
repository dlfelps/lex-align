#!/usr/bin/env python3
import shutil
import subprocess
import sys
from pathlib import Path


def _adr_in_pyproject() -> bool:
    """Return True if adr-agent appears in the project's pyproject.toml."""
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    try:
        return "adr-agent" in pyproject.read_text()
    except OSError:
        return False


def _install_hint(uv: bool) -> str:
    in_pyproject = _adr_in_pyproject()
    if in_pyproject and uv:
        return "adr-agent is listed in pyproject.toml but not installed. Run: uv sync"
    if in_pyproject:
        return (
            "adr-agent is listed in pyproject.toml but not installed. "
            "Activate your virtual environment or run: pip install -e ."
        )
    if uv:
        return "adr-agent not installed. Run: uv tool install adr-agent"
    return "adr-agent not installed. Run: pip install adr-agent"


def main():
    args = sys.argv[1:]
    stdin_data = sys.stdin.buffer.read()

    if shutil.which("adr-agent"):
        sys.exit(subprocess.run(["adr-agent"] + args, input=stdin_data).returncode)

    uv = shutil.which("uv")
    if uv:
        # Capture stderr so uv's own "not found" error doesn't mix with ours.
        r = subprocess.run(["uv", "run", "adr-agent"] + args, input=stdin_data,
                           stderr=subprocess.PIPE)
        if r.returncode == 0:
            sys.exit(0)

    print(f"[adr-agent] {_install_hint(bool(uv))}", file=sys.stderr)
    sys.exit(0)


main()
