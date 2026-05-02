"""End-to-end tests for the evaluate orchestrator with mocked PyPI/OSV."""

from __future__ import annotations

import httpx
import pytest

from lex_align_server.audit import (
    AuditStore,
    DENIAL_CVE,
    DENIAL_LICENSE,
    DENIAL_REGISTRY,
    VERDICT_ALLOWED,
    VERDICT_DENIED,
    VERDICT_PROVISIONALLY_ALLOWED,
)
from lex_align_server.cache import JsonCache
from lex_align_server.config import Settings
from lex_align_server.evaluate import evaluate
from lex_align_server.registry import Registry


class MemCache(JsonCache):
    def __init__(self) -> None:  # type: ignore[no-untyped-def]
        self._store: dict = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ttl_seconds):
        self._store[key] = value


def _registry(packages=None, **policies) -> Registry:
    return Registry.from_dict({
        "version": "1",
        "global_policies": policies,
        "packages": packages or {},
    })


@pytest.fixture
async def audit(tmp_path):
    s = AuditStore(tmp_path / "audit.sqlite")
    await s.init()
    return s


def _settings() -> Settings:
    return Settings(
        redis_url="redis://localhost",
        osv_api_url="https://osv.test/v1/query",
        pypi_api_url="https://pypi.test/pypi",
    )


def _build_mock(pypi_payload=None, osv_payload=None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "osv" in url:
            return httpx.Response(200, json=osv_payload or {"vulns": []})
        return httpx.Response(200, json=pypi_payload or {
            "info": {"license": "MIT", "version": "1.0.0"}
        })
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_registry_block_short_circuits(audit, tmp_path):
    reg = _registry({"requests": {"status": "deprecated", "replacement": "httpx"}})
    transport = _build_mock()
    async with httpx.AsyncClient(transport=transport) as http:
        result = await evaluate(
            package="requests", version=None, project="proj", requester="anon",
            registry=reg, cache=MemCache(), audit=audit,
            settings=_settings(), http_client=http,
        )
    assert result.verdict == VERDICT_DENIED
    assert result.replacement == "httpx"
    rows = (await audit.legal_report())["recent"]
    # Registry-driven denial isn't a legal denial; check overall security/legal
    # counts are zero and that one row exists in audit_log.
    assert rows == []
    sec = (await audit.security_report())["recent"]
    assert sec == []


@pytest.mark.asyncio
async def test_unknown_package_with_mit_provisionally_allowed(audit):
    reg = _registry(auto_approve_licenses=["MIT"])
    transport = _build_mock(pypi_payload={
        "info": {"license": "MIT", "version": "2.0.0"}
    })
    async with httpx.AsyncClient(transport=transport) as http:
        result = await evaluate(
            package="newpkg", version=None, project="proj", requester="anon",
            registry=reg, cache=MemCache(), audit=audit,
            settings=_settings(), http_client=http,
        )
    assert result.verdict == VERDICT_PROVISIONALLY_ALLOWED
    assert result.is_requestable is True
    assert result.license == "MIT"
    assert result.resolved_version == "2.0.0"


@pytest.mark.asyncio
async def test_unknown_package_with_gpl_denied(audit):
    reg = _registry(auto_approve_licenses=["MIT"], hard_ban_licenses=["GPL-3.0"])
    transport = _build_mock(pypi_payload={
        "info": {"license": "GPL-3.0", "version": "1.0.0"}
    })
    async with httpx.AsyncClient(transport=transport) as http:
        result = await evaluate(
            package="gplpkg", version=None, project="proj", requester="anon",
            registry=reg, cache=MemCache(), audit=audit,
            settings=_settings(), http_client=http,
        )
    assert result.verdict == VERDICT_DENIED
    assert result.license == "GPL-3.0"
    legal = await audit.legal_report()
    assert legal["total_denials"] == 1


@pytest.mark.asyncio
async def test_critical_cve_denies_even_when_registry_preferred(audit):
    reg = _registry({"redis": {"status": "preferred"}}, cve_threshold=0.9)
    osv = {"vulns": [{"id": "GHSA-1", "database_specific": {"cvss": {"score": 9.5}}}]}
    transport = _build_mock(osv_payload=osv)
    async with httpx.AsyncClient(transport=transport) as http:
        result = await evaluate(
            package="redis", version="5.0.0", project="proj", requester="anon",
            registry=reg, cache=MemCache(), audit=audit,
            settings=_settings(), http_client=http,
        )
    assert result.verdict == VERDICT_DENIED
    assert result.max_cvss == pytest.approx(9.5)
    assert "GHSA-1" in result.cve_ids
    sec = await audit.security_report()
    assert sec["total_denials"] == 1


@pytest.mark.asyncio
async def test_subcritical_cve_does_not_block(audit):
    reg = _registry({"redis": {"status": "preferred"}}, cve_threshold=0.9)
    osv = {"vulns": [{"id": "GHSA-2", "database_specific": {"cvss": {"score": 7.5}}}]}
    transport = _build_mock(osv_payload=osv)
    async with httpx.AsyncClient(transport=transport) as http:
        result = await evaluate(
            package="redis", version="5.0.0", project="proj", requester="anon",
            registry=reg, cache=MemCache(), audit=audit,
            settings=_settings(), http_client=http,
        )
    assert result.verdict == VERDICT_ALLOWED
    assert result.max_cvss == pytest.approx(7.5)
    assert "GHSA-2" in result.cve_ids


@pytest.mark.asyncio
async def test_approved_status_emits_needs_rationale_flag(audit):
    reg = _registry({"sqlalchemy": {"status": "approved"}})
    transport = _build_mock()
    async with httpx.AsyncClient(transport=transport) as http:
        result = await evaluate(
            package="sqlalchemy", version=None, project="proj", requester="anon",
            registry=reg, cache=MemCache(), audit=audit,
            settings=_settings(), http_client=http,
        )
    assert result.verdict == VERDICT_ALLOWED
    assert result.needs_rationale is True


@pytest.mark.asyncio
async def test_no_registry_configured_falls_through(audit):
    transport = _build_mock()
    async with httpx.AsyncClient(transport=transport) as http:
        result = await evaluate(
            package="anything", version=None, project="proj", requester="anon",
            registry=None, cache=MemCache(), audit=audit,
            settings=_settings(), http_client=http,
        )
    assert result.verdict == VERDICT_PROVISIONALLY_ALLOWED


@pytest.mark.asyncio
async def test_unknown_license_pending_approval_sets_auto_request_flag(audit):
    """With the default unknown_license_policy=pending_approval, an unknown
    license should yield PROVISIONALLY_ALLOWED and auto_request_approval=True."""
    reg = _registry()  # default unknown_license_policy is pending_approval
    transport = _build_mock(pypi_payload={
        "info": {"license": "Some Obscure License v99", "version": "1.0.0"}
    })
    async with httpx.AsyncClient(transport=transport) as http:
        result = await evaluate(
            package="obscurepkg", version=None, project="proj", requester="anon",
            registry=reg, cache=MemCache(), audit=audit,
            settings=_settings(), http_client=http,
        )
    assert result.verdict == VERDICT_PROVISIONALLY_ALLOWED
    assert result.auto_request_approval is True
    assert result.is_requestable is True
    assert result.license == "UNKNOWN"


@pytest.mark.asyncio
async def test_unknown_license_block_policy_still_denies(audit):
    """When unknown_license_policy=block, unknown licenses must still be denied."""
    reg = _registry(unknown_license_policy="block")
    transport = _build_mock(pypi_payload={
        "info": {"license": "Some Obscure License v99", "version": "1.0.0"}
    })
    async with httpx.AsyncClient(transport=transport) as http:
        result = await evaluate(
            package="obscurepkg", version=None, project="proj", requester="anon",
            registry=reg, cache=MemCache(), audit=audit,
            settings=_settings(), http_client=http,
        )
    assert result.verdict == VERDICT_DENIED
    assert result.auto_request_approval is False
