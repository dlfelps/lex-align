"""Tests for ``lex-align-server check-config``.

Each individual check returns a CheckResult with status OK | WARN | FAIL.
The tests cover the cases that single-team operators actually hit:
unset registry, missing/invalid YAML, unwritable parent dirs, and the
proposer auto-detection (which now refuses to silently escalate to the
github backend).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from lex_align_server.check_config import (
    FAIL,
    OK,
    WARN,
    check_audit_path,
    check_auth,
    check_proposer,
    check_registry_path,
    check_registry_yaml,
)
from lex_align_server.config import Settings


# ── REGISTRY_PATH ────────────────────────────────────────────────────────


def test_registry_path_unset_fails():
    settings = Settings(registry_path=None)
    r = check_registry_path(settings)
    assert r.status == FAIL
    assert "REGISTRY_PATH" in r.label
    assert "unset" in r.detail.lower()


def test_registry_path_existing_file_passes(tmp_path):
    path = tmp_path / "registry.yml"
    path.write_text("version: '1'\nglobal_policies: {}\npackages: {}\n")
    settings = Settings(registry_path=path)
    r = check_registry_path(settings)
    assert r.status == OK
    assert str(path) in r.detail


def test_registry_path_missing_file_with_writable_parent_warns(tmp_path):
    settings = Settings(registry_path=tmp_path / "registry.yml")
    r = check_registry_path(settings)
    assert r.status == WARN
    assert "does not exist" in r.detail


def test_registry_path_missing_parent_fails(tmp_path):
    settings = Settings(registry_path=tmp_path / "ghost-dir" / "registry.yml")
    r = check_registry_path(settings)
    assert r.status == FAIL


def test_registry_path_unwritable_parent_fails(tmp_path):
    parent = tmp_path / "ro"
    parent.mkdir()
    parent.chmod(0o555)
    try:
        settings = Settings(registry_path=parent / "registry.yml")
        r = check_registry_path(settings)
        # Skip on systems where root can write through the read-only mode.
        if os.geteuid() == 0:
            pytest.skip("running as root — file mode not enforced")
        assert r.status == FAIL
    finally:
        parent.chmod(0o755)


# ── registry YAML ───────────────────────────────────────────────────────


def test_registry_yaml_unparseable_fails(tmp_path):
    path = tmp_path / "registry.yml"
    path.write_text(":\n  : :")  # invalid YAML
    settings = Settings(registry_path=path)
    r = check_registry_yaml(settings)
    assert r.status == FAIL
    assert "did not parse" in r.detail or "validation" in r.detail.lower()


def test_registry_yaml_invalid_schema_fails(tmp_path):
    path = tmp_path / "registry.yml"
    path.write_text(yaml.safe_dump({
        "version": "1",
        "global_policies": {},
        "packages": {"bad": {"status": "not-a-real-status"}},
    }))
    settings = Settings(registry_path=path)
    r = check_registry_yaml(settings)
    assert r.status == FAIL


def test_registry_yaml_valid_passes(tmp_path):
    path = tmp_path / "registry.yml"
    path.write_text(yaml.safe_dump({
        "version": "1",
        "global_policies": {"auto_approve_licenses": ["MIT"]},
        "packages": {
            "redis": {"status": "preferred"},
            "leftpad": {"status": "banned", "reason": "tiny dep"},
        },
    }))
    settings = Settings(registry_path=path)
    r = check_registry_yaml(settings)
    assert r.status == OK
    assert "2 package rules" in r.detail


def test_registry_yaml_missing_file_passes(tmp_path):
    """When the file doesn't exist yet, the YAML check stays quiet — the
    REGISTRY_PATH check is what surfaces the missing-file warning."""
    settings = Settings(registry_path=tmp_path / "registry.yml")
    r = check_registry_yaml(settings)
    assert r.status == OK


# ── audit DB ────────────────────────────────────────────────────────────


def test_audit_db_writable_parent_warns_when_missing(tmp_path):
    settings = Settings(database_path=tmp_path / "audit.sqlite")
    r = check_audit_path(settings)
    assert r.status == WARN
    assert "will be created" in r.detail


def test_audit_db_missing_parent_fails(tmp_path):
    settings = Settings(database_path=tmp_path / "ghost" / "audit.sqlite")
    r = check_audit_path(settings)
    assert r.status == FAIL


def test_audit_db_existing_file_passes(tmp_path):
    db_path = tmp_path / "audit.sqlite"
    db_path.touch()
    settings = Settings(database_path=db_path)
    r = check_audit_path(settings)
    assert r.status == OK


# ── proposer ────────────────────────────────────────────────────────────


def test_proposer_local_file_when_path_set(tmp_path):
    path = tmp_path / "registry.yml"
    path.write_text("version: '1'\nglobal_policies: {}\npackages: {}\n")
    settings = Settings(registry_path=path)
    r = check_proposer(settings)
    assert r.status == OK
    assert "local_file" in r.detail
    assert "recommended" in r.detail.lower()


def test_proposer_log_only_when_nothing_set():
    settings = Settings(registry_path=None)
    r = check_proposer(settings)
    assert r.status == WARN
    assert "log_only" in r.detail


def test_proposer_github_when_repo_url_set_warns(tmp_path):
    """Even when explicitly configured for github, surface a warning so
    single-team operators notice they're on the heavier path. The check
    is informational, not blocking."""
    settings = Settings(
        registry_repo_url="https://github.com/acme/policy",
        registry_repo_token="ghp_test",
        registry_path=tmp_path / "registry.yml",
    )
    r = check_proposer(settings)
    assert r.status == WARN
    assert "github" in r.detail
    assert "REGISTRY_REPO_TOKEN" in r.detail


def test_proposer_local_git_detected_via_dot_git_marker(tmp_path):
    """check-config does its own git-tree probe (without spawning git)
    so it works on machines where git isn't installed."""
    (tmp_path / ".git").mkdir()
    settings = Settings(registry_path=tmp_path / "registry.yml")
    r = check_proposer(settings)
    assert r.status == OK
    assert "local_git" in r.detail


# ── auth ────────────────────────────────────────────────────────────────


def test_auth_anonymous_loopback_passes():
    settings = Settings(auth_enabled=False, bind_host="127.0.0.1")
    r = check_auth(settings)
    assert r.status == OK


def test_auth_anonymous_external_bind_warns():
    settings = Settings(auth_enabled=False, bind_host="0.0.0.0")
    r = check_auth(settings)
    assert r.status == WARN
    assert "0.0.0.0" in r.detail


def test_auth_enabled_passes():
    settings = Settings(auth_enabled=True, auth_backend="header")
    r = check_auth(settings)
    assert r.status == OK
    assert "header" in r.detail
