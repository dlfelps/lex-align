"""Evaluation orchestrator.

Combines registry lookup, CVE check, and license check into a single verdict.
Emits one audit-log row per call. The order matters:

  1. registry hard-blocks (banned, deprecated, version-violated)
  2. CVE check — applies even to registry-allowed packages so a newly
     published critical CVE on a `preferred` package still blocks
  3. license check — only when the package is unknown to the registry
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

from .audit import (
    AuditRecord,
    AuditStore,
    DENIAL_CVE,
    DENIAL_LICENSE,
    DENIAL_NONE,
    DENIAL_REGISTRY,
    VERDICT_ALLOWED,
    VERDICT_DENIED,
    VERDICT_PROVISIONALLY_ALLOWED,
)
from .auth import AgentInfo
from .cache import JsonCache
from .config import Settings
from .cve import resolve_cves, CveInfo
from .licenses import resolve_license, evaluate_license, LicenseInfo
from .registry import (
    Action,
    PackageStatus,
    PackageVerdict,
    Registry,
)


@dataclass
class EvaluationResult:
    verdict: str                       # ALLOWED | DENIED | PROVISIONALLY_ALLOWED
    reason: str
    package: str
    version: Optional[str]
    resolved_version: Optional[str]
    registry_status: Optional[str]
    replacement: Optional[str] = None
    version_constraint: Optional[str] = None
    license: Optional[str] = None
    cve_ids: list[str] = None  # type: ignore[assignment]
    max_cvss: Optional[float] = None
    is_requestable: bool = False
    needs_rationale: bool = False
    auto_request_approval: bool = False

    def __post_init__(self) -> None:
        if self.cve_ids is None:
            self.cve_ids = []

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "package": self.package,
            "version": self.version,
            "resolved_version": self.resolved_version,
            "registry_status": self.registry_status,
            "replacement": self.replacement,
            "version_constraint": self.version_constraint,
            "license": self.license,
            "cve_ids": self.cve_ids,
            "max_cvss": self.max_cvss,
            "is_requestable": self.is_requestable,
            "needs_rationale": self.needs_rationale,
            "auto_request_approval": self.auto_request_approval,
        }


def _denied(
    *,
    package: str,
    version: Optional[str],
    resolved_version: Optional[str],
    reason: str,
    registry_status: Optional[str],
    replacement: Optional[str] = None,
    version_constraint: Optional[str] = None,
    license: Optional[str] = None,
    cves: Optional[CveInfo] = None,
) -> EvaluationResult:
    return EvaluationResult(
        verdict=VERDICT_DENIED,
        reason=reason,
        package=package,
        version=version,
        resolved_version=resolved_version,
        registry_status=registry_status,
        replacement=replacement,
        version_constraint=version_constraint,
        license=license,
        cve_ids=list(cves.ids) if cves else [],
        max_cvss=cves.max_score if cves else None,
        is_requestable=False,
    )


async def evaluate(
    *,
    package: str,
    version: Optional[str],
    project: str,
    requester: str,
    registry: Optional[Registry],
    cache: JsonCache,
    audit: AuditStore,
    settings: Settings,
    http_client: httpx.AsyncClient,
    agent: Optional[AgentInfo] = None,
) -> EvaluationResult:
    """Single evaluation. Always writes one audit row before returning."""
    agent = agent or AgentInfo()
    # Step 1 — registry verdict (only if a registry is configured).
    pkg_verdict: Optional[PackageVerdict] = None
    if registry is not None:
        pkg_verdict = registry.lookup(package, version)
        if pkg_verdict.action is Action.BLOCK:
            result = _denied(
                package=package,
                version=version,
                resolved_version=None,
                reason=pkg_verdict.reason or "Blocked by enterprise registry.",
                registry_status=pkg_verdict.status.value if pkg_verdict.status else None,
                replacement=pkg_verdict.replacement,
                version_constraint=pkg_verdict.version_constraint,
            )
            await _audit(audit, result, project, requester, agent, DENIAL_REGISTRY)
            return result

    # Step 2 — license + latest version (PyPI). We need PyPI even for known
    # packages so we can resolve "latest" when no version is provided.
    license_info: Optional[LicenseInfo] = None
    latest_version: Optional[str] = None
    if registry is None or pkg_verdict is None or pkg_verdict.action is Action.UNKNOWN:
        license_info, latest_version = await resolve_license(
            package, version, cache, settings.license_cache_ttl,
            settings.pypi_api_url, http_client,
        )
    else:
        # Known to registry — still need latest_version for the CVE call when
        # no version is pinned, so we hit PyPI but ignore the license.
        if version is None:
            license_info, latest_version = await resolve_license(
                package, None, cache, settings.license_cache_ttl,
                settings.pypi_api_url, http_client,
            )

    cve_query_version = version or latest_version

    # Step 3 — CVE check (applies regardless of registry status).
    cves = await resolve_cves(
        package,
        cve_query_version,
        cache,
        settings.cve_cache_ttl,
        settings.osv_api_url,
        http_client,
    )

    if registry is not None and registry.global_policies.cve_blocks(cves.max_score):
        result = _denied(
            package=package,
            version=version,
            resolved_version=cve_query_version,
            reason=(
                f"Critical CVE detected (max CVSS {cves.max_score}); "
                f"threshold is {registry.global_policies.cve_threshold * 10:.1f}."
            ),
            registry_status=(
                pkg_verdict.status.value if pkg_verdict and pkg_verdict.status else None
            ),
            license=license_info.license_normalized if license_info else None,
            cves=cves,
        )
        await _audit(audit, result, project, requester, agent, DENIAL_CVE)
        return result

    # Step 4 — license verdict for unknown-to-registry packages.
    if registry is not None and pkg_verdict is not None and pkg_verdict.action is Action.UNKNOWN:
        assert license_info is not None  # set in step 2
        lic_verdict = evaluate_license(
            license_info.license_normalized, registry.global_policies
        )
        if lic_verdict.action is Action.BLOCK:
            result = _denied(
                package=package,
                version=version,
                resolved_version=cve_query_version,
                reason=lic_verdict.reason,
                registry_status=None,
                license=license_info.license_normalized,
                cves=cves,
            )
            await _audit(audit, result, project, requester, agent, DENIAL_LICENSE)
            return result
        # Unknown but license-passing → provisionally allowed.
        if lic_verdict.needs_human_review:
            prov_reason = (
                "Not yet in the enterprise registry. License could not be "
                "determined; provisionally allowed pending human review. "
                "An approval request will be automatically submitted."
            )
        else:
            prov_reason = (
                "Not yet in the enterprise registry. License "
                f"{license_info.license_normalized} is on the auto-approve list "
                "and no critical CVEs are reported. Run `lex-align-client "
                "request-approval` to formalize."
            )
        result = EvaluationResult(
            verdict=VERDICT_PROVISIONALLY_ALLOWED,
            reason=prov_reason,
            package=package,
            version=version,
            resolved_version=cve_query_version,
            registry_status=None,
            license=license_info.license_normalized,
            cve_ids=list(cves.ids),
            max_cvss=cves.max_score,
            is_requestable=True,
            auto_request_approval=lic_verdict.needs_human_review,
        )
        await _audit(audit, result, project, requester, agent, DENIAL_NONE)
        return result

    # Step 5 — known-to-registry ALLOW or REQUIRE_PROPOSE.
    if pkg_verdict is None:
        # No registry configured at all. Treat as provisionally allowed
        # (license/CVE already passed if applicable).
        result = EvaluationResult(
            verdict=VERDICT_PROVISIONALLY_ALLOWED,
            reason="No enterprise registry configured; permitted by default.",
            package=package,
            version=version,
            resolved_version=cve_query_version,
            registry_status=None,
            license=license_info.license_normalized if license_info else None,
            cve_ids=list(cves.ids),
            max_cvss=cves.max_score,
            is_requestable=False,
        )
        await _audit(audit, result, project, requester, agent, DENIAL_NONE)
        return result

    needs_rationale = pkg_verdict.action is Action.REQUIRE_PROPOSE
    result = EvaluationResult(
        verdict=VERDICT_ALLOWED,
        reason=pkg_verdict.reason or (
            f"Allowed by enterprise registry ({pkg_verdict.status.value if pkg_verdict.status else 'allow'})."
        ),
        package=package,
        version=version,
        resolved_version=cve_query_version,
        registry_status=pkg_verdict.status.value if pkg_verdict.status else None,
        version_constraint=pkg_verdict.version_constraint,
        license=license_info.license_normalized if license_info else None,
        cve_ids=list(cves.ids),
        max_cvss=cves.max_score,
        is_requestable=False,
        needs_rationale=needs_rationale,
    )
    await _audit(audit, result, project, requester, agent, DENIAL_NONE)
    return result


async def _audit(
    audit: AuditStore,
    result: EvaluationResult,
    project: str,
    requester: str,
    agent: AgentInfo,
    denial_category: str,
) -> None:
    await audit.record_evaluation(
        AuditRecord(
            project=project,
            requester=requester,
            package=result.package,
            version=result.version,
            resolved_version=result.resolved_version,
            verdict=result.verdict,
            denial_category=denial_category,
            reason=result.reason,
            license=result.license,
            cve_ids=result.cve_ids,
            max_cvss=result.max_cvss,
            registry_status=result.registry_status,
            agent_model=agent.model,
            agent_version=agent.version,
        )
    )
