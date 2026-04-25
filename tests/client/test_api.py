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
