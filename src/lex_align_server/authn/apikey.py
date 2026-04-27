"""Built-in API-key store — currently a stub.

Reserved for a future backend that validates bearer tokens against an
``api_keys`` SQLite table managed by ``lex-align-server admin keys``.
Useful for evaluation or for orgs that don't want to wire up SSO.

Until that's implemented, selecting ``AUTH_BACKEND=apikey`` raises at
request time so a misconfigured deployment fails loud rather than
silently dropping every request through.
"""

from __future__ import annotations

from fastapi import Request, status, HTTPException

from .base import Authenticator, Identity


class ApiKeyAuthenticator(Authenticator):
    async def authenticate(self, request: Request) -> Identity:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "AUTH_BACKEND=apikey is reserved for a future built-in API-key "
                "store and is not yet implemented. For org-mode auth today, "
                "use AUTH_BACKEND=header (recommended, with a reverse-proxy "
                "auth gateway), AUTH_BACKEND=webhook (with your own verifier "
                "service), or AUTH_BACKEND=module:path:Class (custom)."
            ),
        )
