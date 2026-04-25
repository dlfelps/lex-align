"""Unit tests for the OSV CVE adapter."""

from __future__ import annotations

import httpx
import pytest

from lex_align_server.cache import JsonCache
from lex_align_server.cve import _summarize_vulns, query_osv, resolve_cves


def test_summarize_vulns_picks_max_score():
    vulns = [
        {"id": "GHSA-aaaa-bbbb-cccc", "database_specific": {"cvss": {"score": 7.5}}},
        {"id": "PYSEC-2024-001", "severity": [{"type": "CVSS_V3", "score": "CVSS_BASE_SCORE=9.1"}]},
        {"id": "CVE-1999-9999"},
    ]
    info = _summarize_vulns(vulns)
    assert info.max_score == pytest.approx(9.1)
    assert info.raw_count == 3
    assert "PYSEC-2024-001" in info.ids


def test_summarize_vulns_empty():
    info = _summarize_vulns([])
    assert info.max_score is None
    assert info.raw_count == 0
    assert info.ids == []


@pytest.mark.asyncio
async def test_query_osv_sends_correct_payload():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = request.read()
        return httpx.Response(200, json={"vulns": []})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await query_osv("redis", "5.0.0", "https://api.osv.dev/v1/query", client)
    assert result == []
    assert b'"redis"' in captured["json"]
    assert b'"PyPI"' in captured["json"]
    assert b'"5.0.0"' in captured["json"]


@pytest.mark.asyncio
async def test_query_osv_returns_none_on_failure():
    transport = httpx.MockTransport(lambda r: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await query_osv("redis", None, "https://api.osv.dev/v1/query", client) is None


class _MemCache(JsonCache):
    def __init__(self) -> None:  # type: ignore[no-untyped-def]
        self._store: dict = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ttl_seconds):
        self._store[key] = value


@pytest.mark.asyncio
async def test_resolve_cves_caches_positive_results():
    calls = {"n": 0}
    payload = {"vulns": [{"id": "GHSA-1", "database_specific": {"cvss": {"score": 7.5}}}]}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    cache = _MemCache()
    async with httpx.AsyncClient(transport=transport) as client:
        first = await resolve_cves("redis", "5.0.0", cache, 60, "https://api", client)
        second = await resolve_cves("redis", "5.0.0", cache, 60, "https://api", client)
    assert first.max_score == pytest.approx(7.5)
    assert second.max_score == pytest.approx(7.5)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_resolve_cves_does_not_cache_outage():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    cache = _MemCache()
    async with httpx.AsyncClient(transport=transport) as client:
        await resolve_cves("redis", "5.0.0", cache, 60, "https://api", client)
        await resolve_cves("redis", "5.0.0", cache, 60, "https://api", client)
    # Both calls should hit OSV — outage must not be cached.
    assert calls["n"] == 2
