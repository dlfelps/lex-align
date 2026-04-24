"""Cold-start compliance check.

Drives a one-shot evaluation of every dependency already declared in
`pyproject.toml` against the enterprise registry and license policy. Designed
to seed lex-align in a project that already has dependencies, so the agent can
begin operating under the registry on the next session start.

Bucketing for each runtime dependency:

  * preferred / version-constrained-satisfied / unknown-but-license-auto-approved
    → write an accepted ADR using the same helpers the PreToolUse hook uses.
  * approved (neutral)
    → write an observed entry. The agent must promote with rationale.
  * deprecated / banned / version-violated / license-blocked /
    license-unknown-under-block-policy
    → blocker. No ADRs are written for any package while a blocker exists.
  * already covered by an accepted ADR
    → skip. The check is idempotent across re-runs.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .hooks import (
    _auto_write_license_adr,
    _auto_write_preferred_adr,
    _license_cache,
)
from .licenses import LicenseCache, LicenseInfo, resolve_license
from .models import Confidence, Decision, Provenance, Scope, Status
from .reconciler import get_runtime_deps
from .registry import Action, PackageStatus, Registry
from .store import DecisionStore


@dataclass
class PackageOutcome:
    """Per-package result of the compliance evaluation."""
    name: str
    bucket: str  # "blocked" | "auto_accepted" | "needs_adr" | "already_covered"
    reason: str
    adr_id: Optional[str] = None
    replacement: Optional[str] = None
    license: Optional[str] = None
    status: Optional[str] = None  # registry status when known


@dataclass
class ComplianceReport:
    blocked: list[PackageOutcome] = field(default_factory=list)
    auto_accepted: list[PackageOutcome] = field(default_factory=list)
    needs_adr: list[PackageOutcome] = field(default_factory=list)
    already_covered: list[PackageOutcome] = field(default_factory=list)
    registry_configured: bool = True
    seeded: bool = False  # True if writes were performed on this run

    @property
    def passing(self) -> bool:
        return not self.blocked and not self.needs_adr

    @property
    def total(self) -> int:
        return (
            len(self.blocked)
            + len(self.auto_accepted)
            + len(self.needs_adr)
            + len(self.already_covered)
        )


def _accepted_covering(store: DecisionStore, package: str) -> Optional[Decision]:
    """Return an existing ACCEPTED decision covering this package, if any."""
    for d in store.find_covering(package):
        if d.status is Status.ACCEPTED:
            return d
    return None


def _observed_covering(store: DecisionStore, package: str) -> Optional[Decision]:
    for d in store.find_covering(package):
        if d.status is Status.OBSERVED:
            return d
    return None


def _create_observed_for_approved(
    store: DecisionStore,
    package: str,
    verdict,
    registry_version: str,
) -> Decision:
    """Write an observed entry for a registry-approved (neutral) package.

    Uses provenance=REGISTRY_APPROVED to distinguish from reconciler-created
    observed entries. The agent is expected to promote this with rationale.
    """
    decision = Decision(
        id=store.next_id(),
        title=f"Uses {package} (approved in enterprise registry)",
        status=Status.OBSERVED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=[package]),
        provenance=Provenance.REGISTRY_APPROVED,
        registry_version=registry_version,
        context_text=(
            f"`{package}` is marked `approved` (neutral) in enterprise registry "
            f"version {registry_version}. The package is permitted but the "
            "architectural rationale for adopting it has not yet been captured."
        ),
    )
    store.save(decision)
    return decision


def analyze(
    pyproject_path: Path,
    store: DecisionStore,
    registry: Optional[Registry],
    license_cache: LicenseCache,
) -> ComplianceReport:
    """Evaluate every runtime dep and bucket the outcomes. No writes."""
    report = ComplianceReport(registry_configured=registry is not None)
    packages = sorted(get_runtime_deps(pyproject_path))

    for package in packages:
        # `find_covering` returns only ACCEPTED + OBSERVED (supersedes/rejected
        # are filtered out). An accepted ADR makes the package fully covered;
        # an observed ADR means it still needs promotion regardless of what
        # the registry currently says.
        existing_accepted = _accepted_covering(store, package)
        if existing_accepted is not None:
            report.already_covered.append(
                PackageOutcome(
                    name=package,
                    bucket="already_covered",
                    reason=f"Already covered by accepted {existing_accepted.id}.",
                    adr_id=existing_accepted.id,
                )
            )
            continue

        existing_observed = _observed_covering(store, package)
        if existing_observed is not None:
            report.needs_adr.append(
                PackageOutcome(
                    name=package,
                    bucket="needs_adr",
                    reason="Observed entry exists; awaiting promotion.",
                    adr_id=existing_observed.id,
                )
            )
            continue

        if registry is None:
            # No registry configured — uncovered deps still need rationale.
            report.needs_adr.append(
                PackageOutcome(
                    name=package,
                    bucket="needs_adr",
                    reason="No registry configured; rationale required.",
                )
            )
            continue

        verdict = registry.lookup(package)

        if verdict.action is Action.BLOCK:
            report.blocked.append(
                PackageOutcome(
                    name=package,
                    bucket="blocked",
                    reason=verdict.reason or "Blocked by enterprise registry.",
                    replacement=verdict.replacement,
                    status=verdict.status.value if verdict.status else None,
                )
            )
            continue

        if verdict.action is Action.ALLOW and verdict.status is PackageStatus.PREFERRED:
            report.auto_accepted.append(
                PackageOutcome(
                    name=package,
                    bucket="auto_accepted",
                    reason=verdict.reason or "Preferred in enterprise registry.",
                    status=verdict.status.value,
                )
            )
            continue

        if (
            verdict.action is Action.ALLOW
            and verdict.status is PackageStatus.VERSION_CONSTRAINED
        ):
            report.auto_accepted.append(
                PackageOutcome(
                    name=package,
                    bucket="auto_accepted",
                    reason=(
                        f"Version-constrained ({verdict.version_constraint}); "
                        "no version pinned in pyproject.toml — accepted as registry-sanctioned."
                    ),
                    status=verdict.status.value,
                )
            )
            continue

        if verdict.action is Action.REQUIRE_PROPOSE:
            report.needs_adr.append(
                PackageOutcome(
                    name=package,
                    bucket="needs_adr",
                    reason=(
                        verdict.reason
                        or "Approved (neutral) in enterprise registry; rationale required."
                    ),
                    status=verdict.status.value if verdict.status else None,
                )
            )
            continue

        # UNKNOWN to registry → license check
        info, lic_verdict = resolve_license(package, None, license_cache, registry.global_policies)
        if lic_verdict.action is Action.BLOCK:
            report.blocked.append(
                PackageOutcome(
                    name=package,
                    bucket="blocked",
                    reason=lic_verdict.reason,
                    license=info.license_normalized,
                )
            )
            continue
        # license auto-approved
        report.auto_accepted.append(
            PackageOutcome(
                name=package,
                bucket="auto_accepted",
                reason=(
                    f"Unknown to registry; license {info.license_normalized} "
                    "is on the auto-approve list."
                ),
                license=info.license_normalized,
            )
        )

    return report


def seed(
    pyproject_path: Path,
    store: DecisionStore,
    registry: Optional[Registry],
    license_cache: LicenseCache,
) -> ComplianceReport:
    """Analyze, then write ADRs and observed entries IF no blockers exist.

    If blockers are present, the report is returned with no writes performed —
    the caller should surface the blockers and refuse to seed.
    """
    report = analyze(pyproject_path, store, registry, license_cache)

    if report.blocked:
        return report

    registry_version = registry.version if registry is not None else "0"

    for outcome in report.auto_accepted:
        if outcome.adr_id is not None:
            continue
        # Re-evaluate the verdict to access the full PackageVerdict object the
        # write helpers expect. Cheap — registry lookups are dict reads.
        if registry is not None:
            verdict = registry.lookup(outcome.name)
        else:
            verdict = None

        if verdict is not None and verdict.status in (
            PackageStatus.PREFERRED, PackageStatus.VERSION_CONSTRAINED
        ):
            decision = _auto_write_preferred_adr(
                store, outcome.name, outcome.name, verdict, registry_version
            )
            outcome.adr_id = decision.id
        elif outcome.license is not None:
            # license auto-approved path — synthesize a minimal LicenseInfo
            info = LicenseInfo(
                license_raw=None,
                license_normalized=outcome.license,
                fetched_at=datetime.date.today(),
                source="pypi",
            )
            decision = _auto_write_license_adr(
                store, outcome.name, outcome.name, info, registry_version
            )
            outcome.adr_id = decision.id

    for outcome in report.needs_adr:
        if outcome.adr_id is not None:
            # Already has an observed entry from a previous run.
            continue
        if registry is not None and outcome.status == PackageStatus.APPROVED.value:
            verdict = registry.lookup(outcome.name)
            decision = _create_observed_for_approved(
                store, outcome.name, verdict, registry_version
            )
        else:
            from .store import create_observed
            decision = create_observed(
                outcome.name, store, Provenance.RECONCILIATION
            )
        outcome.adr_id = decision.id

    report.seeded = True
    return report


# ── Formatting ──────────────────────────────────────────────────────────────


def format_report(report: ComplianceReport) -> str:
    """Render a human-readable summary of the report."""
    lines: list[str] = []
    lines.append(
        f"Analyzed {report.total} runtime dependenc"
        f"{'y' if report.total == 1 else 'ies'} against the registry."
    )
    if not report.registry_configured:
        lines.append(
            "  (No enterprise registry configured — every uncovered dep needs an ADR.)"
        )

    if report.blocked:
        lines.append("")
        lines.append(f"BLOCKERS ({len(report.blocked)}) — must be removed before seeding:")
        for o in report.blocked:
            extras = []
            if o.status:
                extras.append(o.status)
            if o.license:
                extras.append(f"license: {o.license}")
            tag = f" [{', '.join(extras)}]" if extras else ""
            lines.append(f"  ✗ {o.name}{tag} — {o.reason}")
            if o.replacement:
                lines.append(f"      use instead: {o.replacement}")

    if report.auto_accepted:
        lines.append("")
        verb = "AUTO-ACCEPTED" if report.seeded else "WOULD AUTO-ACCEPT"
        lines.append(f"{verb} ({len(report.auto_accepted)}):")
        for o in report.auto_accepted:
            adr_suffix = f" → {o.adr_id}" if o.adr_id else ""
            lines.append(f"  ✓ {o.name}{adr_suffix} — {o.reason}")

    if report.needs_adr:
        lines.append("")
        verb = "NEEDS ADR" if report.seeded else "WOULD NEED ADR"
        lines.append(f"{verb} ({len(report.needs_adr)}):")
        for o in report.needs_adr:
            adr_suffix = f" ({o.adr_id})" if o.adr_id else ""
            lines.append(f"  ⚠ {o.name}{adr_suffix} — {o.reason}")

    if report.already_covered:
        lines.append("")
        lines.append(f"ALREADY COVERED ({len(report.already_covered)}):")
        for o in report.already_covered:
            lines.append(f"  · {o.name} — {o.adr_id}")

    lines.append("")
    if report.blocked:
        lines.append(
            "Cannot seed lex-align until the blockers above are removed from "
            "pyproject.toml. Replace or drop them, then re-run `lex-align compliance`."
        )
    elif report.needs_adr:
        prompt = _agent_prompt(report)
        if prompt:
            lines.append(prompt)
            lines.append("")
        verb = "NOT YET PASSING" if report.seeded else "WOULD NOT YET PASS (dry-run)"
        lines.append(
            f"Compliance: {verb} ({len(report.needs_adr)} observed entr"
            f"{'y' if len(report.needs_adr) == 1 else 'ies'} await promotion)."
        )
    else:
        verb = "PASSING" if report.seeded or report.total == len(report.already_covered) else "WOULD PASS (dry-run)"
        lines.append(
            f"Compliance: {verb}. All runtime dependencies are covered by accepted ADRs."
        )

    return "\n".join(lines)


_AGENT_PROMPT_HEADER = (
    "AGENT PROMPT — copy the block below into your AI agent to capture "
    "rationale for the observed entries:"
)


def _agent_prompt(report: ComplianceReport) -> str:
    pkgs = [(o.name, o.adr_id) for o in report.needs_adr if o.adr_id]
    if not pkgs:
        return ""
    pkg_list = ", ".join(f"`{name}` ({adr_id})" for name, adr_id in pkgs)
    body = (
        f"  For each of the following packages, analyze the codebase to determine "
        f"why it was originally selected — search for imports, examine how it is "
        f"used, and infer the architectural need. Then run "
        f"`lex-align promote <id> --context \"...\" --decision \"...\" "
        f"--consequences \"...\" --yes` for each one, supplying the rationale you "
        f"derived. Packages: {pkg_list}.\n"
        f"  Use `lex-align show <id>` first to see the current observed entry."
    )
    return f"{_AGENT_PROMPT_HEADER}\n{body}"


# ── Public façade ──────────────────────────────────────────────────────────


def run(
    project_root: Path,
    pyproject_path: Path,
    store: DecisionStore,
    registry: Optional[Registry],
    dry_run: bool = False,
) -> ComplianceReport:
    """Convenience wrapper: build the cache and call analyze or seed."""
    cache = _license_cache(project_root)
    if dry_run:
        return analyze(pyproject_path, store, registry, cache)
    return seed(pyproject_path, store, registry, cache)
