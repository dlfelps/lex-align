"""Claude Code session hooks (Advisor surface).

`SessionStart` and `PreToolUse` proxy through to the server's `/evaluate` so
the agent sees blocked / provisionally-allowed verdicts during planning,
before the pre-commit gate fires.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from .api import LexAlignClient, ServerError, ServerUnreachable, Verdict
from .config import ClientConfig, find_project_root, load_config
from .pyproject_utils import (
    apply_edit,
    diff_deps,
    extract_pinned_version,
    get_runtime_deps,
)


def _read_event() -> dict:
    try:
        return json.loads(sys.stdin.read())
    except Exception:
        return {}


def _emit_pretool_decision(decision: str, message: str | None = None) -> None:
    payload: dict = {"decision": decision}
    if message:
        payload["reason"] = message
    print(json.dumps(payload))
    if decision == "block" and message:
        print(message, file=sys.stderr)


def handle_session_start(event: dict, project_root: Path, config: ClientConfig) -> str:
    deps = get_runtime_deps(project_root / "pyproject.toml")
    lines = [
        f"# lex-align session brief — project: {config.project}",
        f"Server: {config.server_url} (mode: {config.mode})",
        f"Tracked runtime dependencies: {len(deps)}",
    ]
    try:
        with LexAlignClient(config) as client:
            health = client.health()
        lines.append(
            f"Health: redis={health.get('redis')} db={health.get('db')} "
            f"registry={'loaded' if health.get('registry_loaded') else 'absent'}"
        )
    except Exception as exc:
        lines.append(f"Health: unreachable ({exc.__class__.__name__})")
    lines.append("Edits to pyproject.toml will be evaluated against the registry.")
    return "\n".join(lines)


def handle_pre_tool_use(
    event: dict, project_root: Path, config: ClientConfig
) -> Optional[tuple[str, str | None]]:
    """Return (decision, message) for pyproject.toml edits, else None."""
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}
    path_str = tool_input.get("path", "") or tool_input.get("file_path", "")
    if "pyproject.toml" not in path_str:
        return None
    if tool_name not in ("Edit", "Write", "MultiEdit"):
        return None

    pyproject_path = (
        project_root / path_str if not Path(path_str).is_absolute() else Path(path_str)
    )
    if not pyproject_path.exists():
        return None

    current = pyproject_path.read_text()
    proposed = apply_edit(current, tool_name, tool_input)
    added, removed = diff_deps(current, proposed)
    if not added and not removed:
        return None

    header = ["You are modifying runtime dependencies:"]
    for name, spec in sorted(added.items()):
        header.append(f"  + {spec}")
    for name in sorted(removed):
        header.append(f"  - {name}")

    blocks: list[str] = []
    notes: list[str] = []
    try:
        with LexAlignClient(config) as client:
            for name, spec in sorted(added.items()):
                version = extract_pinned_version(spec)
                v = client.check(name, version)
                if v.denied:
                    blocks.append(_format_verdict(v, spec))
                elif v.verdict == "PROVISIONALLY_ALLOWED":
                    suffix = " (run `lex-align-client request-approval` after this lands)" if v.is_requestable else ""
                    notes.append(f"  ◎ {name} — provisional: {v.reason}{suffix}")
                elif v.needs_rationale:
                    notes.append(
                        f"  • {name} — allowed, but registry-approved (neutral); "
                        "document the architectural need in your commit message or PR."
                    )
                else:
                    notes.append(f"  ✓ {name} — {v.reason}")
    except (ServerUnreachable, ServerError) as exc:
        if config.fail_open:
            return ("allow", "\n".join(header + ["", f"[lex-align] {exc} — fail_open=true; allowing edit."]))
        return ("block", f"[lex-align] cannot reach server: {exc}")

    if blocks:
        msg = "\n".join(header + ["", "ENFORCEMENT — blocked by registry:"] + blocks +
                        ["", "No dependencies were modified. Adjust the edit and retry."])
        return ("block", msg)
    if notes:
        return ("allow", "\n".join(header + [""] + notes))
    return ("allow", "\n".join(header))


def _format_verdict(v: Verdict, spec: str) -> str:
    lines = [f"  ✗ {spec} — {v.reason}"]
    if v.replacement:
        lines.append(f"      use instead: {v.replacement}")
    if v.cve_ids:
        lines.append(f"      CVEs: {', '.join(v.cve_ids[:5])}")
    if v.license:
        lines.append(f"      license: {v.license}")
    return "\n".join(lines)


# ── dispatcher ─────────────────────────────────────────────────────────────


def run_hook(name: str) -> int:
    event = _read_event()
    project_root = find_project_root()
    config = load_config(project_root)
    if config is None:
        # Fail soft when not initialized — the dev hasn't run init yet.
        if name == "session-start":
            print("[lex-align] not initialized in this project; skipping brief.")
        elif name == "pre-tool-use":
            _emit_pretool_decision("allow")
        return 0

    if name == "session-start":
        try:
            print(handle_session_start(event, project_root, config))
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[lex-align] session-start failed: {exc}", file=sys.stderr)
        return 0
    if name == "pre-tool-use":
        outcome = handle_pre_tool_use(event, project_root, config)
        if outcome is None:
            _emit_pretool_decision("allow")
        else:
            decision, message = outcome
            _emit_pretool_decision(decision, message)
        return 0
    if name == "session-end":
        return 0
    return 0
