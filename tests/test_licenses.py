"""Tests for license normalization, cache, and policy evaluation."""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from lex_align.licenses import (
    LicenseCache,
    LicenseInfo,
    evaluate_license,
    normalize_license,
    resolve_license,
)
from lex_align.registry import Action, GlobalPolicies


@pytest.mark.parametrize("raw,expected", [
    (None, "UNKNOWN"),
    ("", "UNKNOWN"),
    ("MIT", "MIT"),
    ("Apache-2.0", "Apache-2.0"),
    ("Apache Software License", "Apache-2.0"),
    ("BSD 3-Clause License", "BSD-3-Clause"),
    ("BSD-3-Clause", "BSD-3-Clause"),
    ("BSD-2-Clause", "BSD-2-Clause"),
    ("GPL v3", "GPL-3.0"),
    ("GNU General Public License v3", "GPL-3.0"),
    ("AGPLv3", "AGPL-3.0"),
    ("LGPLv3", "LGPL-3.0"),
    ("LGPL v2.1", "LGPL-2.1"),
    ("MPL-2.0", "MPL-2.0"),
    ("Proprietary", "Proprietary"),
    ("something unexpected", "UNKNOWN"),
])
def test_normalize_license(raw, expected):
    assert normalize_license(raw) == expected


def test_evaluate_license_auto_approve():
    pol = GlobalPolicies(auto_approve_licenses=["MIT", "Apache-2.0"])
    v = evaluate_license("MIT", pol)
    assert v.action is Action.ALLOW
    assert v.license == "MIT"


def test_evaluate_license_hard_ban():
    pol = GlobalPolicies(hard_ban_licenses=["GPL-3.0", "AGPL-3.0"])
    v = evaluate_license("GPL-3.0", pol)
    assert v.action is Action.BLOCK


def test_evaluate_license_human_review_is_blocked_for_now():
    pol = GlobalPolicies(require_human_review_licenses=["LGPL-3.0"])
    v = evaluate_license("LGPL-3.0", pol)
    # Until the review flow is built LGPL hard-blocks.
    assert v.action is Action.BLOCK


def test_evaluate_license_unknown_defaults_to_block():
    pol = GlobalPolicies(unknown_license_policy="block")
    v = evaluate_license("UNKNOWN", pol)
    assert v.action is Action.BLOCK


def test_evaluate_license_unknown_can_be_allowed_via_policy():
    pol = GlobalPolicies(unknown_license_policy="allow")
    v = evaluate_license("UNKNOWN", pol)
    assert v.action is Action.ALLOW


def test_evaluate_license_non_listed_blocks_even_if_permissive_looking():
    pol = GlobalPolicies(auto_approve_licenses=["MIT"])
    # MPL-2.0 not on allow list, not explicitly banned → conservatively block.
    v = evaluate_license("MPL-2.0", pol)
    assert v.action is Action.BLOCK


def test_license_cache_put_get_roundtrip(tmp_path: Path):
    cache = LicenseCache(tmp_path / "license-cache.json")
    info = LicenseInfo(
        license_raw="MIT License",
        license_normalized="MIT",
        fetched_at=datetime.date(2026, 4, 1),
        source="pypi",
    )
    cache.put("httpx", "0.28.1", info)
    loaded = cache.get("httpx", "0.28.1")
    assert loaded is not None
    assert loaded.license_raw == "MIT License"
    assert loaded.license_normalized == "MIT"
    assert loaded.fetched_at == datetime.date(2026, 4, 1)


def test_license_cache_miss_without_version_distinct_from_pinned(tmp_path: Path):
    cache = LicenseCache(tmp_path / "c.json")
    info = LicenseInfo(
        license_raw="MIT",
        license_normalized="MIT",
        fetched_at=datetime.date.today(),
        source="pypi",
    )
    cache.put("httpx", None, info)
    # Pinned version should be a distinct cache key.
    assert cache.get("httpx", "0.28.1") is None
    assert cache.get("httpx") is not None


def test_license_cache_persists_across_instances(tmp_path: Path):
    path = tmp_path / "c.json"
    cache1 = LicenseCache(path)
    cache1.put(
        "foo", None,
        LicenseInfo("MIT", "MIT", datetime.date(2026, 1, 1), "pypi"),
    )
    cache2 = LicenseCache(path)
    assert cache2.get("foo") is not None


def test_resolve_license_uses_cache(tmp_path: Path, monkeypatch):
    cache = LicenseCache(tmp_path / "c.json")
    # Pre-populate cache; fetch must not be called.
    cache.put(
        "httpx", "0.28.1",
        LicenseInfo("BSD-3-Clause", "BSD-3-Clause", datetime.date(2026, 1, 1), "pypi"),
    )

    def boom(*args, **kwargs):
        raise AssertionError("fetch should not be called when cache hits")

    monkeypatch.setattr("lex_align.licenses.fetch_license_from_pypi", boom)
    pol = GlobalPolicies(auto_approve_licenses=["BSD-3-Clause"])
    info, verdict = resolve_license("httpx", "0.28.1", cache, pol)
    assert info.license_normalized == "BSD-3-Clause"
    assert verdict.action is Action.ALLOW


def test_resolve_license_writes_cache_on_miss(tmp_path: Path, monkeypatch):
    cache = LicenseCache(tmp_path / "c.json")
    monkeypatch.setattr(
        "lex_align.licenses.fetch_license_from_pypi", lambda *a, **kw: "MIT License"
    )
    pol = GlobalPolicies(auto_approve_licenses=["MIT"])
    info, verdict = resolve_license("foo", None, cache, pol)
    assert info.license_normalized == "MIT"
    assert verdict.action is Action.ALLOW
    # Subsequent call hits cache.
    info2, _ = resolve_license("foo", None, cache, pol)
    assert info2.license_normalized == "MIT"


def test_resolve_license_block_on_fetch_failure(tmp_path: Path, monkeypatch):
    cache = LicenseCache(tmp_path / "c.json")
    monkeypatch.setattr(
        "lex_align.licenses.fetch_license_from_pypi", lambda *a, **kw: None
    )
    pol = GlobalPolicies(unknown_license_policy="block")
    info, verdict = resolve_license("foo", None, cache, pol)
    assert info.license_normalized == "UNKNOWN"
    assert verdict.action is Action.BLOCK
