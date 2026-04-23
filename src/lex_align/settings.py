from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path

_LEX_MARKER = "lex-align"
_WRAPPER_SCRIPT_NAME = "lex-align-hook.py"

# Cross-platform hook wrapper: tries direct PATH install first, then uv run,
# exits 0 gracefully if lex-align is not found so collaborators without the
# tool don't see hook failures.
_WRAPPER_SCRIPT_CONTENT = '''\
#!/usr/bin/env python3
import shutil
import subprocess
import sys
from pathlib import Path


def _lex_align_in_pyproject() -> bool:
    """Return True if lex-align appears in the project's pyproject.toml."""
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    try:
        return "lex-align" in pyproject.read_text()
    except OSError:
        return False


def _install_hint(uv: bool) -> str:
    in_pyproject = _lex_align_in_pyproject()
    if in_pyproject and uv:
        return "lex-align is listed in pyproject.toml but not installed. Run: uv sync"
    if in_pyproject:
        return (
            "lex-align is listed in pyproject.toml but not installed. "
            "Activate your virtual environment or run: pip install -e ."
        )
    if uv:
        return "lex-align not installed. Run: uv tool install lex-align"
    return "lex-align not installed. Run: pip install lex-align"


def main():
    args = sys.argv[1:]
    stdin_data = sys.stdin.buffer.read()

    if shutil.which("lex-align"):
        sys.exit(subprocess.run(["lex-align"] + args, input=stdin_data).returncode)

    uv = shutil.which("uv")
    if uv:
        # Capture stderr so uv\'s own "not found" error doesn\'t mix with ours.
        r = subprocess.run(["uv", "run", "lex-align"] + args, input=stdin_data,
                           stderr=subprocess.PIPE)
        if r.returncode == 0:
            sys.exit(0)

    print(f"[lex-align] {_install_hint(bool(uv))}", file=sys.stderr)
    sys.exit(0)


main()
'''

_HOOK_EVENTS = {
    "SessionStart": {
        "subcommand": "session-start",
    },
    "PreToolUse": {
        "subcommand": "pre-tool-use",
        "matcher": "Edit|Write|MultiEdit",
    },
    "PostToolUse": {
        "subcommand": "post-tool-use",
        "matcher": "Edit|Write|MultiEdit",
    },
    "SessionEnd": {
        "subcommand": "session-end",
    },
}


def detect_lex_command(project_root: Path) -> str:
    """Return the shell command that will reliably invoke lex-align from a hook."""
    is_windows = platform.system() == "Windows"
    scripts_dir = "Scripts" if is_windows else "bin"
    exe_suffix = ".exe" if is_windows else ""
    script_name = f"lex-align{exe_suffix}"

    # When running inside the project's own venv (e.g. self-development with uv),
    # prefer "uv run" so hooks don't need an absolute path into a venv.
    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env:
        venv_parent = Path(venv_env).resolve().parent
        if (
            venv_parent == project_root.resolve()
            and (project_root / "uv.lock").exists()
            and shutil.which("uv")
        ):
            return "uv run lex-align"

        # Otherwise pin to the absolute script path in the active venv so hooks
        # work even when the venv is not activated in Claude's shell.
        venv_script = Path(venv_env) / scripts_dir / script_name
        if venv_script.exists():
            return str(venv_script)

    # Installed next to the current interpreter (global pip, pipx, uv tool, etc.)
    py_script = Path(sys.executable).parent / script_name
    if py_script.exists():
        return str(py_script)

    # Assume it's on PATH (global install with PATH configured correctly).
    return "lex-align"


def _build_hooks_config(command: str) -> dict:
    config: dict = {}
    for event, meta in _HOOK_EVENTS.items():
        entry: dict = {
            "hooks": [{"type": "command", "command": f"{command} {meta['subcommand']}"}]
        }
        if "matcher" in meta:
            entry["matcher"] = meta["matcher"]
        config[event] = [entry]
    return config


def _settings_path(project_root: Path) -> Path:
    return project_root / ".claude" / "settings.json"


def load_settings(project_root: Path) -> dict:
    path = _settings_path(project_root)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def save_settings(settings: dict, project_root: Path) -> None:
    path = _settings_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")


def _write_wrapper_script(project_root: Path) -> None:
    script_path = project_root / ".claude" / _WRAPPER_SCRIPT_NAME
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(_WRAPPER_SCRIPT_CONTENT)
    try:
        script_path.chmod(script_path.stat().st_mode | 0o111)
    except OSError:
        pass  # Windows — chmod is a no-op anyway


def add_lex_hooks(project_root: Path) -> None:
    _write_wrapper_script(project_root)
    command = f"python .claude/{_WRAPPER_SCRIPT_NAME}"
    hooks_config = _build_hooks_config(command)

    settings = load_settings(project_root)
    hooks = settings.setdefault("hooks", {})

    for event, new_entries in hooks_config.items():
        existing: list = hooks.setdefault(event, [])
        # Idempotent: skip if any lex-align hook is already registered for this event.
        if any(
            any(_LEX_MARKER in h.get("command", "") for h in e.get("hooks", []))
            for e in existing
        ):
            continue
        for entry in new_entries:
            existing.append(entry)

    save_settings(settings, project_root)


def remove_lex_hooks(project_root: Path) -> None:
    settings = load_settings(project_root)
    hooks = settings.get("hooks", {})

    for event in list(hooks.keys()):
        hooks[event] = [
            entry for entry in hooks[event]
            if not any(_LEX_MARKER in h.get("command", "") for h in entry.get("hooks", []))
        ]
        if not hooks[event]:
            del hooks[event]

    save_settings(settings, project_root)

    script_path = project_root / ".claude" / _WRAPPER_SCRIPT_NAME
    if script_path.exists():
        script_path.unlink()


def check_hooks_present(project_root: Path) -> dict[str, bool]:
    settings = load_settings(project_root)
    hooks = settings.get("hooks", {})
    return {
        event: any(
            any(_LEX_MARKER in h.get("command", "") for h in e.get("hooks", []))
            for e in hooks.get(event, [])
        )
        for event in _HOOK_EVENTS
    }
