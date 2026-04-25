"""Idempotent installers for the Claude Code hooks and the git pre-commit hook."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path


_LEX_MARKER = "lex-align"
_WRAPPER_SCRIPT_NAME = "lex-align-hook.py"

_WRAPPER_SCRIPT_CONTENT = '''\
#!/usr/bin/env python3
"""Cross-platform shim for the lex-align Claude Code hooks."""
import shutil
import subprocess
import sys


def main() -> None:
    args = sys.argv[1:]
    stdin_data = sys.stdin.buffer.read()

    target = shutil.which("lex-align-client")
    if target:
        sys.exit(subprocess.run([target, "hook"] + args, input=stdin_data).returncode)

    uv = shutil.which("uv")
    if uv:
        r = subprocess.run(["uv", "run", "lex-align-client", "hook"] + args,
                           input=stdin_data, stderr=subprocess.PIPE)
        if r.returncode == 0:
            sys.exit(0)

    print("[lex-align] lex-align-client not installed; skipping hook.", file=sys.stderr)
    sys.exit(0)


main()
'''


_HOOK_EVENTS = {
    "SessionStart": {"subcommand": "session-start"},
    "PreToolUse":   {"subcommand": "pre-tool-use", "matcher": "Edit|Write|MultiEdit"},
    "SessionEnd":   {"subcommand": "session-end"},
}


def _settings_path(project_root: Path) -> Path:
    return project_root / ".claude" / "settings.json"


def _load_settings(project_root: Path) -> dict:
    path = _settings_path(project_root)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _save_settings(project_root: Path, settings: dict) -> None:
    path = _settings_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")


def _write_wrapper_script(project_root: Path) -> Path:
    script_path = project_root / ".claude" / _WRAPPER_SCRIPT_NAME
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(_WRAPPER_SCRIPT_CONTENT)
    try:
        script_path.chmod(script_path.stat().st_mode | 0o111)
    except OSError:
        pass
    return script_path


def install_claude_hooks(project_root: Path) -> None:
    _write_wrapper_script(project_root)
    command = f"python .claude/{_WRAPPER_SCRIPT_NAME}"
    settings = _load_settings(project_root)
    hooks = settings.setdefault("hooks", {})
    for event, meta in _HOOK_EVENTS.items():
        existing = hooks.setdefault(event, [])
        if any(
            any(_LEX_MARKER in h.get("command", "") for h in e.get("hooks", []))
            for e in existing
        ):
            continue
        entry: dict = {
            "hooks": [{"type": "command", "command": f"{command} {meta['subcommand']}"}]
        }
        if "matcher" in meta:
            entry["matcher"] = meta["matcher"]
        existing.append(entry)
    _save_settings(project_root, settings)


def remove_claude_hooks(project_root: Path) -> None:
    settings = _load_settings(project_root)
    hooks = settings.get("hooks", {})
    for event in list(hooks.keys()):
        hooks[event] = [
            entry for entry in hooks[event]
            if not any(_LEX_MARKER in h.get("command", "") for h in entry.get("hooks", []))
        ]
        if not hooks[event]:
            del hooks[event]
    _save_settings(project_root, settings)
    script_path = project_root / ".claude" / _WRAPPER_SCRIPT_NAME
    if script_path.exists():
        script_path.unlink()


def claude_hooks_status(project_root: Path) -> dict[str, bool]:
    settings = _load_settings(project_root)
    hooks = settings.get("hooks", {})
    return {
        event: any(
            any(_LEX_MARKER in h.get("command", "") for h in e.get("hooks", []))
            for e in hooks.get(event, [])
        )
        for event in _HOOK_EVENTS
    }


# ── git pre-commit ────────────────────────────────────────────────────────


_PRECOMMIT_MARKER = "# lex-align pre-commit"


def _precommit_path(project_root: Path) -> Path:
    return project_root / ".git" / "hooks" / "pre-commit"


def install_precommit(project_root: Path) -> Path | None:
    """Install or augment `.git/hooks/pre-commit`. Returns the path if written."""
    git_dir = project_root / ".git"
    if not git_dir.exists():
        return None
    hook_path = _precommit_path(project_root)
    hook_path.parent.mkdir(parents=True, exist_ok=True)

    snippet = (
        f"\n{_PRECOMMIT_MARKER}\n"
        "if command -v lex-align-client >/dev/null 2>&1; then\n"
        "  lex-align-client precommit || exit $?\n"
        "elif command -v uv >/dev/null 2>&1; then\n"
        "  uv run lex-align-client precommit || exit $?\n"
        "fi\n"
    )

    if hook_path.exists():
        existing = hook_path.read_text()
        if _PRECOMMIT_MARKER in existing:
            return hook_path
        new = existing.rstrip() + "\n" + snippet
        hook_path.write_text(new)
    else:
        hook_path.write_text("#!/usr/bin/env bash\nset -e\n" + snippet)
    try:
        hook_path.chmod(hook_path.stat().st_mode | 0o111)
    except OSError:
        pass
    return hook_path


def remove_precommit(project_root: Path) -> None:
    hook_path = _precommit_path(project_root)
    if not hook_path.exists():
        return
    text = hook_path.read_text()
    if _PRECOMMIT_MARKER not in text:
        return
    lines = text.splitlines()
    out: list[str] = []
    skip = False
    for line in lines:
        if _PRECOMMIT_MARKER in line:
            skip = True
            continue
        if skip:
            # Skip the lex-align block — it's bounded by the next blank line
            # or end of file.
            if line.strip() == "":
                skip = False
            continue
        out.append(line)
    hook_path.write_text("\n".join(out).rstrip() + "\n")
