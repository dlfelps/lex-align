"""Auth dependencies.

When `AUTH_ENABLED=false` (default) every request is treated as anonymous.
When `AUTH_ENABLED=true` the bearer token is required but the validation/key
storage is intentionally a Phase-3 stub. Wire-up exists so callers don't need
to change when org-mode lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException, status

from .config import Settings


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


async def get_requester(
    settings_dep: Settings,
    authorization: str | None = Header(None),
) -> str:
    if not settings_dep.auth_enabled:
        return "anonymous"
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required when AUTH_ENABLED=true.",
        )
    # Phase 3+: validate against api_keys table and return key id.
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Org-mode auth is not yet implemented.",
    )
