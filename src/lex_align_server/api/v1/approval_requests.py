"""POST /api/v1/approval-requests — the Good Citizen.

Persists the request, then *non-blockingly* fires the configured
proposer so a PR (or local-file write, etc.) gets opened. The 202
returns immediately — the agent isn't penalized if the proposer
hits a transient error; the audit row is still durable, the dashboard
will surface it, and the operator can re-trigger from the UI.

The proposer call defaults the YAML status to ``approved`` — a safe,
explicit "this package is in the registry but isn't blessed as
preferred." The PR description tells reviewers they can flip it to
``preferred`` (or ``version-constrained`` / ``deprecated``) before
merge if a stronger classification is appropriate.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...audit import APPROVAL_PENDING, ApprovalRequest
from ...auth import AgentInfo, get_agent_info, get_project, get_requester
from ...proposer import ProposalContext, ProposedRule, ProposerError


router = APIRouter()
logger = logging.getLogger(__name__)

# Default status the agent path proposes. Reviewers tweak it during PR
# review when something stronger (preferred / version-constrained) fits.
AGENT_PROPOSAL_DEFAULT_STATUS = "approved"


class ApprovalRequestBody(BaseModel):
    package: str = Field(..., min_length=1)
    rationale: str = Field(..., min_length=1)


@router.post("/approval-requests", status_code=status.HTTP_202_ACCEPTED)
async def create_approval_request(
    request: Request,
    body: ApprovalRequestBody,
    project: str = Depends(get_project),
    requester: str = Depends(get_requester),
    agent: AgentInfo = Depends(get_agent_info),
) -> JSONResponse:
    state = request.app.state.lex
    req = ApprovalRequest(
        project=project,
        requester=requester,
        package=body.package,
        rationale=body.rationale,
        status=APPROVAL_PENDING,
        agent_model=agent.model,
        agent_version=agent.version,
    )
    request_id = await state.audit.upsert_approval_request(req)
    logger.info(
        "approval request stored: package=%s project=%s requester=%s "
        "agent=%s/%s request_id=%s",
        body.package, project, requester,
        agent.model or "?", agent.version or "?", request_id,
    )

    rule = ProposedRule(
        name=body.package,
        status=AGENT_PROPOSAL_DEFAULT_STATUS,
    )
    context = ProposalContext(
        source="agent",
        project=project,
        requester=requester,
        rationale=body.rationale,
        agent_model=agent.model,
        agent_version=agent.version,
    )
    # Fire the proposer in the background so a slow git push / API call
    # doesn't extend the agent's wall-clock. Failures are logged; the
    # audit row above remains durable so the dashboard can re-trigger.
    asyncio.create_task(_propose_in_background(state, rule, context, request_id))

    return JSONResponse(
        {
            "request_id": request_id,
            "status": APPROVAL_PENDING,
            "package": body.package,
            "project": project,
            "agent_model": agent.model,
            "agent_version": agent.version,
            "proposal": {
                "backend": type(state.proposer).__name__,
                "status": "queued",
            },
        },
        status_code=status.HTTP_202_ACCEPTED,
    )


async def _propose_in_background(
    state, rule: ProposedRule, context: ProposalContext, request_id: str
) -> None:
    try:
        result = await state.proposer.propose(rule, context)
        logger.info(
            "proposer dispatched for request_id=%s package=%s: %s %s",
            request_id, rule.name, result.status, result.url or "",
        )
    except ProposerError as exc:
        logger.warning(
            "proposer failed for request_id=%s package=%s: %s",
            request_id, rule.name, exc,
        )
    except Exception:
        logger.exception(
            "proposer crashed for request_id=%s package=%s",
            request_id, rule.name,
        )
