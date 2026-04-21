from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path

_ADR_MARKER = "adr-agent"

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


def detect_adr_command(project_root: Path) -> str:
    """Return the shell command that will reliably invoke adr-agent from a hook."""
    is_windows = platform.system() == "Windows"
    scripts_dir = "Scripts" if is_windows else "bin"
    exe_suffix = ".exe" if is_windows else ""
    script_name = f"adr-agent{exe_suffix}"

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
            return "uv run adr-agent"

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
    return "adr-agent"


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


def add_adr_hooks(project_root: Path) -> None:
    command = detect_adr_command(project_root)
    hooks_config = _build_hooks_config(command)

    settings = load_settings(project_root)
    hooks = settings.setdefault("hooks", {})

    for event, new_entries in hooks_config.items():
        existing: list = hooks.setdefault(event, [])
        # Idempotent: skip if any adr-agent hook is already registered for this event.
        if any(
            any(_ADR_MARKER in h.get("command", "") for h in e.get("hooks", []))
            for e in existing
        ):
            continue
        for entry in new_entries:
            existing.append(entry)

    save_settings(settings, project_root)


def remove_adr_hooks(project_root: Path) -> None:
    settings = load_settings(project_root)
    hooks = settings.get("hooks", {})

    for event in list(hooks.keys()):
        hooks[event] = [
            entry for entry in hooks[event]
            if not any(_ADR_MARKER in h.get("command", "") for h in entry.get("hooks", []))
        ]
        if not hooks[event]:
            del hooks[event]

    save_settings(settings, project_root)


def check_hooks_present(project_root: Path) -> dict[str, bool]:
    settings = load_settings(project_root)
    hooks = settings.get("hooks", {})
    return {
        event: any(
            any(_ADR_MARKER in h.get("command", "") for h in e.get("hooks", []))
            for e in hooks.get(event, [])
        )
        for event in _HOOK_EVENTS
    }
