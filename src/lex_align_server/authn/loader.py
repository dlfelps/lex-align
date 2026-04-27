"""Resolve the configured ``AUTH_BACKEND`` to an :class:`Authenticator`.

Called once from the FastAPI lifespan. The result is stored on
``app.state.lex.authenticator`` and reused for every request via the
``get_identity`` dependency.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import httpx

from .anonymous import AnonymousAuthenticator
from .apikey import ApiKeyAuthenticator
from .base import Authenticator
from .header import HeaderAuthenticator
from .webhook import WebhookAuthenticator


if TYPE_CHECKING:
    from ..config import Settings


# Built-in backend names. ``module:Class`` strings bypass this map and go
# through the dynamic-import path below.
BUILTIN_BACKENDS = {"anonymous", "header", "webhook", "apikey"}


def load_authenticator(
    settings: "Settings", http_client: httpx.AsyncClient
) -> Authenticator:
    """Return the authenticator selected by ``settings``.

    Single-user mode short-circuits to anonymous regardless of
    ``AUTH_BACKEND`` — so flipping ``AUTH_ENABLED=false`` is always a
    safe escape hatch during incident response.
    """
    if not settings.auth_enabled:
        return AnonymousAuthenticator()

    backend = (settings.auth_backend or "header").strip()

    if backend == "anonymous":
        return AnonymousAuthenticator()
    if backend == "header":
        return HeaderAuthenticator(
            user_header=settings.auth_user_header,
            email_header=settings.auth_email_header or None,
            groups_header=settings.auth_groups_header or None,
            groups_separator=settings.auth_groups_separator,
            trusted_proxies=_split_csv(settings.auth_trusted_proxies),
        )
    if backend == "webhook":
        return WebhookAuthenticator(
            verify_url=settings.auth_verify_url or "",
            http_client=http_client,
            timeout=settings.auth_verify_timeout,
        )
    if backend == "apikey":
        return ApiKeyAuthenticator()

    if ":" in backend:
        return _load_module_authenticator(backend, settings, http_client)

    raise ValueError(
        f"Unknown AUTH_BACKEND={backend!r}. Built-ins: "
        f"{sorted(BUILTIN_BACKENDS)}, or a 'module.path:ClassName' string."
    )


def _load_module_authenticator(
    spec: str, settings: "Settings", http_client: httpx.AsyncClient
) -> Authenticator:
    """Import ``module.path:ClassName`` and instantiate it.

    The class is called with keyword args ``settings=...`` and
    ``http_client=...``; custom backends declare whichever they need
    (or ``**kwargs`` to accept both). It must return an
    :class:`Authenticator` instance.
    """
    try:
        module_path, _, class_name = spec.partition(":")
        if not module_path or not class_name:
            raise ValueError(f"Expected 'module:Class', got {spec!r}")
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
    except (ImportError, AttributeError, ValueError) as exc:
        raise ValueError(
            f"Could not load AUTH_BACKEND={spec!r}: {exc}. The module must "
            "be importable from the server's PYTHONPATH and the class must "
            "subclass lex_align_server.authn.Authenticator."
        ) from exc

    instance = cls(settings=settings, http_client=http_client)
    if not isinstance(instance, Authenticator):
        raise TypeError(
            f"{spec} produced {type(instance).__name__}, which does not "
            "subclass lex_align_server.authn.Authenticator."
        )
    return instance


def _split_csv(value: str) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]
