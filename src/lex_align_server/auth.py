"""Auth dependencies.

The actual identity resolution lives in the pluggable authenticator on
``app.state.lex.authenticator`` (see ``authn/``). These functions are
thin FastAPI ``Depends`` shims so endpoints can stay declarative:

    requester: str = Depends(get_requester)
    identity: Identity = Depends(get_identity)

Single-user mode ships an ``AnonymousAuthenticator`` so the same code
path works without any auth setup; org mode swaps in whatever the
operator configured via ``AUTH_BACKEND``.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status

from .authn import Identity


PROJECT_HEADER = "X-LexAlign-Project"


async def get_project(
    x_lexalign_project: str | None = Header(None, alias=PROJECT_HEADER),
) -> str:
    if not x_lexalign_project:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{PROJECT_HEADER} header is required.",
        )
    return x_lexalign_project.strip()


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
