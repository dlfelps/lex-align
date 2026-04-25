"""Helpers for parsing and diffing pyproject.toml dependencies.

These are the only pieces of the v1.x reconciler that survive the rewrite —
the client uses them to drive the pre-commit hook and the Claude Code edit
hook.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Optional


def normalize_name(dep_spec: str) -> str:
    for sep in (">=", "<=", "!=", "~=", "==", ">", "<", "[", ";", " "):
        dep_spec = dep_spec.split(sep)[0]
    return dep_spec.strip().lower().replace("-", "_").replace(".", "_")


def get_runtime_deps(pyproject_path: Path) -> dict[str, str]:
    """Return {normalized_name: raw_spec} from [project].dependencies."""
    if not pyproject_path.exists():
        return {}
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    deps = data.get("project", {}).get("dependencies", []) or []
    return {normalize_name(d): d.strip() for d in deps}


def parse_deps_from_content(content: str) -> dict[str, str]:
    try:
        data = tomllib.loads(content)
    except Exception:
        return {}
    deps = data.get("project", {}).get("dependencies", []) or []
    return {normalize_name(d): d.strip() for d in deps}


def diff_deps(old_content: str, new_content: str) -> tuple[dict[str, str], set[str]]:
    """Return ({added_name: raw_spec}, {removed_names})."""
    old = parse_deps_from_content(old_content)
    new = parse_deps_from_content(new_content)
    added = {name: spec for name, spec in new.items() if name not in old}
    removed = set(old) - set(new)
    return added, removed


def extract_pinned_version(spec: str) -> Optional[str]:
    """Pull a concrete version out of a spec like 'redis>=5.0' or 'httpx==0.28.1'."""
    m = re.search(r"(?:>=|<=|!=|~=|==|>|<)\s*([0-9][0-9a-zA-Z\.\-\+\_]*)", spec)
    return m.group(1) if m else None


def apply_edit(current_content: str, tool_name: str, tool_input: dict) -> str:
    """Simulate how a Claude Code Edit/Write/MultiEdit affects file content."""
    if tool_name == "Write":
        return tool_input.get("content", "")
    if tool_name == "Edit":
        old_str = tool_input.get("old_string", "")
        new_str = tool_input.get("new_string", "")
        return current_content.replace(old_str, new_str, 1)
    if tool_name == "MultiEdit":
        content = current_content
        for edit in tool_input.get("edits", []):
            content = content.replace(edit.get("old_string", ""), edit.get("new_string", ""), 1)
        return content
    return current_content


def detect_project_name(pyproject_path: Path, fallback: str) -> str:
    """Best-effort autodetect for `lex-align-client init`.

    Prefer [project].name from pyproject.toml; fall back to the directory name.
    """
    if pyproject_path.exists():
        try:
            with open(pyproject_path, "rb") as f:
                data = tomllib.load(f)
            name = data.get("project", {}).get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        except Exception:
            pass
    return fallback
