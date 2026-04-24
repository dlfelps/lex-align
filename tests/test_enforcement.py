"""End-to-end tests for the PreToolUse enforcement hook.

Each test simulates an agent Edit on pyproject.toml with a different package,
runs handle_pre_tool_use, and asserts the resulting HookResult.decision and
any side-effect ADR that was auto-written.
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from lex_align.hooks import HookResult, handle_pre_tool_use
from lex_align.licenses import LicenseCache, LicenseInfo
from lex_align.models import Provenance, Status
from lex_align.registry import save_config
from lex_align.store import DecisionStore


@pytest.fixture
def project_with_registry(tmp_project: Path, sample_registry_file: Path) -> Path:
    save_config(tmp_project, {"registry_file": str(sample_registry_file)})
    (tmp_project / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.1.0'\ndependencies = [\n]\n"
    )
    return tmp_project


def _edit_event(package_spec: str) -> dict:
    old = "dependencies = [\n]"
    new = f'dependencies = [\n    "{package_spec}",\n]'
    return {
        "tool_name": "Edit",
        "tool_input": {"path": "pyproject.toml", "old_string": old, "new_string": new},
        "session_id": "test-session",
    }


def test_pre_preferred_package_allows_and_writes_adr(project_with_registry: Path):
    event = _edit_event("httpx>=0.28.0")
    result = handle_pre_tool_use(event, project_with_registry)
    assert isinstance(result, HookResult)
    assert result.decision == "allow"
    store = DecisionStore(project_with_registry / ".lex-align" / "decisions")
    decisions = store.load_all()
    assert len(decisions) == 1
    d = decisions[0]
    assert d.status is Status.ACCEPTED
    assert d.provenance is Provenance.REGISTRY_PREFERRED
    assert "httpx" in d.scope.tags
    assert d.registry_version == "1.2"


def test_pre_deprecated_package_blocks(project_with_registry: Path):
    event = _edit_event("requests>=2.30")
    result = handle_pre_tool_use(event, project_with_registry)
    assert result.decision == "block"
    assert "use instead: httpx" in result.message
    store = DecisionStore(project_with_registry / ".lex-align" / "decisions")
    decisions = store.load_all()
    # A rejected audit-trail ADR should have been written.
    assert any(
        d.status is Status.REJECTED and d.provenance is Provenance.REGISTRY_BLOCKED
        for d in decisions
    )


def test_pre_banned_package_blocks(project_with_registry: Path):
    event = _edit_event("pyqt5")
    result = handle_pre_tool_use(event, project_with_registry)
    assert result.decision == "block"
    assert "banned" in result.message


def test_pre_version_constrained_violation_blocks(project_with_registry: Path):
    event = _edit_event("cryptography==41.0.0")
    result = handle_pre_tool_use(event, project_with_registry)
    assert result.decision == "block"
    assert ">=42.0.0" in result.message


def test_pre_version_constrained_satisfied_allows(project_with_registry: Path):
    event = _edit_event("cryptography==42.1.0")
    result = handle_pre_tool_use(event, project_with_registry)
    assert result.decision == "allow"
    store = DecisionStore(project_with_registry / ".lex-align" / "decisions")
    d = store.load_all()[-1]
    assert d.version_constraint == ">=42.0.0"


def test_pre_approved_package_allows_with_propose_hint(project_with_registry: Path):
    event = _edit_event("flask>=3.0")
    result = handle_pre_tool_use(event, project_with_registry)
    assert result.decision == "allow"
    assert "`lex-align propose" in result.message
    # No auto-ADR is written for approved packages — agent must propose.
    store = DecisionStore(project_with_registry / ".lex-align" / "decisions")
    assert store.load_all() == []


def test_pre_unknown_package_with_permissive_license_allows(
    project_with_registry: Path, monkeypatch
):
    # Pre-populate the license cache so no network call happens.
    cache = LicenseCache(project_with_registry / ".lex-align" / "license-cache.json")
    cache.put(
        "someobscurelib", None,
        LicenseInfo(
            license_raw="MIT License",
            license_normalized="MIT",
            fetched_at=datetime.date.today(),
            source="pypi",
        ),
    )
    event = _edit_event("someobscurelib>=1.0")
    result = handle_pre_tool_use(event, project_with_registry)
    assert result.decision == "allow"
    store = DecisionStore(project_with_registry / ".lex-align" / "decisions")
    d = store.load_all()[-1]
    assert d.provenance is Provenance.LICENSE_AUTO_APPROVE
    assert d.license == "MIT"


def test_pre_unknown_package_with_gpl_blocks(project_with_registry: Path):
    cache = LicenseCache(project_with_registry / ".lex-align" / "license-cache.json")
    cache.put(
        "somegpllib", None,
        LicenseInfo(
            license_raw="GPL-3.0-or-later",
            license_normalized="GPL-3.0",
            fetched_at=datetime.date.today(),
            source="pypi",
        ),
    )
    event = _edit_event("somegpllib>=1.0")
    result = handle_pre_tool_use(event, project_with_registry)
    assert result.decision == "block"
    assert "GPL-3.0" in result.message


def test_pre_unknown_package_with_lgpl_blocks(project_with_registry: Path):
    cache = LicenseCache(project_with_registry / ".lex-align" / "license-cache.json")
    cache.put(
        "somelgpllib", None,
        LicenseInfo(
            license_raw="LGPLv3",
            license_normalized="LGPL-3.0",
            fetched_at=datetime.date.today(),
            source="pypi",
        ),
    )
    event = _edit_event("somelgpllib>=1.0")
    result = handle_pre_tool_use(event, project_with_registry)
    assert result.decision == "block"
    assert "LGPL-3.0" in result.message


def test_pre_no_registry_passes_through_with_warning(tmp_project: Path):
    # No registry configured; hook should allow and note the absence.
    (tmp_project / "pyproject.toml").write_text(
        "[project]\ndependencies = [\n]\n"
    )
    event = _edit_event("anything>=1.0")
    result = handle_pre_tool_use(event, tmp_project)
    assert result.decision == "allow"
    assert "No enterprise registry" in result.message


def test_pre_no_dep_change_returns_none(project_with_registry: Path):
    # Editing some whitespace change that doesn't modify dependencies.
    event = {
        "tool_name": "Edit",
        "tool_input": {
            "path": "pyproject.toml",
            "old_string": "name = 'demo'",
            "new_string": "name = \"demo\"",
        },
        "session_id": "test",
    }
    result = handle_pre_tool_use(event, project_with_registry)
    assert result is None
