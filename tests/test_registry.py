"""Tests for the enterprise registry data layer."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lex_align.registry import (
    Action,
    GlobalPolicies,
    PackageRule,
    PackageStatus,
    Registry,
    load_config,
    load_registry,
    resolve_registry_path,
    save_config,
)


def test_registry_load_parses_known_fields(sample_registry_file: Path):
    reg = Registry.load(sample_registry_file)
    assert reg.version == "1.2"
    assert "httpx" in reg.packages
    assert "requests" in reg.packages
    assert reg.packages["httpx"].status is PackageStatus.PREFERRED
    assert reg.packages["requests"].status is PackageStatus.DEPRECATED
    assert reg.packages["requests"].replacement == "httpx"


def test_registry_package_rule_version_constraint():
    rule = PackageRule(status=PackageStatus.VERSION_CONSTRAINED, min_version="1.0.0")
    assert rule.version_constraint_str() == ">=1.0.0"
    rule2 = PackageRule(
        status=PackageStatus.VERSION_CONSTRAINED, min_version="1.0", max_version="3.0"
    )
    assert rule2.version_constraint_str() == ">=1.0,<3.0"
    rule3 = PackageRule(status=PackageStatus.PREFERRED)
    assert rule3.version_constraint_str() is None


def test_registry_lookup_preferred(sample_registry_file: Path):
    reg = Registry.load(sample_registry_file)
    verdict = reg.lookup("httpx")
    assert verdict.action is Action.ALLOW
    assert verdict.status is PackageStatus.PREFERRED


def test_registry_lookup_approved_requires_propose(sample_registry_file: Path):
    reg = Registry.load(sample_registry_file)
    verdict = reg.lookup("flask")
    assert verdict.action is Action.REQUIRE_PROPOSE
    assert verdict.status is PackageStatus.APPROVED


def test_registry_lookup_deprecated_blocks_with_replacement(sample_registry_file: Path):
    reg = Registry.load(sample_registry_file)
    verdict = reg.lookup("requests")
    assert verdict.action is Action.BLOCK
    assert verdict.replacement == "httpx"


def test_registry_lookup_banned_blocks(sample_registry_file: Path):
    reg = Registry.load(sample_registry_file)
    verdict = reg.lookup("pyqt5")
    assert verdict.action is Action.BLOCK


def test_registry_lookup_version_constrained_blocks_older(sample_registry_file: Path):
    reg = Registry.load(sample_registry_file)
    verdict = reg.lookup("cryptography", version="41.0.0")
    assert verdict.action is Action.BLOCK
    assert verdict.version_constraint == ">=42.0.0"


def test_registry_lookup_version_constrained_allows_newer(sample_registry_file: Path):
    reg = Registry.load(sample_registry_file)
    verdict = reg.lookup("cryptography", version="42.1.0")
    assert verdict.action is Action.ALLOW
    assert verdict.version_constraint == ">=42.0.0"


def test_registry_lookup_unknown_package(sample_registry_file: Path):
    reg = Registry.load(sample_registry_file)
    verdict = reg.lookup("somelib")
    assert verdict.action is Action.UNKNOWN


def test_registry_normalizes_package_name(tmp_path: Path):
    content = {
        "version": "1",
        "global_policies": {},
        "packages": {"python-dotenv": {"status": "preferred"}},
    }
    path = tmp_path / "r.json"
    path.write_text(json.dumps(content))
    reg = Registry.load(path)
    # Lookup by underscore or dash or dotted form should match.
    assert reg.lookup("python-dotenv").action is Action.ALLOW
    assert reg.lookup("python_dotenv").action is Action.ALLOW


def test_global_policies_blocks_includes_human_review():
    gp = GlobalPolicies(
        hard_ban_licenses=["GPL-3.0"],
        require_human_review_licenses=["LGPL-3.0"],
    )
    # Until the review flow is built, human-review licenses are hard-banned.
    assert gp.is_blocked("LGPL-3.0")
    assert gp.is_blocked("GPL-3.0")
    assert not gp.is_blocked("MIT")


def test_global_policies_auto_approve():
    gp = GlobalPolicies(auto_approve_licenses=["MIT", "Apache-2.0"])
    assert gp.is_auto_approved("MIT")
    assert gp.is_auto_approved("apache-2.0")  # case-insensitive
    assert not gp.is_auto_approved("GPL-3.0")


def test_resolve_registry_path_cli_wins(sample_registry_file: Path, tmp_project: Path):
    # Even with config.json and env set, CLI flag overrides.
    save_config(tmp_project, {"registry_file": "ignored.json"})
    resolved = resolve_registry_path(tmp_project, cli_flag=str(sample_registry_file))
    assert resolved == sample_registry_file.resolve()


def test_resolve_registry_path_env_over_config(
    sample_registry_file: Path, tmp_project: Path, monkeypatch
):
    save_config(tmp_project, {"registry_file": "ignored.json"})
    monkeypatch.setenv("LEXALIGN_REGISTRY_FILE", str(sample_registry_file))
    resolved = resolve_registry_path(tmp_project)
    assert resolved == sample_registry_file.resolve()


def test_resolve_registry_path_from_config(sample_registry_file: Path, tmp_project: Path):
    save_config(tmp_project, {"registry_file": str(sample_registry_file)})
    resolved = resolve_registry_path(tmp_project)
    assert resolved == sample_registry_file.resolve()


def test_resolve_registry_path_default_when_absent(tmp_project: Path):
    # No config, no env, no default file present
    assert resolve_registry_path(tmp_project) is None


def test_load_registry_returns_none_when_unconfigured(tmp_project: Path):
    assert load_registry(tmp_project) is None


def test_config_roundtrip(tmp_project: Path):
    save_config(tmp_project, {"registry_file": "some/path.json", "other": 42})
    loaded = load_config(tmp_project)
    assert loaded == {"registry_file": "some/path.json", "other": 42}
