"""POST /api/v1/approval-requests — the Good Citizen.

Persists a request and returns 202 immediately. Phase 4 will swap in real
PR creation against the registry's git repo.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...audit import APPROVAL_PENDING, ApprovalRequest
from ...auth import AgentInfo, get_agent_info, get_project, get_requester


router = APIRouter()
logger = logging.getLogger(__name__)


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
    return JSONResponse(
        {
            "request_id": request_id,
            "status": APPROVAL_PENDING,
            "package": body.package,
            "project": project,
            "agent_model": agent.model,
            "agent_version": agent.version,
        },
        status_code=status.HTTP_202_ACCEPTED,
    )
