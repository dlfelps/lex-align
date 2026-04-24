"""Tests for the lex-align hook entry points."""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from lex_align.hooks import (
    HookResult,
    handle_post_tool_use,
    handle_pre_tool_use,
    handle_session_end,
    handle_session_start,
)
from lex_align.models import Confidence, Decision, Provenance, Scope, Status
from lex_align.registry import save_config
from lex_align.session import SessionState, get_current_session_id
from lex_align.store import DecisionStore


@pytest.fixture
def project_with_pyproject(tmp_project: Path) -> Path:
    (tmp_project / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.1.0'\ndependencies = [\n]\n"
    )
    return tmp_project


def test_handle_session_start_sets_current_session(project_with_pyproject: Path):
    brief = handle_session_start({"session_id": "abc"}, project_with_pyproject)
    assert isinstance(brief, str)
    sessions_dir = project_with_pyproject / ".lex-align" / "sessions"
    assert get_current_session_id(sessions_dir) == "abc"


def test_handle_session_start_without_registry_warns(project_with_pyproject: Path):
    brief = handle_session_start({"session_id": "abc"}, project_with_pyproject)
    assert "not configured" in brief


def test_handle_session_start_with_registry_shows_metadata(
    project_with_pyproject: Path, sample_registry_file: Path
):
    save_config(project_with_pyproject, {"registry_file": str(sample_registry_file)})
    brief = handle_session_start({"session_id": "abc"}, project_with_pyproject)
    assert "ENTERPRISE REGISTRY: v1.2" in brief


def test_handle_session_start_reconciles_existing_deps(project_with_pyproject: Path):
    (project_with_pyproject / "pyproject.toml").write_text(
        "[project]\ndependencies = ['redis>=5.0']\n"
    )
    handle_session_start({"session_id": "abc"}, project_with_pyproject)
    store = DecisionStore(project_with_pyproject / ".lex-align" / "decisions")
    decisions = store.load_all()
    redis_entries = [d for d in decisions if "redis" in d.scope.tags]
    assert redis_entries
    for d in redis_entries:
        assert d.provenance is Provenance.RECONCILIATION


def test_handle_session_start_surfaces_pending_promotions(project_with_pyproject: Path):
    """Observed entries must appear in the brief as actionable tasks with a
    runnable command template — no copy-paste from a separate compliance run."""
    (project_with_pyproject / "pyproject.toml").write_text(
        "[project]\ndependencies = ['redis>=5.0']\n"
    )
    brief = handle_session_start({"session_id": "abc"}, project_with_pyproject)
    assert "PENDING PROMOTIONS" in brief
    assert "lex-align promote" in brief
    assert "--yes" in brief
    assert "redis" in brief


def test_handle_session_start_no_observed_no_pending_section(project_with_pyproject: Path):
    brief = handle_session_start({"session_id": "abc"}, project_with_pyproject)
    assert "PENDING PROMOTIONS" not in brief


def test_handle_pre_tool_use_ignores_unrelated_paths(project_with_pyproject: Path):
    event = {
        "tool_name": "Edit",
        "tool_input": {"path": "src/app.py", "old_string": "x", "new_string": "y"},
        "session_id": "s",
    }
    result = handle_pre_tool_use(event, project_with_pyproject)
    # No observed entries → no code-edit prompt → None.
    assert result is None


def test_handle_pre_tool_use_surfaces_observed_import(project_with_pyproject: Path):
    store = DecisionStore(project_with_pyproject / ".lex-align" / "decisions")
    observed = Decision(
        id="ADR-0001",
        title="Uses redis",
        status=Status.OBSERVED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        provenance=Provenance.RECONCILIATION,
    )
    store.save(observed)
    event = {
        "tool_name": "Write",
        "tool_input": {
            "path": "src/foo.py",
            "content": "import redis\n",
        },
        "session_id": "s",
    }
    result = handle_pre_tool_use(event, project_with_pyproject)
    assert isinstance(result, HookResult)
    assert result.decision == "allow"
    assert "observed dependency" in result.message


def test_handle_post_tool_use_runs_reconciliation(project_with_pyproject: Path):
    (project_with_pyproject / "pyproject.toml").write_text(
        "[project]\ndependencies = ['sqlalchemy>=2.0']\n"
    )
    event = {
        "tool_name": "Edit",
        "tool_input": {"path": "pyproject.toml"},
        "session_id": "s",
    }
    result = handle_post_tool_use(event, project_with_pyproject)
    assert isinstance(result, HookResult)
    assert "Reconciliation" in result.message
    store = DecisionStore(project_with_pyproject / ".lex-align" / "decisions")
    assert any("sqlalchemy" in d.scope.tags for d in store.load_all())


def test_handle_session_end_always_returns_none(project_with_pyproject: Path):
    # Hard enforcement replaces the final-capture reminder; session_end only
    # cleans up ephemeral per-session state.
    state = SessionState(project_with_pyproject / ".lex-align" / "sessions", "s")
    state.record_dep_change(["redis"])
    result = handle_session_end({"session_id": "s"}, project_with_pyproject)
    assert result is None
