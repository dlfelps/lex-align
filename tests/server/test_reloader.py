"""Tests for the registry reload coordinator and the implicit-candidates
audit query."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
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
from lex_align_server.audit import (
    APPROVAL_PENDING,
    ApprovalRequest,
    AuditRecord,
    AuditStore,
    DENIAL_CVE,
    DENIAL_LICENSE,
    DENIAL_NONE,
    VERDICT_ALLOWED,
    VERDICT_DENIED,
    VERDICT_PROVISIONALLY_ALLOWED,
)
from lex_align_server.authn import load_authenticator
from lex_align_server.cache import JsonCache
from lex_align_server.config import Settings
from lex_align_server.dashboards import router as dashboards_router
from lex_align_server.proposer import load_proposer
from lex_align_server.registry import Registry
from lex_align_server.reloader import reload_registry
from lex_align_server.state import AppState


# Re-use the test memory cache from test_api so we don't depend on Redis.
class MemCache(JsonCache):
    def __init__(self) -> None:  # type: ignore[no-untyped-def]
        self._store: dict = {}

    async def get(self, key):       return self._store.get(key)
    async def set(self, key, value, ttl_seconds): self._store[key] = value
    async def ping(self) -> bool:   return True
    async def close(self) -> None:  return None


def _registry() -> Registry:
    return Registry.from_dict({
        "version": "1",
        "global_policies": {
            "auto_approve_licenses": ["MIT"],
            "hard_ban_licenses": [],
            "cve_threshold": 0.9,
        },
        "packages": {"httpx": {"status": "preferred"}},
    })


def _build_app(tmp_path: Path, *, registry_path: Path | None) -> FastAPI:
    settings = Settings(
        database_path=tmp_path / "audit.sqlite",
        registry_path=registry_path,
        registry_webhook_secret="testsecret",
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
                "info": {"license": "MIT", "version": "1.0.0"},
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
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


# ── reload_registry direct ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reload_registry_swaps_in_place_and_flips_pending(tmp_path):
    """A YAML edit followed by reload_registry should swap the live
    registry and flip the matching pending requests to APPROVED."""
    registry_path = tmp_path / "registry.yml"
    registry_path.write_text(
        "version: '1'\n"
        "global_policies: {auto_approve_licenses: [MIT]}\n"
        "packages:\n"
        "  httpx: {status: preferred}\n"
    )
    # Build state by hand (avoid TestClient since we want to call reload directly).
    settings = Settings(
        database_path=tmp_path / "audit.sqlite",
        registry_path=registry_path,
    )
    audit = AuditStore(settings.database_path)
    await audit.init()
    await audit.upsert_approval_request(ApprovalRequest(
        project="p1", requester="alice", package="numpy", rationale="math",
    ))

    state = type("S", (), {})()
    state.settings = settings
    state.audit = audit
    state.registry = _registry()  # only httpx in there

    # Edit the YAML to add numpy.
    registry_path.write_text(
        "version: '1'\n"
        "global_policies: {auto_approve_licenses: [MIT]}\n"
        "packages:\n"
        "  httpx: {status: preferred}\n"
        "  numpy: {status: approved}\n"
    )

    result = await reload_registry(state)
    assert result.ok
    assert result.added_packages == 1
    assert result.approved_requests == 1
    assert "numpy" in state.registry.packages
    rows = await audit.list_approval_requests()
    assert rows[0]["status"] == "APPROVED"


@pytest.mark.asyncio
async def test_reload_registry_rejects_invalid_yaml_keeps_old(tmp_path):
    """If the on-disk YAML is invalid, reload returns ok=False and the
    in-memory registry is untouched."""
    registry_path = tmp_path / "registry.yml"
    registry_path.write_text(
        "version: '1'\n"
        "packages:\n"
        "  bad: {status: deprecated}\n"  # missing required `replacement`
    )
    settings = Settings(
        database_path=tmp_path / "audit.sqlite",
        registry_path=registry_path,
    )
    audit = AuditStore(settings.database_path)
    await audit.init()

    state = type("S", (), {})()
    state.settings = settings
    state.audit = audit
    original = _registry()
    state.registry = original

    result = await reload_registry(state)
    assert not result.ok
    assert "replacement" in result.detail
    # In-memory registry is unchanged.
    assert state.registry is original


# ── implicit candidates audit query ──────────────────────────────────────


@pytest.mark.asyncio
async def test_implicit_candidates_classifies_by_reason(tmp_path):
    """Each implicit row carries a ``reason`` matching one of the three
    buckets, ranked provisional > denied > pre-screened."""
    audit = AuditStore(tmp_path / "audit.sqlite")
    await audit.init()

    common = dict(
        project="p1", requester="anon", version=None, resolved_version=None,
    )

    # 1. provisional-no-rationale: a single PROVISIONALLY_ALLOWED that
    #    never got an explicit approval-request follow-up.
    await audit.record_evaluation(AuditRecord(
        package="prov_pkg", verdict=VERDICT_PROVISIONALLY_ALLOWED,
        denial_category=DENIAL_NONE, reason="", **common,
    ))

    # 2. repeatedly-denied: 3+ denials suggest an explicit registry rule
    #    is overdue.
    for _ in range(4):
        await audit.record_evaluation(AuditRecord(
            package="denied_pkg", verdict=VERDICT_DENIED,
            denial_category=DENIAL_LICENSE, reason="GPL", license="GPL-3.0",
            **common,
        ))

    # 3. pre-screened: a couple of plain ALLOWED checks. Lowest signal.
    for _ in range(2):
        await audit.record_evaluation(AuditRecord(
            package="checked_pkg", verdict=VERDICT_ALLOWED,
            denial_category=DENIAL_NONE, reason="", **common,
        ))

    # An explicit approval request — must be filtered out so it doesn't
    # double-count alongside the existing pending panel.
    await audit.upsert_approval_request(ApprovalRequest(
        project="p1", requester="alice", package="explicit_pkg", rationale="x",
    ))
    await audit.record_evaluation(AuditRecord(
        package="explicit_pkg", verdict=VERDICT_PROVISIONALLY_ALLOWED,
        denial_category=DENIAL_NONE, reason="", **common,
    ))

    candidates = await audit.list_implicit_candidates()
    by_pkg = {c["package"]: c for c in candidates}
    assert "explicit_pkg" not in by_pkg
    assert by_pkg["prov_pkg"]["reason"] == "provisional-no-rationale"
    assert by_pkg["denied_pkg"]["reason"] == "repeatedly-denied"
    assert by_pkg["checked_pkg"]["reason"] == "pre-screened"
    # Provisional ranks higher than denied which ranks higher than
    # pre-screened.
    rank = [c["reason"] for c in candidates]
    assert rank.index("provisional-no-rationale") < rank.index("repeatedly-denied")
    assert rank.index("repeatedly-denied") < rank.index("pre-screened")


# ── HTTP endpoints ───────────────────────────────────────────────────────


def test_pending_endpoint_returns_explicit_and_implicit(tmp_path):
    """GET /registry/pending now returns both streams as separate keys."""
    app = _build_app(tmp_path, registry_path=None)
    with TestClient(app) as client:
        # Fire a check on a fresh package to populate audit_log.
        client.get(
            "/api/v1/evaluate",
            params={"package": "newpkg"},
            headers={"X-LexAlign-Project": "demo"},
        )
        body = client.get("/api/v1/registry/pending").json()
        assert "explicit" in body
        assert "implicit" in body


def test_reload_endpoint_returns_409_when_no_registry_path(tmp_path):
    """Operator clicked reload but the server isn't configured for it.
    Surface a 409 with a clear message rather than a silent no-op."""
    app = _build_app(tmp_path, registry_path=None)
    with TestClient(app) as client:
        r = client.post("/api/v1/registry/reload")
        assert r.status_code == 409
        assert "REGISTRY_PATH" in r.json()["detail"]


def test_webhook_rejects_invalid_signature(tmp_path):
    """A request without a valid HMAC signature must be rejected with
    401 — the webhook is the trust boundary into reload."""
    registry_path = tmp_path / "registry.yml"
    registry_path.write_text("version: '1'\nglobal_policies: {}\npackages: {}\n")
    app = _build_app(tmp_path, registry_path=registry_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/registry/webhook",
            content=b'{"action": "closed"}',
            headers={
                "X-Hub-Signature-256": "sha256=deadbeef",
                "X-GitHub-Event": "pull_request",
            },
        )
        assert r.status_code == 401


def test_webhook_accepts_correctly_signed_ping(tmp_path):
    """A signed ping should return 200 — operators rely on this for
    'test webhook' UI in GitHub."""
    import hashlib
    import hmac
    body = b'{"zen":"hi"}'
    secret = "testsecret"
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    registry_path = tmp_path / "registry.yml"
    registry_path.write_text("version: '1'\nglobal_policies: {}\npackages: {}\n")
    app = _build_app(tmp_path, registry_path=registry_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/registry/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "ping",
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json()["event"] == "ping"


def test_webhook_ignores_non_merge_pull_request_events(tmp_path):
    """`opened` / `synchronize` events come through too; we only care
    about merges, not every PR mutation."""
    import hashlib
    import hmac
    import json
    body = json.dumps({
        "action": "opened",
        "pull_request": {"merged": False},
    }).encode()
    sig = "sha256=" + hmac.new(b"testsecret", body, hashlib.sha256).hexdigest()

    registry_path = tmp_path / "registry.yml"
    registry_path.write_text("version: '1'\nglobal_policies: {}\npackages: {}\n")
    app = _build_app(tmp_path, registry_path=registry_path)
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/registry/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "pull_request",
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ignored"]
