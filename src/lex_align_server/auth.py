"""Auth dependencies.

Identity resolution lives in the pluggable authenticator on
``app.state.lex.authenticator`` (see ``authn/``). The functions here are
thin FastAPI ``Depends`` shims so endpoints can stay declarative:

    requester: str = Depends(get_requester)
    identity: Identity = Depends(get_identity)
    agent: AgentInfo = Depends(get_agent_info)

Single-user mode ships an ``AnonymousAuthenticator`` so the same code path
works without any auth setup; org mode swaps in whatever the operator
configured via ``AUTH_BACKEND``.

The agent-identity headers (``X-LexAlign-Agent-Model`` /
``-Version``) are tag metadata, not authentication — they carry the
*model* that originated the request (e.g. opus 4.7) and feed the
agent-activity dashboards. The ``Authenticator`` answers "who is calling"
(the human/principal); ``get_agent_info`` answers "which agent did the
calling for them."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status

from .authn import Identity


PROJECT_HEADER = "X-LexAlign-Project"
AGENT_MODEL_HEADER = "X-LexAlign-Agent-Model"
AGENT_VERSION_HEADER = "X-LexAlign-Agent-Version"


@dataclass(frozen=True)
class AgentInfo:
    """Agent identity reported by the client.

    Both fields are optional; when the client doesn't send the headers, both
    are None and reports group those rows under an "(unknown agent)" bucket.
    """
    model: Optional[str] = None
    version: Optional[str] = None


async def get_project(
    x_lexalign_project: str | None = Header(None, alias=PROJECT_HEADER),
) -> str:
    if not x_lexalign_project:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{PROJECT_HEADER} header is required.",
        )
    return x_lexalign_project.strip()


async def get_agent_info(
    x_lexalign_agent_model: str | None = Header(None, alias=AGENT_MODEL_HEADER),
    x_lexalign_agent_version: str | None = Header(None, alias=AGENT_VERSION_HEADER),
) -> AgentInfo:
    return AgentInfo(
        model=(x_lexalign_agent_model or "").strip() or None,
        version=(x_lexalign_agent_version or "").strip() or None,
    )


async def get_identity(request: Request) -> Identity:
    """Resolve the requester via the per-app authenticator.

    Endpoints depend on this when they need email/groups (e.g. for
    future per-team authorization). For the common "I just need a
    string for the audit log" case, depend on :func:`get_requester`
    instead.
    """
    return await request.app.state.lex.authenticator.authenticate(request)


async def get_requester(identity: Identity = Depends(get_identity)) -> str:
    """The principal id, suitable for ``audit_log.requester``."""
    return identity.id
