"""HTTP client tests using httpx.MockTransport."""

from __future__ import annotations

import os

import httpx
import pytest

from lex_align_client.api import (
    LexAlignClient,
    PROJECT_HEADER,
    ServerError,
    ServerUnreachable,
)
from lex_align_client.config import ClientConfig


def _client(config: ClientConfig, transport: httpx.BaseTransport) -> LexAlignClient:
    http = httpx.Client(transport=transport, timeout=1.0)
    return LexAlignClient(config, http_client=http)


def test_check_sends_project_header():
    captured: dict = {}

    def handler(request):
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={
            "verdict": "ALLOWED", "reason": "ok", "package": "x",
            "version": None, "resolved_version": None, "registry_status": None,
            "replacement": None, "version_constraint": None, "license": None,
            "cve_ids": [], "max_cvss": None, "is_requestable": False,
            "needs_rationale": False,
        })

    cfg = ClientConfig(project="demo", server_url="http://srv")
    with _client(cfg, httpx.MockTransport(handler)) as client:
        verdict = client.check("x")
    assert verdict.verdict == "ALLOWED"
    assert captured["headers"][PROJECT_HEADER.lower()] == "demo"


def test_check_fail_open_returns_synthetic_allow_when_unreachable():
    def handler(request):
        raise httpx.ConnectError("boom")

    cfg = ClientConfig(project="demo", server_url="http://srv", fail_open=True)
    with _client(cfg, httpx.MockTransport(handler)) as client:
        verdict = client.check("x")
    assert verdict.verdict == "ALLOWED"
    assert verdict.transport_error is True


def test_check_fail_closed_raises_when_unreachable():
    def handler(request):
        raise httpx.ConnectError("boom")

    cfg = ClientConfig(project="demo", server_url="http://srv", fail_open=False)
    with _client(cfg, httpx.MockTransport(handler)) as client:
        with pytest.raises(ServerUnreachable):
            client.check("x")


def test_check_raises_server_error_on_4xx():
    def handler(request):
        return httpx.Response(400, json={"detail": "bad"})

    cfg = ClientConfig(project="demo", server_url="http://srv")
    with _client(cfg, httpx.MockTransport(handler)) as client:
        with pytest.raises(ServerError) as info:
            client.check("x")
    assert info.value.status_code == 400
    assert "bad" in info.value.detail


def test_org_mode_attaches_bearer_token(monkeypatch):
    captured: dict = {}

    def handler(request):
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={
            "verdict": "ALLOWED", "reason": "", "package": "x",
            "version": None, "resolved_version": None, "registry_status": None,
            "replacement": None, "version_constraint": None, "license": None,
            "cve_ids": [], "max_cvss": None, "is_requestable": False,
            "needs_rationale": False,
        })

    monkeypatch.setenv("LEXALIGN_API_KEY", "secret-token")
    cfg = ClientConfig(project="demo", server_url="http://srv", mode="org")
    with _client(cfg, httpx.MockTransport(handler)) as client:
        client.check("x")
    assert captured["auth"] == "Bearer secret-token"


def test_check_sends_agent_headers_from_kwargs():
    """Explicit agent_model/agent_version kwargs propagate as
    X-LexAlign-Agent-* headers so the server can tag the audit row."""
    captured: dict = {}

    def handler(request):
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={
            "verdict": "ALLOWED", "reason": "", "package": "x",
            "version": None, "resolved_version": None, "registry_status": None,
            "replacement": None, "version_constraint": None, "license": None,
            "cve_ids": [], "max_cvss": None, "is_requestable": False,
            "needs_rationale": False,
        })

    cfg = ClientConfig(project="demo", server_url="http://srv")
    http = httpx.Client(transport=httpx.MockTransport(handler), timeout=1.0)
    with LexAlignClient(cfg, http_client=http,
                        agent_model="opus", agent_version="4.7") as client:
        client.check("x")
    assert captured["headers"]["x-lexalign-agent-model"] == "opus"
    assert captured["headers"]["x-lexalign-agent-version"] == "4.7"


def test_agent_headers_default_to_env_vars(monkeypatch):
    captured: dict = {}

    def handler(request):
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={
            "verdict": "ALLOWED", "reason": "", "package": "x",
            "version": None, "resolved_version": None, "registry_status": None,
            "replacement": None, "version_constraint": None, "license": None,
            "cve_ids": [], "max_cvss": None, "is_requestable": False,
            "needs_rationale": False,
        })

    monkeypatch.setenv("LEXALIGN_AGENT_MODEL", "sonnet")
    monkeypatch.setenv("LEXALIGN_AGENT_VERSION", "4.6")
    cfg = ClientConfig(project="demo", server_url="http://srv")
    http = httpx.Client(transport=httpx.MockTransport(handler), timeout=1.0)
    with LexAlignClient(cfg, http_client=http) as client:
        client.check("x")
    assert captured["headers"]["x-lexalign-agent-model"] == "sonnet"
    assert captured["headers"]["x-lexalign-agent-version"] == "4.6"


def test_agent_headers_omitted_when_unset(monkeypatch):
    """If neither kwargs nor env vars supply an identity, the client
    omits the headers entirely; the server then buckets the row as
    "unknown agent" rather than rejecting it."""
    captured: dict = {}

    def handler(request):
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={
            "verdict": "ALLOWED", "reason": "", "package": "x",
            "version": None, "resolved_version": None, "registry_status": None,
            "replacement": None, "version_constraint": None, "license": None,
            "cve_ids": [], "max_cvss": None, "is_requestable": False,
            "needs_rationale": False,
        })

    monkeypatch.delenv("LEXALIGN_AGENT_MODEL", raising=False)
    monkeypatch.delenv("LEXALIGN_AGENT_VERSION", raising=False)
    cfg = ClientConfig(project="demo", server_url="http://srv")
    http = httpx.Client(transport=httpx.MockTransport(handler), timeout=1.0)
    with LexAlignClient(cfg, http_client=http) as client:
        client.check("x")
    assert "x-lexalign-agent-model" not in captured["headers"]
    assert "x-lexalign-agent-version" not in captured["headers"]


def test_request_approval_returns_payload():
    captured: dict = {}

    def handler(request):
        captured["body"] = request.read()
        return httpx.Response(202, json={
            "request_id": "abc", "status": "PENDING_REVIEW",
            "package": "newpkg", "project": "demo",
        })

    cfg = ClientConfig(project="demo", server_url="http://srv")
    with _client(cfg, httpx.MockTransport(handler)) as client:
        body = client.request_approval("newpkg", "needed")
    assert body["status"] == "PENDING_REVIEW"
    assert b"newpkg" in captured["body"]
