"""GET /api/v1/evaluate — the Advisor."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Query, Request

from ...audit import APPROVAL_PENDING, ApprovalRequest
from ...auth import AgentInfo, get_agent_info, get_project, get_requester
from ...evaluate import evaluate
from ...proposer import ProposalContext, ProposedRule, ProposerError


router = APIRouter()
logger = logging.getLogger(__name__)

_AUTO_RATIONALE = (
    "Auto-submitted: license unknown, pending human review of the registry entry."
)


@router.get("/evaluate")
async def evaluate_endpoint(
    request: Request,
    package: str = Query(..., min_length=1),
    version: str | None = Query(None),
    project: str = Depends(get_project),
    requester: str = Depends(get_requester),
    agent: AgentInfo = Depends(get_agent_info),
) -> dict:
    state = request.app.state.lex
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

    if result.auto_request_approval:
        req = ApprovalRequest(
            project=project,
            requester=requester,
            package=package,
            rationale=_AUTO_RATIONALE,
            status=APPROVAL_PENDING,
            agent_model=agent.model,
            agent_version=agent.version,
        )
        request_id = await state.audit.upsert_approval_request(req)
        rule = ProposedRule(name=package, status="approved")
        context = ProposalContext(
            source="agent",
            project=project,
            requester=requester,
            rationale=_AUTO_RATIONALE,
            agent_model=agent.model,
            agent_version=agent.version,
        )
        asyncio.create_task(_fire_proposer(state, rule, context, request_id))

    return result.to_dict()


async def _fire_proposer(
    state, rule: ProposedRule, context: ProposalContext, request_id: str
) -> None:
    try:
        proposal = await state.proposer.propose(rule, context)
        logger.info(
            "auto-approval proposed for request_id=%s package=%s: %s %s",
            request_id, rule.name, proposal.status, proposal.url or "",
        )
    except ProposerError as exc:
        logger.warning(
            "auto-approval proposer failed for request_id=%s package=%s: %s",
            request_id, rule.name, exc,
        )
    except Exception:
        logger.exception(
            "auto-approval proposer crashed for request_id=%s package=%s",
            request_id, rule.name,
        )
