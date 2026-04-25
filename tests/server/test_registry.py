"""Unit tests for the registry module — pure functions, no I/O."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lex_align_server.registry import (
    Action,
    GlobalPolicies,
    PackageRule,
    PackageStatus,
    Registry,
    load_registry,
    normalize_name,
)


def _registry(packages: dict | None = None, **policies) -> Registry:
    return Registry.from_dict({
        "version": "1",
        "global_policies": policies,
        "packages": packages or {},
    })


def test_normalize_name_handles_dashes_dots_and_case():
    assert normalize_name("Pyyaml") == "pyyaml"
    assert normalize_name("python-frontmatter") == "python_frontmatter"
    assert normalize_name(" zope.event ") == "zope_event"


def test_lookup_unknown_package_returns_unknown_action():
    reg = _registry()
    assert reg.lookup("nonexistent").action is Action.UNKNOWN


@pytest.mark.parametrize("status,expected_action", [
    ("preferred", Action.ALLOW),
    ("approved", Action.REQUIRE_PROPOSE),
    ("deprecated", Action.BLOCK),
    ("banned", Action.BLOCK),
])
def test_status_to_action_mapping(status, expected_action):
    reg = _registry({"x": {"status": status, "replacement": "y" if status == "deprecated" else None}})
    verdict = reg.lookup("x")
    assert verdict.action is expected_action
    assert verdict.status is PackageStatus(status)


def test_version_constrained_blocks_violating_version():
    reg = _registry({
        "cryptography": {"status": "version-constrained", "min_version": "42.0.0"},
    })
    assert reg.lookup("cryptography", "41.0.0").action is Action.BLOCK
    assert reg.lookup("cryptography", "42.0.0").action is Action.ALLOW
    # No version → allow (we cannot judge until pinned).
    assert reg.lookup("cryptography").action is Action.ALLOW


def test_global_policies_default_cve_threshold_is_09():
    gp = GlobalPolicies.from_dict({})
    assert gp.cve_threshold == pytest.approx(0.9)


def test_global_policies_cve_blocks():
    gp = GlobalPolicies.from_dict({"cve_threshold": 0.7})
    assert gp.cve_blocks(7.5)
    assert gp.cve_blocks(7.0)
    assert not gp.cve_blocks(6.99)
    assert not gp.cve_blocks(None)


def test_global_policies_license_classification():
    gp = GlobalPolicies.from_dict({
        "auto_approve_licenses": ["MIT", "Apache-2.0"],
        "hard_ban_licenses": ["GPL-3.0"],
    })
    assert gp.is_auto_approved("apache-2.0")  # case-insensitive
    assert gp.is_blocked("GPL-3.0")
    assert not gp.is_blocked("MIT")


def test_load_registry_returns_none_when_path_missing(tmp_path: Path):
    assert load_registry(tmp_path / "missing.json") is None
    assert load_registry(None) is None


def test_load_registry_reads_compiled_json(tmp_path: Path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({
        "version": "1.2",
        "global_policies": {
            "auto_approve_licenses": ["MIT"],
            "hard_ban_licenses": ["GPL-3.0"],
            "cve_threshold": 0.8,
        },
        "packages": {"httpx": {"status": "preferred", "reason": "standard"}},
    }))
    reg = load_registry(path)
    assert reg is not None
    assert reg.version == "1.2"
    assert "httpx" in reg.packages
    assert reg.global_policies.cve_threshold == pytest.approx(0.8)
