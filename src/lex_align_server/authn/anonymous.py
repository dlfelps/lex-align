"""Anonymous authenticator — used when ``AUTH_ENABLED=false``.

Always returns the same identity. Single-user mode runs this so that the
audit log still has a valid ``requester`` value without any auth setup.
"""

from __future__ import annotations

from fastapi import Request

from .base import Authenticator, Identity


ANONYMOUS_ID = "anonymous"


class AnonymousAuthenticator(Authenticator):
    async def authenticate(self, request: Request) -> Identity:
        return Identity(id=ANONYMOUS_ID)
