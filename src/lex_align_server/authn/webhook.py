"""Forward the bearer token to an org-controlled verifier.

The org writes one tiny endpoint that accepts ``POST <verify_url>`` with
``{"token": "..."}`` and returns ``{"id": "...", "email"?: "...",
"groups"?: ["..."]}`` on success or a non-2xx on failure. lex-align
treats the returned JSON as the :class:`Identity` payload.

This is a good fit for orgs that don't have an HTTP-level auth gateway
but do have an internal user-info or session-validation service. The
verifier can be a Lambda, a sidecar, an internal microservice — any
HTTP endpoint that knows how to validate the org's tokens.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import Request

from .base import AuthError, Authenticator, Identity


logger = logging.getLogger(__name__)


class WebhookAuthenticator(Authenticator):
    def __init__(
        self,
        *,
        verify_url: str,
        http_client: httpx.AsyncClient,
        timeout: float,
    ):
        if not verify_url:
            raise ValueError(
                "WebhookAuthenticator requires AUTH_VERIFY_URL to be set."
            )
        self.verify_url = verify_url
        self.http = http_client
        self.timeout = timeout

    async def authenticate(self, request: Request) -> Identity:
        token = _extract_bearer(request)
        if not token:
            raise AuthError("Bearer token required.")

        try:
            resp = await self.http.post(
                self.verify_url,
                json={"token": token},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("auth verifier unreachable: %s", exc)
            raise AuthError("Authentication service unreachable.") from exc

        if resp.status_code == 401 or resp.status_code == 403:
            raise AuthError("Token rejected by verifier.")
        if resp.status_code >= 400:
            logger.warning(
                "auth verifier returned %s: %s",
                resp.status_code, resp.text[:200],
            )
            raise AuthError("Authentication service error.")

        try:
            body = resp.json()
        except ValueError as exc:
            raise AuthError("Verifier returned non-JSON response.") from exc

        principal_id = (body.get("id") or "").strip() if isinstance(body, dict) else ""
        if not principal_id:
            raise AuthError("Verifier response missing required `id` field.")

        groups_raw = body.get("groups") or ()
        groups = tuple(
            str(g) for g in groups_raw if isinstance(g, str) and g
        ) if isinstance(groups_raw, (list, tuple)) else ()

        return Identity(
            id=principal_id,
            email=body.get("email") or None,
            groups=groups,
            raw={"source": "webhook", "response": body},
        )


def _extract_bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    return header[7:].strip() or None
