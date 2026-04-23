from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Optional

from .models import Status
from .reconciler import apply_edit, diff_deps, reconcile
from .session import EventLogger, SessionState, clear_current_session, set_current_session_id
from .store import DecisionStore


def _find_project_root() -> Path:
    """Walk up from cwd looking for .lex-align directory."""
    path = Path.cwd()
    for parent in [path] + list(path.parents):
        if (parent / ".lex-align").exists():
            return parent
    return path


def _make_store(project_root: Path) -> DecisionStore:
    return DecisionStore(project_root / ".lex-align" / "decisions")


def _sessions_dir(project_root: Path) -> Path:
    return project_root / ".lex-align" / "sessions"


def handle_session_start(event: dict, project_root: Path) -> str:
    session_id = event.get("session_id", "unknown")
    sessions_dir = _sessions_dir(project_root)
    set_current_session_id(sessions_dir, session_id)

    logger = EventLogger(sessions_dir, session_id)
    store = _make_store(project_root)
    pyproject = project_root / "pyproject.toml"

    created = reconcile(pyproject, store)
    if created:
        logger.log_automated("reconciliation", created)

    return _build_brief(store, created)


def _build_brief(store: DecisionStore, new_observed: list[str]) -> str:
    decisions = store.load_all()
    accepted = [d for d in decisions if d.status == Status.ACCEPTED]
    observed = [d for d in decisions if d.status == Status.OBSERVED]

    lines = [f"# Architecture decisions ({len(accepted)} accepted, {len(observed)} observed)"]

    if accepted:
        lines.append("\nACCEPTED")
        for d in accepted:
            lines.append(f"  {d.id}  {d.title}")

    if observed:
        lines.append("\nOBSERVED (no rationale captured)")
        for d in observed:
            tag = "  [new: added by reconciliation]" if d.scope.tags and d.scope.tags[0] in new_observed else ""
            lines.append(f"  {d.id}  {d.title}{tag}")

    lines.append("\nRun `lex-align show <id>` for full rationale and alternatives.")
    lines.append('Run `lex-align plan "<prompt>"` to get relevant context before starting a task.')
    lines.append("Run `lex-align propose` to record a new decision or supersession.")
    lines.append("Run `lex-align promote <id>` to capture rationale for an observed entry.")
    return "\n".join(lines)


def handle_pre_tool_use(event: dict, project_root: Path) -> Optional[str]:
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})
    session_id = event.get("session_id", "unknown")
    sessions_dir = _sessions_dir(project_root)
    store = _make_store(project_root)
    state = SessionState(sessions_dir, session_id)

    path_str = tool_input.get("path", "")

    if "pyproject.toml" in path_str:
        return _handle_dep_edit_pre(tool_name, tool_input, path_str, store, state, project_root)

    if path_str.endswith(".py") and tool_name in ("Edit", "Write", "MultiEdit"):
        return _handle_code_edit_pre(tool_input, tool_name, path_str, store, state)

    return None


def _handle_dep_edit_pre(
    tool_name: str, tool_input: dict, path_str: str, store: DecisionStore, state: SessionState, project_root: Path
) -> Optional[str]:
    pyproject_path = project_root / path_str if not Path(path_str).is_absolute() else Path(path_str)
    if not pyproject_path.exists():
        return None

    current_content = pyproject_path.read_text()
    new_content = apply_edit(current_content, tool_name, tool_input)
    added, removed = diff_deps(current_content, new_content)
    if not added and not removed:
        return None

    state.record_dep_change(list(added | removed))

    lines = ["You are modifying runtime dependencies:"]
    for pkg in sorted(added):
        lines.append(f"  + {pkg}")
    for pkg in sorted(removed):
        lines.append(f"  - {pkg}")

    relevant = []
    for pkg in added | removed:
        relevant.extend(store.find_covering(pkg))
    # Also find all accepted decisions as context
    all_accepted = [d for d in store.load_all() if d.status == Status.ACCEPTED]
    seen_ids = {d.id for d in relevant}
    # include decisions whose scope tags overlap
    for pkg in added | removed:
        for d in all_accepted:
            if d.id not in seen_ids and pkg in [t.lower() for t in d.scope.tags]:
                relevant.append(d)
                seen_ids.add(d.id)

    if relevant:
        lines.append("\nRelevant existing decisions:")
        for d in relevant:
            lines.append(f"  {d.id}: {d.title} ({d.status.value})")

    lines.append("\nIf this change is covered by an existing decision, proceed.")
    lines.append("If it represents a new or superseding decision, run `lex-align propose` first.")
    lines.append("The propose flow will pre-fill the dependency name and relevant decisions; you provide the rationale.")
    return "\n".join(lines)


def _handle_code_edit_pre(
    tool_input: dict, tool_name: str, path_str: str, store: DecisionStore, state: SessionState
) -> Optional[str]:
    if tool_name == "Write":
        content = tool_input.get("content", "")
    elif tool_name == "Edit":
        content = tool_input.get("new_string", "")
    else:
        content = tool_input.get("content", "")

    imports = _extract_imports(content)
    observed = [d for d in store.load_all() if d.status == Status.OBSERVED]
    observed_tags = {tag.lower(): d for d in observed for tag in d.scope.tags}

    prompts = []
    for imp in imports:
        imp_lower = imp.lower()
        if imp_lower in observed_tags:
            d = observed_tags[imp_lower]
            if state.has_observed_prompt_fired(d.id):
                continue
            state.record_observed_prompt(d.id)
            via = f"during {d.provenance.value}" if d.provenance else "observed"
            prompts.append(
                f"You are editing code that imports `{imp}`, an observed dependency.\n"
                f"{d.id}: {d.title} (observed; no rationale captured)\n"
                f"  Added: {d.created} ({via})\n"
                f"If you have context for why `{imp}` was originally adopted, consider running "
                f"`lex-align promote {d.id}` to capture it.\n"
                f"If you don't have context, no action is needed."
            )

    return "\n\n".join(prompts) if prompts else None


def _extract_imports(content: str) -> set[str]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0])
    return imports


def handle_post_tool_use(event: dict, project_root: Path) -> Optional[str]:
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})
    session_id = event.get("session_id", "unknown")
    sessions_dir = _sessions_dir(project_root)
    path_str = tool_input.get("path", "")

    if "pyproject.toml" not in path_str:
        return None

    store = _make_store(project_root)
    pyproject_path = project_root / "pyproject.toml"
    created = reconcile(pyproject_path, store)

    logger = EventLogger(sessions_dir, session_id)
    if created:
        logger.log_automated("reconciliation", created)

    state = SessionState(sessions_dir, session_id)
    unresolved = state.unresolved_dep_changes()

    lines = []
    if created:
        lines.append(f"Reconciliation: {len(created)} new observed entr{'y' if len(created)==1 else 'ies'} created:")
        for pkg in created:
            covering = store.find_covering(pkg)
            if covering:
                lines.append(f"  {covering[-1].id}: {covering[-1].title}")

    if unresolved:
        lines.append(
            "\nYou modified dependencies without calling `lex-align propose` first. "
            "The affected packages are now observed entries. "
            "Run `lex-align promote <id>` if you can capture rationale now."
        )

    return "\n".join(lines) if lines else None


def handle_session_end(event: dict, project_root: Path) -> Optional[str]:
    session_id = event.get("session_id", "unknown")
    sessions_dir = _sessions_dir(project_root)
    state = SessionState(sessions_dir, session_id)
    unresolved = state.unresolved_dep_changes()

    clear_current_session(sessions_dir)
    state.cleanup()

    if not unresolved:
        return None

    lines = ["This session had unresolved dependency changes:"]
    for pkg in unresolved:
        lines.append(f"  {pkg} (added/removed; no rationale recorded)")
    lines.append("\nIf you have context for these changes, run `lex-align promote <id>` to capture rationale.")
    return "\n".join(lines)


def run_hook(hook_name: str) -> None:
    """Entry point for hook subcommands — reads JSON from stdin, writes output to stdout."""
    try:
        event = json.loads(sys.stdin.read())
    except Exception:
        event = {}

    project_root = _find_project_root()

    handlers = {
        "session-start": handle_session_start,
        "pre-tool-use": handle_pre_tool_use,
        "post-tool-use": handle_post_tool_use,
        "session-end": handle_session_end,
    }

    handler = handlers.get(hook_name)
    if handler is None:
        sys.exit(0)

    result = handler(event, project_root)

    if hook_name in ("pre-tool-use", "post-tool-use"):
        output: dict = {"decision": "allow"}
        if result:
            output["reason"] = result
        print(json.dumps(output))
    else:
        if result:
            print(result)
