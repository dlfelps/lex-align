from __future__ import annotations

import ast
import datetime
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .licenses import LicenseCache, LicenseInfo, resolve_license, LICENSE_CACHE_FILENAME
from .models import Confidence, Decision, Provenance, Scope, Status
from .reconciler import apply_edit, diff_deps, diff_deps_with_specs, extract_pinned_version, reconcile
from .registry import Action, PackageStatus, Registry, load_registry
from .session import EventLogger, SessionState, clear_current_session, set_current_session_id
from .store import DecisionStore


@dataclass
class HookResult:
    """Outcome a pre/post hook handler returns.

    `decision` is either "allow" (edit proceeds) or "block" (edit refused).
    `message` is shown to the agent verbatim.
    """
    decision: str
    message: Optional[str] = None


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


def _license_cache(project_root: Path) -> LicenseCache:
    return LicenseCache(project_root / ".lex-align" / LICENSE_CACHE_FILENAME)


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

    registry = load_registry(project_root)
    return _build_brief(store, created, registry)


def _build_brief(
    store: DecisionStore, new_observed: list[str], registry: Optional[Registry]
) -> str:
    decisions = store.load_all()
    accepted = [d for d in decisions if d.status == Status.ACCEPTED]
    observed = [d for d in decisions if d.status == Status.OBSERVED]

    lines = [f"# Architecture decisions ({len(accepted)} accepted, {len(observed)} observed)"]

    if registry is not None:
        lines.append(
            f"\nENTERPRISE REGISTRY: v{registry.version}, "
            f"{len(registry.packages)} packages, from {registry.source_path}"
        )
    else:
        lines.append(
            "\nENTERPRISE REGISTRY: not configured. "
            "Run `lex-align init --registry <path>` to enable package enforcement."
        )

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


def handle_pre_tool_use(event: dict, project_root: Path) -> Optional[HookResult]:
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})
    session_id = event.get("session_id", "unknown")
    sessions_dir = _sessions_dir(project_root)
    store = _make_store(project_root)
    state = SessionState(sessions_dir, session_id)

    path_str = tool_input.get("path", "")

    if "pyproject.toml" in path_str:
        return _handle_dep_edit_pre(
            tool_name, tool_input, path_str, store, state, project_root, sessions_dir, session_id
        )

    if path_str.endswith(".py") and tool_name in ("Edit", "Write", "MultiEdit"):
        msg = _handle_code_edit_pre(tool_input, tool_name, path_str, store, state)
        return HookResult(decision="allow", message=msg) if msg else None

    return None


def _handle_dep_edit_pre(
    tool_name: str,
    tool_input: dict,
    path_str: str,
    store: DecisionStore,
    state: SessionState,
    project_root: Path,
    sessions_dir: Path,
    session_id: str,
) -> Optional[HookResult]:
    pyproject_path = project_root / path_str if not Path(path_str).is_absolute() else Path(path_str)
    if not pyproject_path.exists():
        return None

    current_content = pyproject_path.read_text()
    new_content = apply_edit(current_content, tool_name, tool_input)
    added_specs, removed = diff_deps_with_specs(current_content, new_content)
    if not added_specs and not removed:
        return None

    state.record_dep_change(list(set(added_specs) | removed))

    registry = load_registry(project_root)
    logger = EventLogger(sessions_dir, session_id)

    # Header lines shared by allow and block outcomes.
    header: list[str] = ["You are modifying runtime dependencies:"]
    for pkg, spec in sorted(added_specs.items()):
        header.append(f"  + {spec}")
    for pkg in sorted(removed):
        header.append(f"  - {pkg}")

    if registry is None:
        # No policy configured — fall back to advisory behavior.
        header.append(
            "\nNo enterprise registry configured; proceeding without policy enforcement. "
            "Run `lex-align init --registry <path>` to enable enforcement."
        )
        return HookResult(decision="allow", message="\n".join(header))

    blocks: list[str] = []
    allow_messages: list[str] = []
    require_propose: list[str] = []
    cache = _license_cache(project_root)

    for package, spec in sorted(added_specs.items()):
        version = extract_pinned_version(spec)
        verdict = registry.lookup(package, version)

        if verdict.action is Action.BLOCK:
            reason = _format_block_reason(package, spec, verdict, source="registry")
            blocks.append(reason)
            logger.log_automated("enforcement-block", [package])
            _record_blocked_attempt(store, package, spec, verdict, registry.version)
            continue

        if verdict.action is Action.ALLOW and verdict.status is PackageStatus.PREFERRED:
            _auto_write_preferred_adr(store, package, spec, verdict, registry.version)
            allow_messages.append(
                f"  ✓ {package} — preferred in enterprise registry; accepted ADR auto-written."
            )
            logger.log_automated("enforcement-allow", [package])
            continue

        if verdict.action is Action.ALLOW and verdict.status is PackageStatus.VERSION_CONSTRAINED:
            _auto_write_preferred_adr(store, package, spec, verdict, registry.version)
            allow_messages.append(
                f"  ✓ {package} — version-constrained; requested version satisfies "
                f"{verdict.version_constraint}. Accepted ADR auto-written."
            )
            logger.log_automated("enforcement-allow", [package])
            continue

        if verdict.action is Action.REQUIRE_PROPOSE:
            require_propose.append(
                f"  • {package} — approved but neutral. Run "
                f"`lex-align propose --dependency {package} --yes ...` to document "
                "the architectural need before or after this edit."
            )
            logger.log_automated("enforcement-require-propose", [package])
            continue

        if verdict.action is Action.UNKNOWN:
            info, lic_verdict = resolve_license(package, version, cache, registry.global_policies)
            if lic_verdict.action is Action.BLOCK:
                blocks.append(
                    f"  ✗ {package} ({spec}) — {lic_verdict.reason} "
                    f"(license observed: {info.license_normalized or 'UNKNOWN'})"
                )
                logger.log_automated("enforcement-license-block", [package])
                _record_blocked_attempt(
                    store, package, spec, verdict, registry.version,
                    license=info.license_normalized,
                    reason_override=lic_verdict.reason,
                )
                continue
            # license allowed
            _auto_write_license_adr(
                store, package, spec, info, registry.version
            )
            allow_messages.append(
                f"  ✓ {package} — unknown to registry; license {info.license_normalized} "
                "is on the auto-approve list. Accepted ADR auto-written."
            )
            logger.log_automated("enforcement-license-allow", [package])
            continue

    if blocks:
        msg_parts = header + ["", "ENFORCEMENT: hard-block", ""] + blocks
        if allow_messages:
            msg_parts += ["", "Other packages in this edit would have been allowed:"] + allow_messages
        msg_parts += [
            "",
            "No dependencies were modified. Adjust the edit to remove blocked packages.",
        ]
        return HookResult(decision="block", message="\n".join(msg_parts))

    msg_parts = header + [""]
    if allow_messages:
        msg_parts += ["Registry verdicts:"] + allow_messages
    if require_propose:
        msg_parts += ["", "Packages requiring `lex-align propose`:"] + require_propose
    return HookResult(decision="allow", message="\n".join(msg_parts))


def _format_block_reason(package: str, spec: str, verdict, source: str) -> str:
    status_name = verdict.status.value if verdict.status else "unknown"
    lines = [f"  ✗ {package} ({spec}) — {source} verdict: {status_name}"]
    if verdict.reason:
        lines.append(f"      reason: {verdict.reason}")
    if verdict.replacement:
        lines.append(f"      use instead: {verdict.replacement}")
    if verdict.version_constraint:
        lines.append(f"      required: {verdict.version_constraint}")
    return "\n".join(lines)


def _auto_write_preferred_adr(
    store: DecisionStore, package: str, spec: str, verdict, registry_version: str
) -> Decision:
    provenance = Provenance.REGISTRY_PREFERRED
    title = f"Use {package} ({verdict.status.value} in enterprise registry)"
    reason = verdict.reason or "Sanctioned by the enterprise registry."
    decision = Decision(
        id=store.next_id(),
        title=title,
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.HIGH,
        scope=Scope(tags=[package]),
        provenance=provenance,
        version_constraint=verdict.version_constraint,
        registry_version=registry_version,
        context_text=(
            f"The enterprise registry (version {registry_version}) designates "
            f"`{package}` as {verdict.status.value}."
        ),
        decision_text=f"Adopt `{package}` with spec `{spec}`. {reason}",
        consequences_text=(
            "Sanctioned by enterprise policy. Future changes to this dependency "
            "will be re-evaluated against the registry."
        ),
    )
    store.save(decision)
    return decision


def _auto_write_license_adr(
    store: DecisionStore,
    package: str,
    spec: str,
    info: LicenseInfo,
    registry_version: str,
) -> Decision:
    decision = Decision(
        id=store.next_id(),
        title=f"Use {package} (license auto-approved)",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=[package]),
        provenance=Provenance.LICENSE_AUTO_APPROVE,
        license=info.license_normalized,
        license_checked_at=info.fetched_at,
        registry_version=registry_version,
        context_text=(
            f"`{package}` is not listed in the enterprise registry. Its license "
            f"({info.license_raw or 'unspecified'}) normalizes to "
            f"`{info.license_normalized}`, which is on the auto-approve list."
        ),
        decision_text=f"Adopt `{package}` with spec `{spec}` under license {info.license_normalized}.",
        consequences_text=(
            "The dependency is permitted on license grounds alone. If usage grows, "
            "consider promoting this to a registry-level decision."
        ),
    )
    store.save(decision)
    return decision


def _record_blocked_attempt(
    store: DecisionStore,
    package: str,
    spec: str,
    verdict,
    registry_version: str,
    *,
    license: Optional[str] = None,
    reason_override: Optional[str] = None,
) -> Decision:
    reason = reason_override or verdict.reason or "Blocked by enterprise policy."
    decision = Decision(
        id=store.next_id(),
        title=f"Blocked: add {package}",
        status=Status.REJECTED,
        created=datetime.date.today(),
        confidence=Confidence.HIGH,
        scope=Scope(tags=[package]),
        provenance=Provenance.REGISTRY_BLOCKED,
        license=license,
        version_constraint=verdict.version_constraint,
        registry_version=registry_version,
        context_text=(
            f"An agent attempted to add `{package}` with spec `{spec}` to pyproject.toml. "
            "The enterprise registry refused the edit."
        ),
        decision_text=reason + (
            f" Registry-suggested replacement: {verdict.replacement}." if verdict.replacement else ""
        ),
        consequences_text="Dependency was not added. This record exists as an audit trail.",
    )
    store.save(decision)
    return decision


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
            via = f"via {d.provenance.value}" if d.provenance else "observed"
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


def handle_post_tool_use(event: dict, project_root: Path) -> Optional[HookResult]:
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

    if not lines:
        return None
    return HookResult(decision="allow", message="\n".join(lines))


def handle_session_end(event: dict, project_root: Path) -> Optional[str]:
    session_id = event.get("session_id", "unknown")
    sessions_dir = _sessions_dir(project_root)
    state = SessionState(sessions_dir, session_id)
    clear_current_session(sessions_dir)
    state.cleanup()
    # Hard enforcement on PreToolUse makes the final-capture reminder
    # redundant: any dependency addition either landed with an auto-ADR
    # or was hard-blocked. Observed entries created by reconciliation
    # are already surfaced at next session start.
    return None


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
        if isinstance(result, HookResult):
            if result.decision == "block":
                # Claude Code honours "decision": "block" with a "reason" message.
                payload = {"decision": "block", "reason": result.message or "Blocked by lex-align."}
                print(json.dumps(payload))
                # Also emit to stderr so human-running developers see the reason.
                if result.message:
                    print(result.message, file=sys.stderr)
                sys.exit(0)
            payload = {"decision": "allow"}
            if result.message:
                payload["reason"] = result.message
            print(json.dumps(payload))
        else:
            # No result — allow with no message.
            print(json.dumps({"decision": "allow"}))
    else:
        if result:
            print(result)
