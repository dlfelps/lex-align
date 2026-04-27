"""Authenticator contract.

Custom backends only need to subclass :class:`Authenticator` and implement
``authenticate``. Everything else (settings parsing, dependency wiring,
audit logging) is handled by the framework.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

from fastapi import HTTPException, Request, status


@dataclass(frozen=True)
class Identity:
    """The authenticated principal for a single request.

    ``id`` is what lex-align records in ``audit_log.requester`` and uses to
    de-dupe approval requests. The other fields are advisory and surface
    in the dashboards: ``email`` for "who asked for this package", and
    ``groups`` for future per-team authorization (e.g. limit registry
    edits to ``security-engineers``).
    ``raw`` is a free-form dict — backend-specific extras (claims from a
    JWT, the full user record from a webhook response, etc.) — that
    custom authenticators can stash for downstream code without inventing
    new fields.
    """
    id: str
    email: str | None = None
    groups: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)


class AuthError(HTTPException):
    """Authentication failed. Maps to HTTP 401 with a ``WWW-Authenticate``
    header so curl/SDK clients can react sensibly."""

    def __init__(self, detail: str = "Authentication required."):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


class Authenticator(ABC):
    """Resolve a :class:`Request` to an :class:`Identity` or raise.

    Implementations must:
      * return an :class:`Identity` on success;
      * raise :class:`AuthError` (or any ``HTTPException``) on failure;
      * be ``async`` — the framework awaits them on every request.

    They should *not* perform authorization (group/role checks): that's
    a downstream concern. The Authenticator's only job is to answer
    "who is making this request".
    """

    @abstractmethod
    async def authenticate(self, request: Request) -> Identity:
        ...
