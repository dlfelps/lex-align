"""Read-only report endpoints.

Phase 4 dashboards consume these; Phase 3 also exposes them directly so
operators can query them with curl.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request


router = APIRouter()


@router.get("/reports/legal")
async def legal_report(
    request: Request, project: Optional[str] = Query(None)
) -> dict:
    return await request.app.state.lex.audit.legal_report(project)


@router.get("/reports/security")
async def security_report(
    request: Request, project: Optional[str] = Query(None)
) -> dict:
    return await request.app.state.lex.audit.security_report(project)


@router.get("/reports/approval-requests")
async def approval_request_report(
    request: Request,
    project: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
) -> dict:
    rows = await request.app.state.lex.audit.list_approval_requests(project, status_filter)
    return {"project": project, "status": status_filter, "items": rows}


@router.get("/reports/projects")
async def projects_report(request: Request) -> dict:
    rows = await request.app.state.lex.audit.projects_summary()
    return {"projects": rows}
