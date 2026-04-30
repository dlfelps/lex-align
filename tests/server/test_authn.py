"""Unit tests for the org-mode authentication backends."""

from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from lex_align_server.authn import Authenticator, Identity, load_authenticator
from lex_align_server.authn.anonymous import AnonymousAuthenticator
from lex_align_server.authn.apikey import ApiKeyAuthenticator
from lex_align_server.authn.header import HeaderAuthenticator
from lex_align_server.authn.webhook import WebhookAuthenticator
from lex_align_server.config import Settings


def _make_request(headers: dict[str, str], client_host: str = "127.0.0.1") -> Request:
    """Build a minimal Starlette Request with the given headers + client IP."""
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
        "client": (client_host, 12345),
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


# ── anonymous ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anonymous_authenticator_returns_anonymous_id():
    auth = AnonymousAuthenticator()
    identity = await auth.authenticate(_make_request({}))
    assert identity.id == "anonymous"
    assert identity.email is None
    assert identity.groups == ()


# ── header ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_header_authenticator_reads_forwarded_user():
    auth = HeaderAuthenticator(
        user_header="X-Forwarded-User",
        email_header="X-Forwarded-Email",
        groups_header="X-Forwarded-Groups",
        groups_separator=",",
        trusted_proxies=["127.0.0.1/32"],
    )
    request = _make_request({
        "X-Forwarded-User": "alice@example.com",
        "X-Forwarded-Email": "alice@example.com",
        "X-Forwarded-Groups": "engineering,security",
    })
    identity = await auth.authenticate(request)
    assert identity.id == "alice@example.com"
    assert identity.email == "alice@example.com"
    assert identity.groups == ("engineering", "security")


@pytest.mark.asyncio
async def test_header_authenticator_rejects_untrusted_client():
    """Direct caller can't spoof headers — rejected before headers are read."""
    auth = HeaderAuthenticator(
        user_header="X-Forwarded-User", email_header=None, groups_header=None,
        groups_separator=",", trusted_proxies=["10.0.0.0/8"],
    )
    request = _make_request(
        {"X-Forwarded-User": "attacker"},
        client_host="203.0.113.5",  # outside trusted CIDR
    )
    with pytest.raises(HTTPException) as info:
        await auth.authenticate(request)
    assert info.value.status_code == 401
    assert "trusted_proxies" in info.value.detail


@pytest.mark.asyncio
async def test_header_authenticator_rejects_when_user_header_missing():
    auth = HeaderAuthenticator(
        user_header="X-Forwarded-User", email_header=None, groups_header=None,
        groups_separator=",", trusted_proxies=["127.0.0.1/32"],
    )
    request = _make_request({}, client_host="127.0.0.1")
    with pytest.raises(HTTPException) as info:
        await auth.authenticate(request)
    assert info.value.status_code == 401
    assert "X-Forwarded-User" in info.value.detail


# ── webhook ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_authenticator_calls_verifier_and_returns_identity():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read()
        return httpx.Response(200, json={
            "id": "alice@example.com",
            "email": "alice@example.com",
            "groups": ["security"],
        })

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        auth = WebhookAuthenticator(
            verify_url="http://verifier.example.com/verify",
            http_client=http, timeout=1.0,
        )
        request = _make_request({"Authorization": "Bearer my-token"})
        identity = await auth.authenticate(request)

    assert identity.id == "alice@example.com"
    assert identity.email == "alice@example.com"
    assert identity.groups == ("security",)
    assert b"my-token" in captured["body"]
    assert captured["url"].endswith("/verify")


@pytest.mark.asyncio
async def test_webhook_authenticator_rejects_missing_token():
    async with httpx.AsyncClient() as http:
        auth = WebhookAuthenticator(
            verify_url="http://verifier.example.com/verify",
            http_client=http, timeout=1.0,
        )
        with pytest.raises(HTTPException) as info:
            await auth.authenticate(_make_request({}))
        assert info.value.status_code == 401


@pytest.mark.asyncio
async def test_webhook_authenticator_rejects_when_verifier_says_401():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad token"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        auth = WebhookAuthenticator(
            verify_url="http://v/", http_client=http, timeout=1.0,
        )
        with pytest.raises(HTTPException) as info:
            await auth.authenticate(_make_request({"Authorization": "Bearer x"}))
        assert info.value.status_code == 401


@pytest.mark.asyncio
async def test_webhook_authenticator_rejects_response_without_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"email": "x@y.com"})  # missing `id`

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        auth = WebhookAuthenticator(
            verify_url="http://v/", http_client=http, timeout=1.0,
        )
        with pytest.raises(HTTPException) as info:
            await auth.authenticate(_make_request({"Authorization": "Bearer x"}))
        assert "id" in info.value.detail


# ── apikey stub ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apikey_backend_fails_loud_until_implemented():
    """The stub must reject every request rather than silently allowing
    them — a misconfigured org should never accidentally run unauthenticated."""
    auth = ApiKeyAuthenticator()
    with pytest.raises(HTTPException) as info:
        await auth.authenticate(_make_request({"Authorization": "Bearer x"}))
    assert info.value.status_code == 501
    assert "AUTH_BACKEND=header" in info.value.detail


# ── loader ───────────────────────────────────────────────────────────────


def test_loader_returns_anonymous_when_auth_disabled():
    settings = Settings(auth_enabled=False, auth_backend="header")
    auth = load_authenticator(settings, http_client=None)  # type: ignore[arg-type]
    assert isinstance(auth, AnonymousAuthenticator)


def test_loader_picks_header_backend_by_default_in_org_mode():
    settings = Settings(auth_enabled=True)
    auth = load_authenticator(settings, http_client=None)  # type: ignore[arg-type]
    assert isinstance(auth, HeaderAuthenticator)


def test_loader_rejects_unknown_backend():
    settings = Settings(auth_enabled=True, auth_backend="bogus")
    with pytest.raises(ValueError, match="Unknown AUTH_BACKEND"):
        load_authenticator(settings, http_client=None)  # type: ignore[arg-type]


def test_loader_rejects_webhook_without_verify_url():
    """``WebhookAuthenticator.__init__`` validates the URL up front so a
    misconfigured deployment fails at boot, not on the first request."""
    settings = Settings(auth_enabled=True, auth_backend="webhook")
    with pytest.raises(ValueError, match="AUTH_VERIFY_URL"):
        load_authenticator(settings, http_client=None)  # type: ignore[arg-type]


def test_loader_dynamic_module_load(tmp_path, monkeypatch):
    """An org can drop a Python file with a custom Authenticator and point
    AUTH_BACKEND at it via 'module.path:ClassName'."""
    pkg = tmp_path / "myorg_auth"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "backend.py").write_text(
        "from lex_align_server.authn import Authenticator, Identity\n"
        "class MyAuth(Authenticator):\n"
        "    def __init__(self, **kwargs): pass\n"
        "    async def authenticate(self, request):\n"
        "        return Identity(id='custom-principal')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    settings = Settings(auth_enabled=True, auth_backend="myorg_auth.backend:MyAuth")
    auth = load_authenticator(settings, http_client=None)  # type: ignore[arg-type]
    assert isinstance(auth, Authenticator)
    assert type(auth).__name__ == "MyAuth"


def test_loader_rejects_module_class_that_isnt_an_authenticator(tmp_path, monkeypatch):
    pkg = tmp_path / "badorg_auth"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "backend.py").write_text(
        "class NotAnAuthenticator:\n"
        "    def __init__(self, **kwargs): pass\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    settings = Settings(
        auth_enabled=True, auth_backend="badorg_auth.backend:NotAnAuthenticator"
    )
    with pytest.raises(TypeError, match="subclass"):
        load_authenticator(settings, http_client=None)  # type: ignore[arg-type]
