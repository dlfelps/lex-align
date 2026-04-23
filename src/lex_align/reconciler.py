from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Optional

from .models import ObservedVia, Status
from .store import DecisionStore, create_observed


def get_runtime_deps(pyproject_path: Path) -> set[str]:
    """Return normalized package names from [project].dependencies in pyproject.toml."""
    if not pyproject_path.exists():
        return set()
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    deps = data.get("project", {}).get("dependencies", [])
    return {_normalize_name(dep) for dep in deps}


def _normalize_name(dep_spec: str) -> str:
    """Extract bare package name from a dependency specifier like 'redis>=5.0'."""
    for sep in (">=", "<=", "!=", "~=", "==", ">", "<", "[", ";", " "):
        dep_spec = dep_spec.split(sep)[0]
    return dep_spec.strip().lower().replace("-", "_").replace(".", "_")


def find_uncovered(packages: set[str], store: DecisionStore) -> set[str]:
    """Return packages not covered by any active decision."""
    uncovered = set()
    for pkg in packages:
        covering = store.find_covering(pkg)
        if not covering:
            uncovered.add(pkg)
    return uncovered


def reconcile(
    pyproject_path: Path,
    store: DecisionStore,
    observed_via: ObservedVia = ObservedVia.RECONCILIATION,
) -> list[str]:
    """Create observed entries for uncovered runtime deps. Returns new package names."""
    packages = get_runtime_deps(pyproject_path)
    uncovered = find_uncovered(packages, store)
    created = []
    for pkg in sorted(uncovered):
        create_observed(pkg, store, observed_via)
        created.append(pkg)
    return created


def diff_deps(old_content: str, new_content: str) -> tuple[set[str], set[str]]:
    """Return (added, removed) package name sets between two pyproject.toml contents."""
    old = _parse_deps_from_content(old_content)
    new = _parse_deps_from_content(new_content)
    return new - old, old - new


def _parse_deps_from_content(content: str) -> set[str]:
    try:
        data = tomllib.loads(content)
        deps = data.get("project", {}).get("dependencies", [])
        return {_normalize_name(d) for d in deps}
    except Exception:
        return set()


def apply_edit(current_content: str, tool_name: str, tool_input: dict) -> str:
    """Simulate how an Edit/Write/MultiEdit changes file content."""
    if tool_name == "Write":
        return tool_input.get("content", "")
    elif tool_name == "Edit":
        old_str = tool_input.get("old_string", "")
        new_str = tool_input.get("new_string", "")
        return current_content.replace(old_str, new_str, 1)
    elif tool_name == "MultiEdit":
        content = current_content
        for edit in tool_input.get("edits", []):
            content = content.replace(edit.get("old_string", ""), edit.get("new_string", ""), 1)
        return content
    return current_content
