"""Pluggable authentication for org mode.

Single-user mode (``AUTH_ENABLED=false``) bypasses this entirely and uses
``AnonymousAuthenticator``. Org mode (``AUTH_ENABLED=true``) selects one of
the built-in backends via ``AUTH_BACKEND``:

* ``header`` (recommended) — trust ``X-Forwarded-User`` / ``-Email`` /
  ``-Groups`` injected by an upstream auth gateway (oauth2-proxy,
  Pomerium, Cloudflare Access, an ingress' OIDC filter, etc.). Zero
  Python customization; the org configures their existing gateway.
* ``webhook`` — forward the request's bearer token to a URL the org
  controls (``AUTH_VERIFY_URL``); the verifier returns user JSON. The
  org writes one tiny endpoint in any language.
* ``apikey`` — stub for a future built-in API-key store. Currently
  raises ``NotImplementedError`` so an ill-configured deployment fails
  loud rather than silently allowing anonymous access.
* ``module:path.to:ClassName`` — escape hatch. Drop a Python file
  implementing :class:`Authenticator` into the container and point the
  env var at it.

The dependency surface (``get_identity``/``get_requester`` in
``auth.py``) is stable; orgs only need to swap the backend.
"""

from __future__ import annotations

from .base import AuthError, Authenticator, Identity
from .loader import load_authenticator

__all__ = ["AuthError", "Authenticator", "Identity", "load_authenticator"]
