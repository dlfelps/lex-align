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
from ...auth import get_project


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
) -> JSONResponse:
    state = request.app.state.lex
    requester = "anonymous"
    req = ApprovalRequest(
        project=project,
        requester=requester,
        package=body.package,
        rationale=body.rationale,
        status=APPROVAL_PENDING,
    )
    request_id = await state.audit.upsert_approval_request(req)
    # PR creation is a Phase-4 deliverable. For now, leave a clear breadcrumb
    # in the server log so operators can see what would be opened.
    logger.info(
        "TODO: open PR to add %s for project=%s requester=%s (request_id=%s)",
        body.package, project, requester, request_id,
    )
    return JSONResponse(
        {
            "request_id": request_id,
            "status": APPROVAL_PENDING,
            "package": body.package,
            "project": project,
        },
        status_code=status.HTTP_202_ACCEPTED,
    )
