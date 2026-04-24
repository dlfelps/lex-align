from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import Optional

import click

from .hooks import run_hook
from .models import Alternative, Confidence, Outcome, Reversible, Scope, Status
from .reconciler import get_runtime_deps, reconcile
from .report import generate_report
from .session import EventLogger, SessionState, get_current_session_id
from .settings import add_lex_hooks, check_hooks_present, detect_lex_command, remove_lex_hooks
from .store import DecisionStore, STOP_WORDS, create_observed, tokenize


def _find_project_root() -> Path:
    path = Path.cwd()
    for parent in [path] + list(path.parents):
        if (parent / ".lex-align").exists():
            return parent
    return path


def _require_initialized(project_root: Path) -> None:
    if not (project_root / ".lex-align").exists():
        raise click.ClickException(
            "lex-align is not initialized in this repository. Run `lex-align init` first."
        )


def _make_store(project_root: Path) -> DecisionStore:
    return DecisionStore(project_root / ".lex-align" / "decisions")


def _sessions_dir(project_root: Path) -> Path:
    return project_root / ".lex-align" / "sessions"


def _get_logger(project_root: Path) -> Optional[EventLogger]:
    sessions_dir = _sessions_dir(project_root)
    session_id = get_current_session_id(sessions_dir)
    if session_id:
        return EventLogger(sessions_dir, session_id)
    return None


def _get_logger_required(project_root: Path) -> EventLogger:
    """Return a logger, creating an ad-hoc session when no session is active.

    Used by write commands (promote, propose) so events are always recorded.
    """
    import uuid
    sessions_dir = _sessions_dir(project_root)
    session_id = get_current_session_id(sessions_dir)
    if not session_id:
        session_id = f"standalone-{uuid.uuid4()}"
    return EventLogger(sessions_dir, session_id)


def _registry_guidance(terms: set, registry) -> list[str]:
    """Surface registry packages whose names appear in the planning prompt.

    Returns pre-formatted two-space-indented lines for inclusion in the plan
    output under the REGISTRY GUIDANCE heading.
    """
    if registry is None:
        return []
    hits: list[str] = []
    for name in sorted(registry.packages):
        token = name.replace("_", "").replace("-", "")
        if name in terms or token in {t.replace("_", "").replace("-", "") for t in terms}:
            rule = registry.packages[name]
            suffix = ""
            if rule.replacement:
                suffix += f" → use `{rule.replacement}` instead"
            vc = rule.version_constraint_str()
            if vc:
                suffix += f" (required {vc})"
            reason = f" — {rule.reason}" if rule.reason else ""
            hits.append(f"  [{rule.status.value}] {name}{suffix}{reason}")
    return hits


def _first_sentence(text: str) -> str:
    if not text:
        return ""
    for sep in (".", "!", "?", "\n"):
        idx = text.find(sep)
        if 0 < idx < 150:
            return text[: idx + 1].strip()
    return text[:150].strip()


_PRIVACY_NOTICE = """\
lex-align records architectural decisions for use by AI agents.

Before initializing, please note:

1. Decision files and the decision index are committed to git and become
   part of your repository's permanent history. Rationale and alternatives
   will be visible to everyone with repo access. Treat them with the same
   sensitivity as source code.

2. Session logs are stored locally under .lex-align/sessions/ and
   are gitignored by default. They contain command metadata, not
   decision content.

3. lex-align does not transmit any data externally. No telemetry,
   no central collection.

4. The aggregate pattern of decisions can reveal information even
   when individual decisions are innocuous.
"""

_CLAUDE_MD_MARKER = "## lex-align"


def _claude_md_section(command: str = "lex-align") -> str:
    return f"""\
## lex-align

Before starting any non-trivial task that may touch dependencies, run:
  {command} plan "<task description>"
This surfaces relevant decisions, previously-evaluated alternatives, active
constraints, and any registry guidance that applies to the task.

When modifying pyproject.toml dependencies:
- The PreToolUse hook will evaluate every added or bumped package against the
  enterprise registry and license policy. It may hard-block the edit.
- For `preferred` packages, the hook auto-writes an accepted ADR; no action needed.
- For `approved` (neutral) packages, run `{command} propose` to document the
  architectural need.
- For `deprecated`, `banned`, or license-blocked packages, use the registry-named
  replacement or choose a different package.

When you encounter an OBSERVED entry for a dependency you are actively using:
- Run `{command} promote <id>`. You likely have enough context from the current
  task. If you genuinely don't, leave it observed.

Run `{command} show <id>` before touching code governed by a decision. Do not
repeat evaluation work already recorded in alternatives.

IMPORTANT for AI agents: NEVER run `{command} propose` or `{command} promote`
without supplying all required flags and `--yes`. These commands have interactive
prompts that will hang a non-interactive session. Always use flags to provide
every field. Run `{command} propose --help` or `{command} promote --help` to see
the required flags.
"""


_FIRST_RUN_MARKER = Path.home() / ".lex-align-initialized"


@click.group()
def main() -> None:
    """lex-align — enterprise legal and architectural alignment for AI coding agents."""


# ── init ──────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--yes", "-y", is_flag=True, help="Skip privacy confirmation prompt.")
@click.option(
    "--registry",
    "registry_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to an enterprise registry JSON file; recorded in .lex-align/config.json.",
)
@click.option(
    "--no-compliance",
    is_flag=True,
    help="Skip the cold-start compliance check of existing pyproject.toml dependencies.",
)
def init(yes: bool, registry_path: Optional[str], no_compliance: bool) -> None:
    """Initialize lex-align in the current repository.

    After configuring hooks, if pyproject.toml has runtime dependencies they
    are evaluated against the registry and seeded into the store. Pass
    --no-compliance to defer this to an explicit `lex-align compliance` run.
    """
    from .registry import Registry, save_config, load_config

    project_root = Path.cwd()

    # Privacy notice on first run
    if not _FIRST_RUN_MARKER.exists():
        click.echo(_PRIVACY_NOTICE)
        if not yes:
            if not click.confirm("Proceed with init?", default=False):
                click.echo("Aborted.")
                return
        _FIRST_RUN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _FIRST_RUN_MARKER.touch()

    lex_dir = project_root / ".lex-align"
    lex_dir.mkdir(exist_ok=True)
    (lex_dir / "decisions").mkdir(exist_ok=True)
    (lex_dir / "sessions").mkdir(exist_ok=True)

    # Gitignore local-only artifacts
    gitignore = project_root / ".gitignore"
    gitignore_entries = [".lex-align/sessions/", ".lex-align/license-cache.json"]
    if gitignore.exists():
        content = gitignore.read_text()
        additions = [e for e in gitignore_entries if e not in content]
        if additions:
            gitignore.write_text(content.rstrip() + "\n" + "\n".join(additions) + "\n")
    else:
        gitignore.write_text("\n".join(gitignore_entries) + "\n")

    # Configure hooks
    add_lex_hooks(project_root)

    registry_msg = None
    if registry_path is not None:
        absolute = Path(registry_path).expanduser().resolve()
        # Validate by attempting a load before persisting.
        Registry.load(absolute)
        config = load_config(project_root)
        try:
            recorded = str(absolute.relative_to(project_root.resolve()))
        except ValueError:
            recorded = str(absolute)
        config["registry_file"] = recorded
        save_config(project_root, config)
        registry_msg = f"Registry configured: {recorded}"

    # CLAUDE.md — agent behavioral rules
    command = detect_lex_command(project_root)
    section = _claude_md_section(command)
    claude_md = project_root / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(section)
        claude_md_msg = "Created CLAUDE.md with lex-align behavioral rules."
    elif _CLAUDE_MD_MARKER not in claude_md.read_text():
        if yes or click.confirm("CLAUDE.md already exists. Append lex-align section?", default=True):
            existing = claude_md.read_text()
            claude_md.write_text(existing.rstrip() + "\n\n" + section)
            claude_md_msg = "Appended lex-align section to CLAUDE.md."
        else:
            claude_md_msg = "Skipped CLAUDE.md (no changes made)."
    else:
        claude_md_msg = "CLAUDE.md already contains lex-align section (skipped)."

    click.echo("Initialized lex-align.")
    click.echo("Hooks configured in .claude/settings.json.")
    if registry_msg:
        click.echo(registry_msg)
    click.echo(claude_md_msg)

    # Cold-start compliance check: seed the store from existing dependencies so
    # the hooks aren't installed against an empty store with a deps-laden
    # pyproject. Opt out with --no-compliance.
    pyproject = project_root / "pyproject.toml"
    if not no_compliance and pyproject.exists():
        from .reconciler import get_runtime_deps
        deps = get_runtime_deps(pyproject)
        if deps:
            from . import compliance as compliance_mod
            from .registry import load_registry
            click.echo("")
            click.echo(
                f"Running compliance check on {len(deps)} existing runtime "
                f"dependenc{'y' if len(deps) == 1 else 'ies'}..."
            )
            click.echo("")
            registry = load_registry(project_root)
            store = _make_store(project_root)
            report = compliance_mod.run(
                project_root, pyproject, store, registry, dry_run=False
            )
            click.echo(compliance_mod.format_report(report))
            if report.blocked:
                click.echo("")
                click.echo(
                    "Init completed, but compliance found blockers. Resolve them "
                    "and re-run `lex-align compliance`."
                )


# ── show ──────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("adr_id")
def show(adr_id: str) -> None:
    """Display a full decision record."""
    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)
    decision = store.get(adr_id)
    if decision is None:
        raise click.ClickException(f"Decision {adr_id} not found.")

    logger = _get_logger(project_root)
    if logger:
        logger.log_voluntary("show", [decision.id])

    lines = [f"# {decision.id}: {decision.title}"]
    lines.append(f"Status: {decision.status.value}  |  Confidence: {decision.confidence.value}  |  Created: {decision.created}")
    if decision.scope.tags:
        lines.append(f"Tags: {', '.join(decision.scope.tags)}")
    if decision.scope.paths:
        lines.append(f"Paths: {', '.join(decision.scope.paths)}")
    if decision.supersedes:
        lines.append(f"Supersedes: {', '.join(decision.supersedes)}")
    if decision.superseded_by:
        lines.append(f"Superseded by: {', '.join(decision.superseded_by)}")
    if decision.constraints_depended_on:
        lines.append(f"Constraints: {', '.join(decision.constraints_depended_on)}")
    if decision.provenance:
        lines.append(f"Observed via: {decision.provenance.value}")

    if decision.alternatives:
        lines.append("\nAlternatives:")
        for alt in decision.alternatives:
            rev = f"reversible: {alt.reversible.value}"
            constraint = f", constraint: {alt.constraint}" if alt.constraint else ""
            lines.append(f"  [{alt.outcome.value}] {alt.name}")
            lines.append(f"    {alt.reason} ({rev}{constraint})")

    if decision.context_text:
        lines.append(f"\n## Context\n{decision.context_text}")
    if decision.decision_text:
        lines.append(f"\n## Decision\n{decision.decision_text}")
    if decision.consequences_text:
        lines.append(f"\n## Consequences\n{decision.consequences_text}")

    click.echo("\n".join(lines))

    if decision.status == Status.OBSERVED:
        click.echo(
            f"\n[Observed entry] Run `lex-align promote {decision.id}` to capture rationale if you have context."
        )


# ── plan ──────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("prompt")
def plan(prompt: str) -> None:
    """Get relevant architectural context for a task before starting."""
    from .registry import load_registry

    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)

    logger = _get_logger(project_root)
    if logger:
        logger.log_voluntary("plan", [prompt[:200]])

    terms = tokenize(prompt) - STOP_WORDS
    registry = load_registry(project_root)
    registry_hits = _registry_guidance(terms, registry) if registry is not None else []

    if not terms and not registry_hits:
        click.echo("No meaningful terms found in prompt.")
        click.echo("Run `lex-align propose` when ready to record a decision.")
        return

    candidates = store.search_by_terms(terms) if terms else []
    if not candidates and not registry_hits:
        click.echo("No relevant decisions or registry matches found for this task.")
        click.echo("Run `lex-align propose` when ready to record a decision.")
        return

    accepted = [d for d in candidates if d.status == Status.ACCEPTED]
    observed = [d for d in candidates if d.status == Status.OBSERVED]

    lines: list[str] = []

    if registry_hits:
        lines.append("REGISTRY GUIDANCE")
        for entry in registry_hits:
            lines.append(entry)

    if accepted:
        if lines:
            lines.append("")
        lines.append("RELEVANT DECISIONS")
        for d in sorted(accepted, key=lambda x: x.created, reverse=True):
            lines.append(f"  {d.id} ({d.status.value}) {d.title}")
            snippet = _first_sentence(d.decision_text)
            if snippet:
                lines.append(f"    {snippet}")

    if observed:
        if lines:
            lines.append("")
        lines.append("OBSERVED ENTRIES THAT MAY BE AFFECTED")
        for d in sorted(observed, key=lambda x: x.created, reverse=True):
            lines.append(f"  {d.id} {d.title} (no rationale captured)")

    # Group alternatives by name, collecting (alt, decision) pairs
    alt_groups: dict[str, list[tuple]] = {}
    for d in candidates:
        for alt in d.alternatives:
            if alt.outcome in (Outcome.NOT_CHOSEN, Outcome.REJECTED):
                key = alt.name.lower()
                alt_groups.setdefault(key, []).append((alt, d))

    if alt_groups:
        if lines:
            lines.append("")
        lines.append("WHAT HAS BEEN CONSIDERED")
        for entries in alt_groups.values():
            first_alt = entries[0][0]
            lines.append(f"  {first_alt.name}")
            for alt, d in entries:
                label = "NOT-CHOSEN" if alt.outcome == Outcome.NOT_CHOSEN else "REJECTED"
                lines.append(
                    f'    {label} in {d.id} ({d.created}): "{alt.reason}" — reversible: {alt.reversible.value}'
                )
            if any(alt.outcome == Outcome.NOT_CHOSEN for alt, _ in entries):
                lines.append("    This alternative may be worth revisiting if conditions have changed.")

    # Collect constraints from matching decisions and their alternatives
    constraints_map: dict[str, list[str]] = {}
    for d in candidates:
        for c in d.constraints_depended_on:
            constraints_map.setdefault(c, [])
            if d.id not in constraints_map[c]:
                constraints_map[c].append(d.id)
        for alt in d.alternatives:
            if alt.constraint:
                constraints_map.setdefault(alt.constraint, [])
                if d.id not in constraints_map[alt.constraint]:
                    constraints_map[alt.constraint].append(d.id)

    if constraints_map:
        if lines:
            lines.append("")
        lines.append("ACTIVE CONSTRAINTS RELEVANT TO THIS TASK")
        for constraint, refs in sorted(constraints_map.items()):
            lines.append(f"  {constraint} (referenced by {', '.join(refs)})")
            for ref_id in refs:
                ref_d = next((d for d in candidates if d.id == ref_id), None) or store.get(ref_id)
                if ref_d and ref_d.decision_text:
                    snippet = _first_sentence(ref_d.decision_text)
                    if snippet:
                        lines.append(f"    {snippet}")
                    break

    if lines:
        lines.append("")
    lines.append("Run `lex-align show <id>` for full rationale on any entry above.")
    lines.append("Run `lex-align propose` when you are ready to record your decision.")

    click.echo("\n".join(lines))


# ── history ───────────────────────────────────────────────────────────────────

@main.command()
@click.argument("path_or_tag")
def history(path_or_tag: str) -> None:
    """Show all decisions that have governed a path or tag, chronologically."""
    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)

    logger = _get_logger(project_root)
    if logger:
        logger.log_voluntary("history", [path_or_tag])

    decisions = store.history(path_or_tag)
    if not decisions:
        click.echo(f"No decisions found for '{path_or_tag}'.")
        return

    for d in decisions:
        sup_note = f" → superseded by {d.superseded_by[0]}" if d.superseded_by else ""
        click.echo(f"{d.id} ({d.created})  [{d.status.value}]{sup_note}")
        click.echo(f"  {d.title}")


# ── check-constraint ──────────────────────────────────────────────────────────

@main.command("check-constraint")
@click.argument("tag")
def check_constraint(tag: str) -> None:
    """Find all decisions and alternatives that depend on a constraint tag."""
    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)

    logger = _get_logger(project_root)
    if logger:
        logger.log_voluntary("check-constraint", [tag])

    results = store.check_constraint(tag)
    if not results:
        click.echo(f"No decisions reference constraint '{tag}'.")
        return

    for decision, alt_matches in results:
        constraint_in_decision = tag.lower() in [c.lower() for c in decision.constraints_depended_on]
        if constraint_in_decision:
            click.echo(f"{decision.id}: {decision.title}")
            click.echo(f"  Constraint '{tag}' is depended upon by this decision.")
        for alt in alt_matches:
            click.echo(f"  Alternative '{alt.name}' was {alt.outcome.value} due to constraint '{tag}'.")
            click.echo(f"  Reason: {alt.reason}")


# ── propose ───────────────────────────────────────────────────────────────────

@main.command()
@click.option("--dependency", default=None, help="Dependency change that triggered this proposal.")
@click.option("--relevant-adrs", default=None, help="Comma-separated ADR IDs relevant to this proposal.")
@click.option("--path", "scope_path", default=None, help="File path scope hint.")
@click.option("--title", default=None, help="Decision title (one sentence).")
@click.option(
    "--confidence",
    "confidence_flag",
    default=None,
    type=click.Choice(["low", "medium", "high"]),
    help="Confidence level.",
)
@click.option("--tags", default=None, help="Scope tags (comma-separated).")
@click.option("--paths", "paths_flag", default=None, help="Scope paths (comma-separated).")
@click.option("--constraints", default=None, help="Constraints depended on (comma-separated).")
@click.option("--supersedes", default=None, help="ADR IDs superseded by this decision (comma-separated).")
@click.option(
    "--alternatives-json",
    default=None,
    help=(
        'Alternatives as a JSON array, e.g. \'[{"name":"X","outcome":"not-chosen",'
        '"reason":"Y","reversible":"cheap","constraint":null}]\'. '
        "Bypasses the interactive alternatives loop."
    ),
)
@click.option("--yes", "-y", is_flag=True, help="Non-interactive: skip all prompts and use provided flags or defaults.")
@click.option("--context", "context_prose", default=None, help="ADR context prose (background and problem).")
@click.option("--decision", "decision_prose", default=None, help="ADR decision prose (what was decided).")
@click.option("--consequences", "consequences_prose", default=None, help="ADR consequences prose (outcomes, positive and negative).")
def propose(
    dependency: Optional[str],
    relevant_adrs: Optional[str],
    scope_path: Optional[str],
    title: Optional[str],
    confidence_flag: Optional[str],
    tags: Optional[str],
    paths_flag: Optional[str],
    constraints: Optional[str],
    supersedes: Optional[str],
    alternatives_json: Optional[str],
    yes: bool,
    context_prose: Optional[str],
    decision_prose: Optional[str],
    consequences_prose: Optional[str],
) -> None:
    """Record a new architectural decision (non-interactive with --yes)."""
    from .registry import Action, PackageStatus, load_registry

    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)

    # If a dependency is named and the registry says it is banned or
    # deprecated, the registry is authoritative — refuse the propose.
    if dependency:
        registry = load_registry(project_root)
        if registry is not None:
            verdict = registry.lookup(dependency)
            if verdict.action is Action.BLOCK and verdict.status in (
                PackageStatus.BANNED, PackageStatus.DEPRECATED
            ):
                msg = f"Cannot propose `{dependency}`: enterprise registry status is {verdict.status.value}."
                if verdict.reason:
                    msg += f"\n  reason: {verdict.reason}"
                if verdict.replacement:
                    msg += f"\n  use instead: {verdict.replacement}"
                raise click.ClickException(msg)

    # Pre-fill defaults from triggered context
    default_title = f"Add {dependency}" if dependency else ""
    default_scope_tags = dependency or ""
    default_scope_paths = scope_path or ""

    relevant_decisions = []
    if relevant_adrs:
        for adr_id in relevant_adrs.split(","):
            d = store.get(adr_id.strip())
            if d:
                relevant_decisions.append(d)
                click.echo(f"Relevant decision: {d.id}: {d.title}")

    # Non-TTY stdin is a hard signal that we're running under an agent; auto-enable
    # --yes so we never hang on a click.prompt that has no reader.
    if not yes and not sys.stdin.isatty():
        yes = True
        click.echo(
            "lex-align: stdin is not a TTY; running non-interactively (--yes auto-enabled).",
            err=True,
        )

    if yes:
        missing = [
            name for name, value in (
                ("--title", title),
                ("--context", context_prose),
                ("--decision", decision_prose),
                ("--consequences", consequences_prose),
            ) if not value
        ]
        if missing:
            raise click.UsageError(
                f"Non-interactive propose requires: {', '.join(missing)}. "
                "Pass every flag with --yes to avoid the interactive prompts."
            )
        title_val = title
        confidence = Confidence(confidence_flag or "medium")
        tags_raw = tags or default_scope_tags
        paths_raw = paths_flag or default_scope_paths
        constraints_raw = constraints or ""
        supersedes_raw = supersedes or ""
    else:
        title_val = click.prompt("Decision title (one sentence)", default=title or default_title or "")
        confidence_raw = click.prompt(
            "Confidence",
            type=click.Choice(["low", "medium", "high"]),
            default=confidence_flag or "medium",
        )
        confidence = Confidence(confidence_raw)
        tags_raw = click.prompt("Scope tags (comma-separated, or empty)", default=tags or default_scope_tags)
        paths_raw = click.prompt("Scope paths (comma-separated, or empty)", default=paths_flag or default_scope_paths)
        constraints_raw = click.prompt("Constraints depended on (comma-separated, or empty)", default=constraints or "")
        supersedes_raw = click.prompt("Supersedes (ADR IDs, comma-separated, or empty)", default=supersedes or "")
        if context_prose is None:
            context_prose = click.prompt("Context (background and problem that prompted this decision)", default="")
        if decision_prose is None:
            decision_prose = click.prompt("Decision (what was decided)", default="")
        if consequences_prose is None:
            consequences_prose = click.prompt("Consequences (outcomes, positive and negative)", default="")

    tag_list = [t.strip() for t in tags_raw.split(",") if t.strip()]
    path_list = [p.strip() for p in paths_raw.split(",") if p.strip()]
    constraint_list = [c.strip() for c in constraints_raw.split(",") if c.strip()]
    supersedes_list = [s.strip() for s in supersedes_raw.split(",") if s.strip()]

    alternatives: list[Alternative] = []
    if alternatives_json is not None:
        try:
            raw_alts = json.loads(alternatives_json)
        except json.JSONDecodeError as exc:
            raise click.UsageError(f"--alternatives-json is not valid JSON: {exc}") from exc
        for item in raw_alts:
            alternatives.append(
                Alternative(
                    name=item["name"],
                    outcome=Outcome(item["outcome"]),
                    reason=item["reason"],
                    reversible=Reversible(item["reversible"]),
                    constraint=item.get("constraint") or None,
                )
            )
    elif not yes:
        while click.confirm("Add an alternative?", default=False):
            alt_name = click.prompt("Alternative name")
            alt_outcome_raw = click.prompt(
                "Outcome",
                type=click.Choice(["chosen", "not-chosen", "rejected"]),
            )
            alt_reason = click.prompt("Reason")
            alt_reversible_raw = click.prompt(
                "Reversible",
                type=click.Choice(["cheap", "costly", "no"]),
            )
            alt_constraint = click.prompt("Constraint (optional, or empty)", default="") or None
            alternatives.append(
                Alternative(
                    name=alt_name,
                    outcome=Outcome(alt_outcome_raw),
                    reason=alt_reason,
                    reversible=Reversible(alt_reversible_raw),
                    constraint=alt_constraint,
                )
            )

    context_text, decision_text, consequences_text = context_prose or "", decision_prose or "", consequences_prose or ""

    adr_id = store.next_id()
    from .models import Decision
    decision = Decision(
        id=adr_id,
        title=title_val,
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=confidence,
        scope=Scope(tags=tag_list, paths=path_list),
        alternatives=alternatives,
        supersedes=supersedes_list,
        constraints_depended_on=constraint_list,
        context_text=context_text,
        decision_text=decision_text,
        consequences_text=consequences_text,
    )
    path = store.save(decision)

    # Update superseded_by on parent decisions
    for sup_id in supersedes_list:
        sup_decision = store.get(sup_id)
        if sup_decision:
            if adr_id not in sup_decision.superseded_by:
                sup_decision.superseded_by.append(adr_id)
            sup_decision.status = Status.SUPERSEDED
            store.save(sup_decision)

    # Log and update session state
    logger = _get_logger_required(project_root)
    logger.log_voluntary("propose", [adr_id])

    sessions_dir = _sessions_dir(project_root)
    session_id = get_current_session_id(sessions_dir)
    if session_id:
        state = SessionState(sessions_dir, session_id)
        state.record_propose_called([dependency] if dependency else [])

    click.echo(f"\nWritten: {path}")


# ── promote ───────────────────────────────────────────────────────────────────

@main.command()
@click.argument("adr_id")
@click.option("--context", "context_text", default=None, help="Context explaining the original choice.")
@click.option(
    "--confidence",
    "confidence_flag",
    default=None,
    type=click.Choice(["low", "medium", "high"]),
    help="Confidence level.",
)
@click.option("--tags", default=None, help="Scope tags (comma-separated).")
@click.option("--paths", "paths_flag", default=None, help="Scope paths (comma-separated).")
@click.option("--constraints", default=None, help="Constraints depended on (comma-separated).")
@click.option(
    "--alternatives-json",
    default=None,
    help=(
        'Alternatives to append as a JSON array, e.g. \'[{"name":"X","outcome":"not-chosen",'
        '"reason":"Y","reversible":"cheap","constraint":null}]\'. '
        "Bypasses the interactive alternatives loop."
    ),
)
@click.option("--yes", "-y", is_flag=True, help="Non-interactive: skip all prompts and use provided flags or defaults.")
@click.option("--decision", "decision_prose", default=None, help="ADR decision prose (bypasses LLM generation when all three prose flags are set).")
@click.option("--consequences", "consequences_prose", default=None, help="ADR consequences prose (bypasses LLM generation when all three prose flags are set).")
def promote(
    adr_id: str,
    context_text: Optional[str],
    confidence_flag: Optional[str],
    tags: Optional[str],
    paths_flag: Optional[str],
    constraints: Optional[str],
    alternatives_json: Optional[str],
    yes: bool,
    decision_prose: Optional[str],
    consequences_prose: Optional[str],
) -> None:
    """Promote an observed entry to accepted (non-interactive with --yes)."""
    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)

    decision = store.get(adr_id)
    if decision is None:
        raise click.ClickException(f"Decision {adr_id} not found.")
    if decision.status != Status.OBSERVED:
        raise click.ClickException(f"{adr_id} is not an observed entry (status: {decision.status.value}).")

    click.echo(f"Promoting {decision.id}: {decision.title}")

    if not yes and not sys.stdin.isatty():
        yes = True
        click.echo(
            "lex-align: stdin is not a TTY; running non-interactively (--yes auto-enabled).",
            err=True,
        )

    if yes:
        if not context_text:
            raise click.UsageError(
                "Non-interactive promote requires --context. "
                "Supply it along with --yes to skip the interactive prompts."
            )
        confidence = Confidence(confidence_flag or "medium")
        tags_raw = tags if tags is not None else ",".join(decision.scope.tags)
        paths_raw = paths_flag if paths_flag is not None else ",".join(decision.scope.paths)
        constraints_raw = constraints or ""
    else:
        if context_text is None:
            context_text = click.prompt(
                "Provide context for why this dependency was originally adopted "
                "(or describe what you know about it)"
            )
        confidence_raw = click.prompt(
            "Confidence",
            type=click.Choice(["low", "medium", "high"]),
            default=confidence_flag or "medium",
        )
        confidence = Confidence(confidence_raw)
        tags_raw = click.prompt("Scope tags (comma-separated, or empty)", default=tags or ",".join(decision.scope.tags))
        paths_raw = click.prompt("Scope paths (comma-separated, or empty)", default=paths_flag or ",".join(decision.scope.paths))
        constraints_raw = click.prompt("Constraints depended on (comma-separated, or empty)", default=constraints or "")

    tag_list = [t.strip() for t in tags_raw.split(",") if t.strip()]
    path_list = [p.strip() for p in paths_raw.split(",") if p.strip()]
    constraint_list = [c.strip() for c in constraints_raw.split(",") if c.strip()]

    alternatives: list[Alternative] = list(decision.alternatives)
    if alternatives_json is not None:
        try:
            raw_alts = json.loads(alternatives_json)
        except json.JSONDecodeError as exc:
            raise click.UsageError(f"--alternatives-json is not valid JSON: {exc}") from exc
        for item in raw_alts:
            alternatives.append(
                Alternative(
                    name=item["name"],
                    outcome=Outcome(item["outcome"]),
                    reason=item["reason"],
                    reversible=Reversible(item["reversible"]),
                    constraint=item.get("constraint") or None,
                )
            )
    elif not yes:
        if click.confirm("Add alternatives?", default=False):
            while True:
                alt_name = click.prompt("Alternative name")
                alt_outcome_raw = click.prompt("Outcome", type=click.Choice(["chosen", "not-chosen", "rejected"]))
                alt_reason = click.prompt("Reason")
                alt_reversible_raw = click.prompt("Reversible", type=click.Choice(["cheap", "costly", "no"]))
                alt_constraint = click.prompt("Constraint (optional, or empty)", default="") or None
                alternatives.append(
                    Alternative(
                        name=alt_name,
                        outcome=Outcome(alt_outcome_raw),
                        reason=alt_reason,
                        reversible=Reversible(alt_reversible_raw),
                        constraint=alt_constraint,
                    )
                )
                if not click.confirm("Add another alternative?", default=False):
                    break

    new_context = context_text or decision.context_text or ""
    new_decision = decision_prose or decision.decision_text or ""
    new_consequences = consequences_prose or decision.consequences_text or ""

    decision.status = Status.ACCEPTED
    decision.confidence = confidence
    decision.scope = Scope(tags=tag_list, paths=path_list)
    decision.constraints_depended_on = constraint_list
    decision.alternatives = alternatives
    decision.context_text = new_context
    decision.decision_text = new_decision
    decision.consequences_text = new_consequences

    path = store.save(decision)

    logger = _get_logger_required(project_root)
    logger.log_voluntary("promote", [decision.id])

    click.echo(f"\nPromoted {decision.id} to accepted. Written: {path}")


# ── report ────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--since", default=None, help="Filter events since this time (e.g. '2 weeks ago').")
def report(since: Optional[str]) -> None:
    """Display a summary of lex-align activity and store integrity."""
    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)
    sessions_dir = _sessions_dir(project_root)
    click.echo(generate_report(sessions_dir, store, since_str=since))


# ── compliance ────────────────────────────────────────────────────────────────

@main.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Analyze and report without writing any ADRs or observed entries.",
)
@click.option(
    "--registry",
    "registry_path",
    default=None,
    help="Override the configured registry file path.",
)
def compliance(dry_run: bool, registry_path: Optional[str]) -> None:
    """Cold-start: evaluate every existing dependency against the registry.

    Writes accepted ADRs for preferred and license-auto-approved packages,
    creates observed entries for registry-`approved` packages (which the agent
    must then promote with rationale), and refuses to seed if any blockers
    (banned, deprecated, license-blocked) are present.

    Exit codes: 0 = passing, 1 = needs ADR(s), 2 = blockers present.
    """
    from . import compliance as compliance_mod
    from .registry import load_registry

    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)
    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        raise click.ClickException("No pyproject.toml found at project root.")

    registry = load_registry(project_root, registry_path)

    logger = _get_logger(project_root)
    if logger:
        logger.log_voluntary("compliance", ["dry-run"] if dry_run else [])

    report = compliance_mod.run(
        project_root, pyproject, store, registry, dry_run=dry_run
    )
    click.echo(compliance_mod.format_report(report))

    if report.blocked:
        sys.exit(2)
    if report.needs_adr:
        sys.exit(1)
    sys.exit(0)


# ── doctor ────────────────────────────────────────────────────────────────────

@main.command()
@click.option(
    "--repair",
    is_flag=True,
    help="Repair missing hook configuration and/or rebuild a stale decision index.",
)
def doctor(repair: bool) -> None:
    """Check hook configuration and decision-index health."""
    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)

    hook_status = check_hooks_present(project_root)
    hooks_ok = all(hook_status.values())
    index_ok = store.index_is_healthy()

    for event, present in hook_status.items():
        mark = "✓" if present else "✗"
        click.echo(f"  {mark} {event} hook")
    click.echo(f"  {'✓' if index_ok else '✗'} decision index")

    if hooks_ok and index_ok:
        click.echo("Configuration is healthy.")
        return

    if not hooks_ok:
        click.echo("Some hooks are missing or misconfigured.")
    if not index_ok:
        click.echo("Decision index is missing or out of sync with decision files.")

    if repair:
        if not hooks_ok:
            add_lex_hooks(project_root)
            click.echo("Repaired hook configuration.")
        if not index_ok:
            store.rebuild_index()
            count = len(store.load_all())
            click.echo(
                f"Rebuilt index from {count} decision "
                f"{'file' if count == 1 else 'files'}."
            )
    else:
        click.echo("Run `lex-align doctor --repair` to fix.")


# ── uninstall ─────────────────────────────────────────────────────────────────

@main.command()
@click.option("--yes", "-y", is_flag=True)
def uninstall(yes: bool) -> None:
    """Remove lex-align hook configuration from .claude/settings.json."""
    project_root = _find_project_root()
    _require_initialized(project_root)

    if not yes:
        if not click.confirm("Remove lex-align hooks from .claude/settings.json?", default=False):
            click.echo("Aborted.")
            return

    remove_lex_hooks(project_root)
    click.echo("lex-align hooks removed from .claude/settings.json.")
    click.echo("The .lex-align/ directory and decision files are preserved.")


# ── privacy ───────────────────────────────────────────────────────────────────

@main.command()
def privacy() -> None:
    """Display the privacy notice."""
    click.echo(_PRIVACY_NOTICE)


# ── registry ──────────────────────────────────────────────────────────────────

@main.group()
def registry() -> None:
    """Inspect or query the enterprise registry."""


@registry.command("show")
@click.option("--registry", "registry_path", default=None,
              help="Override the configured registry file path.")
def registry_show(registry_path: Optional[str]) -> None:
    """Print the effective registry (global policies + packages)."""
    from .registry import load_registry, resolve_registry_path

    project_root = _find_project_root()
    path = resolve_registry_path(project_root, registry_path)
    if path is None:
        raise click.ClickException(
            "No registry configured. Pass --registry <path> or set LEXALIGN_REGISTRY_FILE, "
            "or run `lex-align init --registry <path>`."
        )
    reg = load_registry(project_root, registry_path)
    if reg is None:
        raise click.ClickException(f"Registry file not found: {path}")

    click.echo(f"Registry: {path}")
    click.echo(f"Version: {reg.version}")
    gp = reg.global_policies
    click.echo("")
    click.echo("Global policies")
    click.echo(f"  auto_approve_licenses:         {', '.join(gp.auto_approve_licenses) or '(none)'}")
    click.echo(f"  hard_ban_licenses:             {', '.join(gp.hard_ban_licenses) or '(none)'}")
    if gp.require_human_review_licenses:
        click.echo(
            f"  require_human_review_licenses: {', '.join(gp.require_human_review_licenses)} "
            "(treated as hard_ban until review flow is implemented)"
        )
    click.echo(f"  unknown_license_policy:        {gp.unknown_license_policy}")
    click.echo("")
    click.echo(f"Packages ({len(reg.packages)})")
    for name in sorted(reg.packages):
        rule = reg.packages[name]
        suffix = ""
        if rule.replacement:
            suffix = f" → {rule.replacement}"
        vc = rule.version_constraint_str()
        if vc:
            suffix += f" ({vc})"
        reason = f" — {rule.reason}" if rule.reason else ""
        click.echo(f"  [{rule.status.value}] {name}{suffix}{reason}")


@registry.command("check")
@click.argument("package")
@click.option("--version", default=None, help="Target version to evaluate against constraints.")
@click.option("--registry", "registry_path", default=None,
              help="Override the configured registry file path.")
def registry_check(package: str, version: Optional[str], registry_path: Optional[str]) -> None:
    """Show the registry verdict for a package (and optional version)."""
    from .registry import load_registry, Action

    project_root = _find_project_root()
    reg = load_registry(project_root, registry_path)
    if reg is None:
        raise click.ClickException(
            "No registry configured. Pass --registry <path> or set LEXALIGN_REGISTRY_FILE."
        )

    verdict = reg.lookup(package, version)
    click.echo(f"Package: {package}")
    if version:
        click.echo(f"Version: {version}")
    click.echo(f"Action:  {verdict.action.value}")
    if verdict.status is not None:
        click.echo(f"Status:  {verdict.status.value}")
    if verdict.reason:
        click.echo(f"Reason:  {verdict.reason}")
    if verdict.replacement:
        click.echo(f"Replacement: {verdict.replacement}")
    if verdict.version_constraint:
        click.echo(f"Version constraint: {verdict.version_constraint}")
    if verdict.action is Action.UNKNOWN:
        click.echo("(Package is not in the registry; license policy will apply.)")


# ── hook dispatch (hidden) ────────────────────────────────────────────────────

@main.command(hidden=True)
@click.argument("name")
def hook(name: str) -> None:
    """Hook dispatch entry point invoked by the Claude Code wrapper script."""
    run_hook(name)
