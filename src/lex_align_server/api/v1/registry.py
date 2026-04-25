"""Registry endpoints used by the dashboard workshop.

- GET  /registry            returns the live registry as JSON.
- GET  /registry/pending    returns PENDING_REVIEW approval requests for
                            packages not in the loaded registry.
- POST /registry/parse-yaml validates user-supplied YAML against the
  same schema the CLI compiler uses and returns it as JSON so the
  browser-side workshop can resume editing an existing file.

These endpoints don't mutate server state; the workshop's edits live
only in the browser and are exported as YAML on the client.
"""

from __future__ import annotations

import yaml
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ...registry_schema import ValidationError, validate_registry


router = APIRouter()


class YamlBody(BaseModel):
    yaml_text: str = Field(..., min_length=1)


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

    Used by the dashboard to surface a triage queue. Items already present
    in the loaded registry are filtered out so resolved requests don't
    re-appear after the next compile/redeploy.
    """
    state = request.app.state.lex
    grouped = await state.audit.list_pending_by_package()
    if state.registry is not None:
        registered = set(state.registry.packages.keys())
        grouped = [g for g in grouped if g["normalized_name"] not in registered]
    return {"items": grouped}


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
