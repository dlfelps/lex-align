"""Unit tests for license normalization and policy evaluation."""

from __future__ import annotations

import httpx
import pytest

from lex_align_server.cache import JsonCache
from lex_align_server.licenses import (
    evaluate_license,
    fetch_license_from_pypi,
    normalize_license,
    resolve_license,
)
from lex_align_server.registry import Action, GlobalPolicies


@pytest.mark.parametrize("raw,expected", [
    ("MIT", "MIT"),
    ("Apache 2.0", "Apache-2.0"),
    ("BSD 3-Clause", "BSD-3-Clause"),
    ("AGPLv3", "AGPL-3.0"),
    ("LGPL v3", "LGPL-3.0"),
    ("LGPL v2.1", "LGPL-2.1"),
    ("GNU General Public License v3", "GPL-3.0"),
    ("Proprietary", "Proprietary"),
    ("Some made-up legalese", "UNKNOWN"),
    (None, "UNKNOWN"),
    ("", "UNKNOWN"),
])
def test_normalize_license(raw, expected):
    assert normalize_license(raw) == expected


def test_evaluate_license_block_takes_precedence():
    gp = GlobalPolicies.from_dict({
        "auto_approve_licenses": ["MIT"],
        "hard_ban_licenses": ["GPL-3.0"],
    })
    assert evaluate_license("MIT", gp).action is Action.ALLOW
    assert evaluate_license("GPL-3.0", gp).action is Action.BLOCK
    # Not in either list → block (must be explicitly auto-approved).
    assert evaluate_license("Apache-2.0", gp).action is Action.BLOCK


def test_evaluate_license_unknown_policy_allow():
    gp = GlobalPolicies.from_dict({"unknown_license_policy": "allow"})
    v = evaluate_license("UNKNOWN", gp)
    assert v.action is Action.ALLOW
    assert v.needs_human_review is False


def test_evaluate_license_unknown_policy_block():
    gp = GlobalPolicies.from_dict({"unknown_license_policy": "block"})
    v = evaluate_license("UNKNOWN", gp)
    assert v.action is Action.BLOCK
    assert v.needs_human_review is False


def test_evaluate_license_unknown_policy_pending_approval():
    gp = GlobalPolicies.from_dict({"unknown_license_policy": "pending_approval"})
    v = evaluate_license("UNKNOWN", gp)
    assert v.action is Action.ALLOW
    assert v.needs_human_review is True
    assert "pending human review" in v.reason


def test_evaluate_license_unknown_policy_default_is_pending_approval():
    gp = GlobalPolicies.from_dict({})
    v = evaluate_license("UNKNOWN", gp)
    assert v.action is Action.ALLOW
    assert v.needs_human_review is True


@pytest.mark.asyncio
async def test_fetch_license_extracts_pypi_payload():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"info": {"license": "MIT", "version": "1.0.0"}}
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        raw, latest = await fetch_license_from_pypi("foo", None, "https://pypi", client)
    assert raw == "MIT"
    assert latest == "1.0.0"


@pytest.mark.asyncio
async def test_fetch_license_falls_back_to_classifiers():
    payload = {"info": {"license": "", "classifiers": [
        "License :: OSI Approved :: MIT License",
    ], "version": "0.1"}}
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        raw, _ = await fetch_license_from_pypi("foo", None, "https://pypi", client)
    assert raw and "MIT" in raw


@pytest.mark.asyncio
async def test_fetch_license_returns_none_on_error_status():
    transport = httpx.MockTransport(lambda r: httpx.Response(404))
    async with httpx.AsyncClient(transport=transport) as client:
        raw, latest = await fetch_license_from_pypi("foo", None, "https://pypi", client)
    assert raw is None
    assert latest is None


class _MemCache(JsonCache):
    """In-memory JsonCache double — never touches Redis."""
    def __init__(self) -> None:  # type: ignore[no-untyped-def]
        self._store: dict = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ttl_seconds):
        self._store[key] = value


@pytest.mark.asyncio
async def test_resolve_license_caches_after_first_fetch():
    payload = {"info": {"license": "MIT", "version": "1.2.3"}}
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        return httpx.Response(200, json=payload)

    cache = _MemCache()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        info1, latest1 = await resolve_license("foo", None, cache, 60, "https://pypi", client)
        info2, latest2 = await resolve_license("foo", None, cache, 60, "https://pypi", client)
    assert calls["count"] == 1
    assert info1.license_normalized == "MIT"
    assert info2.license_normalized == "MIT"
    assert latest1 == "1.2.3"
    assert latest2 == "1.2.3"
