"""FastAPI route tests using the in-process TestClient."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lex_align_server.api.v1 import (
    approval_requests as approval_router,
    evaluate as evaluate_router,
    health as health_router,
    registry as registry_router,
    reports as reports_router,
)
from lex_align_server.audit import AuditStore
from lex_align_server.cache import JsonCache
from lex_align_server.config import Settings
from lex_align_server.dashboards import router as dashboards_router
from lex_align_server.registry import Registry
from lex_align_server.state import AppState


class MemCache(JsonCache):
    def __init__(self) -> None:  # type: ignore[no-untyped-def]
        self._store: dict = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ttl_seconds):
        self._store[key] = value

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def _registry() -> Registry:
    return Registry.from_dict({
        "version": "1",
        "global_policies": {
            "auto_approve_licenses": ["MIT"],
            "hard_ban_licenses": ["GPL-3.0"],
            "cve_threshold": 0.9,
        },
        "packages": {
            "requests": {"status": "deprecated", "replacement": "httpx"},
            "httpx": {"status": "preferred"},
        },
    })


def _build_app(tmp_path, *, auth_enabled: bool = False) -> FastAPI:
    settings = Settings(
        database_path=tmp_path / "audit.sqlite",
        auth_enabled=auth_enabled,
        osv_api_url="https://osv.test/v1/query",
        pypi_api_url="https://pypi.test/pypi",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cache = MemCache()
        audit = AuditStore(settings.database_path)
        await audit.init()

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "osv" in url:
                return httpx.Response(200, json={"vulns": []})
            return httpx.Response(200, json={
                "info": {"license": "MIT", "version": "1.0.0"}
            })

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        app.state.lex = AppState(
            settings=settings, cache=cache, audit=audit,
            http=client, registry=_registry(),
        )
        yield
        await client.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(evaluate_router.router, prefix="/api/v1")
    app.include_router(approval_router.router, prefix="/api/v1")
    app.include_router(reports_router.router, prefix="/api/v1")
    app.include_router(registry_router.router, prefix="/api/v1")
    app.include_router(health_router.router, prefix="/api/v1")
    app.include_router(dashboards_router.router)
    return app


def test_evaluate_requires_project_header(tmp_path):
    with TestClient(_build_app(tmp_path)) as client:
        r = client.get("/api/v1/evaluate", params={"package": "httpx"})
        assert r.status_code == 400
        assert "X-LexAlign-Project" in r.json()["detail"]


def test_evaluate_returns_allowed_for_preferred(tmp_path):
    with TestClient(_build_app(tmp_path)) as client:
        r = client.get(
            "/api/v1/evaluate",
            params={"package": "httpx"},
            headers={"X-LexAlign-Project": "demo"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["verdict"] == "ALLOWED"
        assert body["registry_status"] == "preferred"


def test_evaluate_blocks_deprecated(tmp_path):
    with TestClient(_build_app(tmp_path)) as client:
        r = client.get(
            "/api/v1/evaluate",
            params={"package": "requests"},
            headers={"X-LexAlign-Project": "demo"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["verdict"] == "DENIED"
        assert body["replacement"] == "httpx"


def test_approval_requests_persisted(tmp_path):
    with TestClient(_build_app(tmp_path)) as client:
        headers = {"X-LexAlign-Project": "demo"}
        r = client.post(
            "/api/v1/approval-requests",
            json={"package": "newpkg", "rationale": "needed"},
            headers=headers,
        )
        assert r.status_code == 202
        body = r.json()
        assert body["package"] == "newpkg"
        assert body["status"] == "PENDING_REVIEW"

        r = client.get("/api/v1/reports/approval-requests", params={"project": "demo"})
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["rationale"] == "needed"


def test_health_endpoint(tmp_path):
    with TestClient(_build_app(tmp_path)) as client:
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        body = r.json()
        assert body["redis"] == "ok"
        assert body["db"] == "ok"
        assert body["registry_loaded"] is True


def test_dashboards_render_when_auth_disabled(tmp_path):
    """Dashboards are now available in single-user mode too."""
    with TestClient(_build_app(tmp_path)) as client:
        assert client.get("/dashboard/security").status_code == 200
        assert client.get("/dashboard/legal").status_code == 200
        r = client.get("/dashboard/registry")
        assert r.status_code == 200
        assert "Registry workshop" in r.text


def test_registry_endpoint_returns_yaml_shape(tmp_path):
    with TestClient(_build_app(tmp_path)) as client:
        r = client.get("/api/v1/registry")
        assert r.status_code == 200
        body = r.json()
        assert body["version"] == "1"
        assert "global_policies" in body
        assert body["packages"]["httpx"]["status"] == "preferred"
        assert body["packages"]["requests"]["replacement"] == "httpx"


def test_parse_yaml_accepts_valid_document(tmp_path):
    yaml_text = """
version: "1.0"
global_policies:
  auto_approve_licenses: [MIT]
  cve_threshold: 0.8
packages:
  httpx:
    status: preferred
    reason: ok
"""
    with TestClient(_build_app(tmp_path)) as client:
        r = client.post("/api/v1/registry/parse-yaml", json={"yaml_text": yaml_text})
        assert r.status_code == 200
        body = r.json()
        assert body["version"] == "1.0"
        assert body["packages"]["httpx"]["status"] == "preferred"


def test_pending_endpoint_filters_registered_and_groups(tmp_path):
    """Pending requests for unregistered packages are surfaced; requests
    for packages already in the registry are filtered out; multiple
    requests for the same package collapse into one row."""
    with TestClient(_build_app(tmp_path)) as client:
        headers = {"X-LexAlign-Project": "demo"}
        # Two pending requests for numpy across different requesters by way
        # of different projects (the dedupe index is per requester).
        client.post("/api/v1/approval-requests",
                    json={"package": "numpy", "rationale": "math"},
                    headers={"X-LexAlign-Project": "p1"})
        client.post("/api/v1/approval-requests",
                    json={"package": "numpy", "rationale": "more"},
                    headers={"X-LexAlign-Project": "p2"})
        # A request for httpx, which IS in the registry — must be filtered out.
        client.post("/api/v1/approval-requests",
                    json={"package": "httpx", "rationale": "already in"},
                    headers=headers)
        # An unrelated pending request.
        client.post("/api/v1/approval-requests",
                    json={"package": "scipy", "rationale": "science"},
                    headers=headers)

        r = client.get("/api/v1/registry/pending")
        assert r.status_code == 200
        items = {i["package"]: i for i in r.json()["items"]}
        assert set(items) == {"numpy", "scipy"}
        assert items["numpy"]["request_count"] == 2


def test_parse_yaml_rejects_invalid_document(tmp_path):
    # `deprecated` without a replacement must fail validation.
    yaml_text = """
version: "1"
packages:
  requests:
    status: deprecated
"""
    with TestClient(_build_app(tmp_path)) as client:
        r = client.post("/api/v1/registry/parse-yaml", json={"yaml_text": yaml_text})
        assert r.status_code == 400
        assert "replacement" in r.json()["detail"]


def test_legal_and_security_reports_separate_categories(tmp_path):
    """A license-blocked package shows up only in /legal; a CVE-blocked
    package only in /security."""
    # Reuse the standard registry, but inject a GPL package PyPI response so
    # the orchestrator routes through the license check.
    settings = Settings(
        database_path=tmp_path / "audit.sqlite",
        osv_api_url="https://osv.test/v1/query",
        pypi_api_url="https://pypi.test/pypi",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cache = MemCache()
        audit = AuditStore(settings.database_path)
        await audit.init()

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "osv" in url:
                payload = (request.read() or b"")
                if b"dangerous" in payload:
                    return httpx.Response(200, json={
                        "vulns": [{"id": "GHSA-x", "database_specific": {"cvss": {"score": 9.8}}}]
                    })
                return httpx.Response(200, json={"vulns": []})
            if "/gplpkg" in url:
                return httpx.Response(200, json={"info": {"license": "GPL-3.0", "version": "1"}})
            return httpx.Response(200, json={"info": {"license": "MIT", "version": "1"}})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        app.state.lex = AppState(
            settings=settings, cache=cache, audit=audit,
            http=client, registry=_registry(),
        )
        yield
        await client.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(evaluate_router.router, prefix="/api/v1")
    app.include_router(reports_router.router, prefix="/api/v1")

    with TestClient(app) as client:
        headers = {"X-LexAlign-Project": "demo"}
        client.get("/api/v1/evaluate", params={"package": "gplpkg"}, headers=headers)
        client.get(
            "/api/v1/evaluate",
            params={"package": "dangerous", "version": "1.0.0"},
            headers=headers,
        )
        legal = client.get("/api/v1/reports/legal").json()
        security = client.get("/api/v1/reports/security").json()
    assert legal["total_denials"] == 1
    assert legal["recent"][0]["package"] == "gplpkg"
    assert security["total_denials"] == 1
    assert security["recent"][0]["package"] == "dangerous"
