"""Registry endpoints used by the dashboard workshop.

- GET    /registry             returns the live registry as JSON.
- GET    /registry/pending     returns PENDING_REVIEW approval requests for
                               packages not in the loaded registry.
- POST   /registry/packages    classifies a package and adds it to the
                               in-memory registry. Pending approval requests
                               for that package flip to APPROVED. The change
                               is **not persisted** to the registry file —
                               the operator must export the updated YAML
                               and redeploy the server to make it stick.
- POST   /registry/parse-yaml  validates user-supplied YAML against the
                               schema the CLI compiler uses and returns it
                               as JSON.

The classify endpoint is the only one that mutates server state.
"""

from __future__ import annotations

import logging
from typing import Optional

import yaml
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ...registry import PackageRule, PackageStatus, normalize_name
from ...registry_schema import ValidationError, validate_package_rule, validate_registry


router = APIRouter()
logger = logging.getLogger(__name__)


class YamlBody(BaseModel):
    yaml_text: str = Field(..., min_length=1)


class PackageClassifyBody(BaseModel):
    name: str = Field(..., min_length=1)
    status: str = Field(..., min_length=1)
    reason: Optional[str] = None
    replacement: Optional[str] = None
    min_version: Optional[str] = None
    max_version: Optional[str] = None


def _registry_to_dict(reg) -> dict:
    """Serialize the in-memory Registry object to the YAML-shaped dict
    consumed by the dashboard. We deliberately mirror the YAML schema, not
    the compiled-JSON shape, so round-tripping export → import works."""
    gp = reg.global_policies
    return {
        "version": reg.version,
        "global_policies": {
            "auto_approve_licenses": list(gp.auto_approve_licenses),
            "hard_ban_licenses": list(gp.hard_ban_licenses),
            "require_human_review_licenses": list(gp.require_human_review_licenses),
            "unknown_license_policy": gp.unknown_license_policy,
            "cve_threshold": gp.cve_threshold,
        },
        "packages": {
            name: {
                "status": rule.status.value,
                **({"reason": rule.reason} if rule.reason else {}),
                **({"replacement": rule.replacement} if rule.replacement else {}),
                **({"min_version": rule.min_version} if rule.min_version else {}),
                **({"max_version": rule.max_version} if rule.max_version else {}),
            }
            for name, rule in sorted(reg.packages.items())
        },
    }


@router.get("/registry")
async def get_registry(request: Request) -> dict:
    reg = request.app.state.lex.registry
    if reg is None:
        return {
            "version": "1",
            "global_policies": {
                "auto_approve_licenses": [],
                "hard_ban_licenses": [],
                "require_human_review_licenses": [],
                "unknown_license_policy": "block",
                "cve_threshold": 0.9,
            },
            "packages": {},
        }
    return _registry_to_dict(reg)


@router.get("/registry/pending")
async def pending_requests(request: Request) -> dict:
    """Pending approval requests for packages not yet in the live registry.

    Once a package is classified via POST /registry/packages, its pending
    approval requests are flipped to APPROVED in the audit store and stop
    appearing here. Without a loaded registry, every PENDING_REVIEW row is
    surfaced.
    """
    state = request.app.state.lex
    grouped = await state.audit.list_pending_by_package()
    registered: set[str] = set()
    if state.registry is not None:
        registered = set(state.registry.packages.keys())
    grouped = [g for g in grouped if g["normalized_name"] not in registered]
    return {"items": grouped}


@router.post("/registry/packages", status_code=200)
async def classify_package(body: PackageClassifyBody, request: Request) -> dict:
    """Classify a package and upsert it into the in-memory registry.

    This is what the dashboard calls when an operator approves a pending
    request: the package + its assigned status is written into the live
    `Registry` so subsequent `/evaluate` calls see it immediately, and
    every PENDING_REVIEW approval request for the same normalized name
    flips to APPROVED.

    The change lives only in memory. To persist it across restarts, the
    operator must export the YAML from the dashboard and rebuild the
    server image (or re-run `lex-align-server registry compile` against
    the updated YAML and restart the process).
    """
    state = request.app.state.lex
    if state.registry is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "No registry is loaded; classification requires REGISTRY_PATH "
                "to point at a compiled registry."
            ),
        )

    rule_dict: dict = {"status": body.status}
    if body.reason:      rule_dict["reason"] = body.reason
    if body.replacement: rule_dict["replacement"] = body.replacement
    if body.min_version: rule_dict["min_version"] = body.min_version
    if body.max_version: rule_dict["max_version"] = body.max_version

    try:
        validate_package_rule(body.name, rule_dict)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    normalized = normalize_name(body.name)
    state.registry.packages[normalized] = PackageRule(
        status=PackageStatus(body.status),
        reason=body.reason,
        replacement=body.replacement,
        min_version=body.min_version,
        max_version=body.max_version,
    )
    approved = await state.audit.mark_approved_by_package(normalized)

    logger.info(
        "registry mutated in-memory: package=%s status=%s approved=%d",
        normalized, body.status, approved,
    )
    return {
        "package": body.name,
        "normalized_name": normalized,
        "status": body.status,
        "approved_requests": approved,
        "persisted": False,
        "note": (
            "Change is in-memory only; export the YAML and rebuild the "
            "server image to persist it."
        ),
    }


@router.post("/registry/parse-yaml")
async def parse_yaml(body: YamlBody) -> dict:
    try:
        doc = yaml.safe_load(body.yaml_text)
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"YAML parse error: {exc}",
        )
    try:
        compiled = validate_registry(doc or {})
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    # The compiled form is fine for re-hydrating the dashboard; the schema
    # matches what GET /registry produces minus dropped-empty fields.
    return compiled
