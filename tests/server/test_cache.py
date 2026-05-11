"""Tests for JsonCache — specifically the disabled-URL sentinel."""

from __future__ import annotations

import pytest

from lex_align_server.cache import JsonCache


@pytest.mark.parametrize("url", ["none", "None", "NONE", "disabled", ""])
async def test_disabled_url_returns_none_on_get(url: str):
    cache = JsonCache(url)
    assert await cache.get("any-key") is None


@pytest.mark.parametrize("url", ["none", "disabled", ""])
async def test_disabled_url_set_is_a_noop(url: str):
    cache = JsonCache(url)
    await cache.set("k", {"v": 1}, ttl_seconds=60)  # must not raise


@pytest.mark.parametrize("url", ["none", "disabled", ""])
async def test_disabled_url_ping_returns_false(url: str):
    cache = JsonCache(url)
    assert await cache.ping() is False
