"""Registry endpoints used by the dashboard workshop and the agent
``request-approval`` flow.

- GET    /registry              live registry as JSON.
- GET    /registry/pending      explicit pending approval requests
                                **plus** implicit candidates (packages
                                seen in audit_log without an
                                approval-request follow-up). Each row
                                carries a ``reason`` describing why
                                it's surfacing.
- POST   /registry/proposals    operator-initiated proposal — opens a
                                PR / commits / writes YAML depending on
                                the configured proposer backend.
                                Replaces the pre-Phase-4 in-memory
                                classify endpoint.
- POST   /registry/parse-yaml   YAML round-trip validator, used by the
                                dashboard's import button.
- POST   /registry/reload       operator-only manual reload (fallback
                                for when the merge webhook is missed).
- POST   /registry/webhook      git-host webhook callback (verified by
                                ``REGISTRY_WEBHOOK_SECRET``); triggers a
                                pull-and-reload.

The classify endpoint (``POST /registry/packages``) was removed in
Phase 4 — every registry update now flows through a proposer and a
reload, so there's exactly one source of truth (``registry.yml``).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Optional

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from ...auth import AgentInfo, get_agent_info, get_project, get_requester
from ...proposer import ProposalContext, ProposedRule, ProposerError
from ...registry_schema import ValidationError, validate_package_rule, validate_registry
from ...reloader import reload_registry


router = APIRouter()
logger = logging.getLogger(__name__)


class YamlBody(BaseModel):
    yaml_text: str = Field(..., min_length=1)


class ProposalBody(BaseModel):
    """Same shape as the legacy classify body. The endpoint emits a
    proposer call instead of mutating the in-memory registry, so the
    fields are advisory until the proposal lands (PR merged / file
    written / commit landed)."""
    name: str = Field(..., min_length=1)
    status: str = Field(..., min_length=1)
    reason: Optional[str] = None
    replacement: Optional[str] = None
    min_version: Optional[str] = None
    max_version: Optional[str] = None
    rationale: Optional[str] = None  # operator-supplied note for the PR body


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
    """Triage queue for the dashboard.

    Combines two streams:
      * ``explicit`` — approval requests an agent or operator filed via
        ``POST /approval-requests``.
      * ``implicit`` — packages seen in ``audit_log`` over the last 30
        days that *never* generated an approval request (because the
        agent only called ``check``, or the call returned DENIED, or
        only the pre-commit hook ran). Each row carries a ``reason``
        explaining why it surfaced.

    Both streams filter against the live registry — once a package is
    classified (PR merged → reload → mark_approved_by_package fires), it
    drops out of both lists automatically.
    """
    state = request.app.state.lex
    explicit = await state.audit.list_pending_by_package()
    implicit = await state.audit.list_implicit_candidates(window_days=30)

    registered: set[str] = set()
    if state.registry is not None:
        registered = set(state.registry.packages.keys())
    explicit_keys = {row["normalized_name"] for row in explicit}

    explicit = [r for r in explicit if r["normalized_name"] not in registered]
    implicit = [
        r for r in implicit
        if r["normalized_name"] not in registered
        and r["normalized_name"] not in explicit_keys
    ]
    return {"explicit": explicit, "implicit": implicit}


@router.post("/registry/proposals", status_code=200)
async def open_proposal(
    body: ProposalBody,
    request: Request,
    project: str = Depends(get_project),
    requester: str = Depends(get_requester),
    agent: AgentInfo = Depends(get_agent_info),
) -> dict:
    """Operator-initiated proposal — fires the configured proposer.

    Replaces the legacy ``POST /registry/packages`` in-memory classify
    endpoint. The dashboard's "Approve" button calls this; the result
    payload tells the UI whether a PR was opened (``opened`` /
    ``amended``), the change was applied directly (``applied``,
    local-file / local-git modes), or it was logged only.
    """
    state = request.app.state.lex

    rule_dict: dict = {"status": body.status}
    if body.reason:      rule_dict["reason"] = body.reason
    if body.replacement: rule_dict["replacement"] = body.replacement
    if body.min_version: rule_dict["min_version"] = body.min_version
    if body.max_version: rule_dict["max_version"] = body.max_version
    try:
        validate_package_rule(body.name, rule_dict)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        )

    rule = ProposedRule(
        name=body.name,
        status=body.status,
        reason=body.reason,
        replacement=body.replacement,
        min_version=body.min_version,
        max_version=body.max_version,
    )
    context = ProposalContext(
        source="operator",
        project=project,
        requester=requester,
        rationale=body.rationale or "",
        agent_model=agent.model,
        agent_version=agent.version,
    )
    try:
        result = await state.proposer.propose(rule, context)
    except ProposerError as exc:
        logger.warning("proposer rejected proposal for %s: %s", body.name, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc),
        )
    return result.to_dict()


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
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        )
    return compiled


@router.post("/registry/reload", status_code=200)
async def reload_endpoint(request: Request) -> dict:
    """Manual fallback for the case where the merge webhook is lost.

    Re-reads ``REGISTRY_PATH`` from disk, validates, and atomically
    swaps the in-memory registry. Operator-only by convention — when
    org-mode auth is enabled the auth backend gates this with
    everything else.
    """
    result = await reload_registry(request.app.state.lex)
    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.detail,
        )
    return result.to_dict()


@router.post("/registry/webhook", status_code=200)
async def webhook_endpoint(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None),
    x_github_event: Optional[str] = Header(None),
) -> dict:
    """GitHub-style webhook callback.

    Verifies the HMAC-SHA256 signature against
    ``REGISTRY_WEBHOOK_SECRET``. On a merged ``pull_request`` event,
    pulls the merged YAML and triggers a reload. Pings (``ping`` event)
    return 200 so the operator can use the host's "test webhook" UI.
    """
    state = request.app.state.lex
    secret = state.settings.registry_webhook_secret
    raw = await request.body()

    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "REGISTRY_WEBHOOK_SECRET is not configured; webhook is "
                "disabled. Configure the secret to enable hot-reload."
            ),
        )
    if not _verify_signature(secret, raw, x_hub_signature_256):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature.",
        )

    if x_github_event == "ping":
        return {"ok": True, "event": "ping"}

    if x_github_event != "pull_request":
        return {"ok": True, "event": x_github_event, "ignored": True}

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Webhook body is not JSON: {exc}",
        )

    action = payload.get("action")
    pr = payload.get("pull_request") or {}
    if action != "closed" or not pr.get("merged"):
        return {"ok": True, "event": "pull_request", "action": action, "ignored": True}

    # The proposer (if it's a Git-backed one) knows how to fetch the
    # latest YAML to disk; the reloader then re-reads from there.
    try:
        await _refresh_local_yaml(state)
    except Exception:
        logger.exception("webhook: failed to refresh local registry YAML")

    result = await reload_registry(state)
    return {"ok": result.ok, "event": "pull_request.merged", **result.to_dict()}


async def _refresh_local_yaml(state) -> None:
    """Poke the proposer to refresh its local working tree if it has one.

    For ``GitHubProposer`` this means a fast-forward fetch of the default
    branch and copying the merged YAML into ``REGISTRY_PATH``.
    Other backends (local-file / local-git / log-only) are no-ops.
    """
    proposer = state.proposer
    refresh = getattr(proposer, "refresh_local_yaml", None)
    if refresh is None:
        return
    await refresh(state.settings.registry_path)


def _verify_signature(
    secret: str, body: bytes, signature_header: Optional[str]
) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = signature_header.split("=", 1)[1]
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, expected)
