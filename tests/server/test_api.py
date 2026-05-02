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
from lex_align_server.authn import load_authenticator
from lex_align_server.proposer import load_proposer
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


def _build_app(tmp_path, *, auth_enabled: bool = False, **settings_overrides) -> FastAPI:
    settings = Settings(
        database_path=tmp_path / "audit.sqlite",
        auth_enabled=auth_enabled,
        osv_api_url="https://osv.test/v1/query",
        pypi_api_url="https://pypi.test/pypi",
        **settings_overrides,
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
            authenticator=load_authenticator(settings, client),
            proposer=load_proposer(settings, client),
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


# The CIDR check is unit-tested in tests/server/test_authn.py — these
# end-to-end tests verify the rest of the pipeline (header read → audit
# row), so they widen trusted_proxies to accept FastAPI TestClient's
# synthetic origin.
_TRUST_ALL = "0.0.0.0/0,::/0"


def test_org_mode_with_header_backend_accepts_forwarded_user(tmp_path):
    """AUTH_ENABLED=true + header backend resolves the requester from
    X-Forwarded-User and the request reaches the evaluator."""
    app = _build_app(tmp_path, auth_enabled=True, auth_trusted_proxies=_TRUST_ALL)
    with TestClient(app) as client:
        r = client.get(
            "/api/v1/evaluate",
            params={"package": "httpx"},
            headers={
                "X-LexAlign-Project": "demo",
                "X-Forwarded-User": "alice@example.com",
            },
        )
        assert r.status_code == 200, r.text


def test_org_mode_rejects_request_missing_forwarded_user(tmp_path):
    """Without an X-Forwarded-User header the request is rejected with
    401 — fails closed when the upstream proxy isn't injecting identity."""
    app = _build_app(tmp_path, auth_enabled=True, auth_trusted_proxies=_TRUST_ALL)
    with TestClient(app) as client:
        r = client.get(
            "/api/v1/evaluate",
            params={"package": "httpx"},
            headers={"X-LexAlign-Project": "demo"},
        )
        assert r.status_code == 401
        assert "X-Forwarded-User" in r.json()["detail"]


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
        a = client.get("/dashboard/agents")
        assert a.status_code == 200
        assert "Agent activity" in a.text


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


def test_evaluate_records_agent_headers_in_audit_row(tmp_path):
    """The X-LexAlign-Agent-Model and -Version headers reported by the
    client must end up on the audit row so reports can group by them."""
    with TestClient(_build_app(tmp_path)) as client:
        r = client.get(
            "/api/v1/evaluate",
            params={"package": "requests"},  # deprecated → DENIED → audit row
            headers={
                "X-LexAlign-Project": "demo",
                "X-LexAlign-Agent-Model": "opus",
                "X-LexAlign-Agent-Version": "4.7",
            },
        )
        assert r.status_code == 200
        legal = client.get("/api/v1/reports/legal").json()  # registry blocks → no rows here
        # Registry-category denials show up in the agents report instead.
        agents = client.get("/api/v1/reports/agents").json()
    by_key = {(a["agent_model"], a["agent_version"]): a for a in agents["agents"]}
    assert by_key[("opus", "4.7")]["evaluations"] >= 1
    assert by_key[("opus", "4.7")]["denials"] >= 1


def test_approval_request_records_agent_identity(tmp_path):
    with TestClient(_build_app(tmp_path)) as client:
        r = client.post(
            "/api/v1/approval-requests",
            json={"package": "newpkg", "rationale": "needed"},
            headers={
                "X-LexAlign-Project": "demo",
                "X-LexAlign-Agent-Model": "opus",
                "X-LexAlign-Agent-Version": "4.7",
            },
        )
        assert r.status_code == 202
        body = r.json()
        assert body["agent_model"] == "opus"
        assert body["agent_version"] == "4.7"
        items = client.get("/api/v1/reports/approval-requests").json()["items"]
        assert items[0]["agent_model"] == "opus"
        assert items[0]["agent_version"] == "4.7"


def test_proposals_endpoint_routes_through_proposer(tmp_path):
    """POST /api/v1/registry/proposals fires the configured proposer.

    With no REGISTRY_PATH / REGISTRY_REPO_URL the loader picks the
    log-only backend, so the response carries ``status='logged'`` and
    no durable change is made. That's the right behaviour for the
    "evaluating lex-align with no policy repo" smoke-test path."""
    with TestClient(_build_app(tmp_path)) as client:
        r = client.post(
            "/api/v1/registry/proposals",
            json={
                "name": "requests-cache",
                "status": "banned",
                "reason": "policy",
                "rationale": "operator triage",
            },
            headers={"X-LexAlign-Project": "demo"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["backend"] == "log_only"
        assert body["status"] == "logged"


def test_proposals_endpoint_rejects_invalid_rule(tmp_path):
    """Validation matches the YAML-compile path: deprecated without
    replacement fails the same way it would in CI."""
    with TestClient(_build_app(tmp_path)) as client:
        r = client.post(
            "/api/v1/registry/proposals",
            json={"name": "foo", "status": "deprecated"},
            headers={"X-LexAlign-Project": "demo"},
        )
        assert r.status_code == 400
        assert "replacement" in r.json()["detail"]


def test_proposals_endpoint_writes_through_local_file(tmp_path):
    """When REGISTRY_PATH is set, the loader picks the local-file
    backend; the YAML on disk reflects the proposed rule, validation
    fails-loud rather than corrupting the file."""
    registry_path = tmp_path / "registry.yml"
    registry_path.write_text(
        "version: '1'\n"
        "global_policies: {}\n"
        "packages: {}\n"
    )
    app = _build_app(tmp_path, registry_path=registry_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/registry/proposals",
            json={"name": "Some-Pkg", "status": "approved", "rationale": "ok"},
            headers={"X-LexAlign-Project": "demo"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["backend"] == "local_file"
        assert body["status"] == "applied"

    # The YAML on disk reflects the proposal.
    import yaml
    on_disk = yaml.safe_load(registry_path.read_text())
    assert on_disk["packages"]["some_pkg"]["status"] == "approved"


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
        body = r.json()
        # Phase 4: split into explicit (approval_requests rows) and
        # implicit (audit_log inferences). For these test inserts only
        # the explicit channel fires.
        items = {i["package"]: i for i in body["explicit"]}
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
            authenticator=load_authenticator(settings, client),
            proposer=load_proposer(settings, client),
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
