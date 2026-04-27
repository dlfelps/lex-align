"""GET /api/v1/evaluate — the Advisor."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ...auth import AgentInfo, get_agent_info, get_project
from ...evaluate import evaluate


router = APIRouter()


@router.get("/evaluate")
async def evaluate_endpoint(
    request: Request,
    package: str = Query(..., min_length=1),
    version: str | None = Query(None),
    project: str = Depends(get_project),
    agent: AgentInfo = Depends(get_agent_info),
) -> dict:
    state = request.app.state.lex
    requester = "anonymous"  # auth wiring lands in org-mode
    result = await evaluate(
        package=package,
        version=version,
        project=project,
        requester=requester,
        agent=agent,
        registry=state.registry,
        cache=state.cache,
        audit=state.audit,
        settings=state.settings,
        http_client=state.http,
    )
    return result.to_dict()
