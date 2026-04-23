"""Tests for hook handlers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lex_align.hooks import (
    _extract_imports,
    handle_post_tool_use,
    handle_pre_tool_use,
    handle_session_end,
    handle_session_start,
)
from lex_align.models import (
    Confidence,
    Decision,
    ObservedVia,
    Scope,
    Status,
)
from lex_align.session import SessionState, set_current_session_id
from lex_align.store import DecisionStore


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / ".lex-align" / "decisions").mkdir(parents=True)
    (tmp_path / ".lex-align" / "sessions").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def store(project_root: Path) -> DecisionStore:
    return DecisionStore(project_root / ".lex-align" / "decisions")


def test_session_start_generates_brief(project_root: Path, store: DecisionStore, sample_decision):
    store.save(sample_decision)
    event = {"session_id": "sess-001"}
    output = handle_session_start(event, project_root)
    assert "ACCEPTED" in output
    assert "ADR-0001" in output
    assert "Use Pytest" in output


def test_session_start_runs_reconciliation(project_root: Path):
    pyproject = project_root / "pyproject.toml"
    pyproject.write_text('[project]\ndependencies = ["fastapi>=0.100"]\n')
    event = {"session_id": "sess-001"}
    output = handle_session_start(event, project_root)
    assert "OBSERVED" in output
    assert "fastapi" in output.lower()


def test_session_start_sets_current_session(project_root: Path):
    event = {"session_id": "sess-abc"}
    handle_session_start(event, project_root)
    from lex_align.session import get_current_session_id
    sid = get_current_session_id(project_root / ".lex-align" / "sessions")
    assert sid == "sess-abc"


def test_brief_contains_plan_hint(project_root: Path):
    event = {"session_id": "sess-001"}
    output = handle_session_start(event, project_root)
    assert 'lex-align plan' in output
    assert 'considered' not in output


def test_brief_separates_accepted_observed(project_root: Path, store: DecisionStore):
    import datetime
    accepted = Decision(
        id="ADR-0001",
        title="Use Pytest",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.HIGH,
        scope=Scope(tags=["testing"]),
    )
    observed = Decision(
        id="ADR-0002",
        title="Uses redis",
        status=Status.OBSERVED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        observed_via=ObservedVia.SEED,
    )
    store.save(accepted)
    store.save(observed)
    event = {"session_id": "sess-001"}
    output = handle_session_start(event, project_root)
    assert "ACCEPTED" in output
    assert "OBSERVED" in output


def test_pre_tool_use_pyproject_dep_added(project_root: Path, store: DecisionStore):
    pyproject = project_root / "pyproject.toml"
    pyproject.write_text('[project]\ndependencies = ["fastapi>=0.100"]\n')
    event = {
        "session_id": "sess-001",
        "tool_name": "Edit",
        "tool_input": {
            "path": "pyproject.toml",
            "old_string": '"fastapi>=0.100"',
            "new_string": '"fastapi>=0.100", "redis>=5.0"',
        },
    }
    output = handle_pre_tool_use(event, project_root)
    assert output is not None
    assert "redis" in output
    assert "lex-align propose" in output


def test_pre_tool_use_pyproject_no_change(project_root: Path):
    pyproject = project_root / "pyproject.toml"
    pyproject.write_text('[project]\ndependencies = ["fastapi>=0.100"]\n')
    event = {
        "session_id": "sess-001",
        "tool_name": "Edit",
        "tool_input": {
            "path": "pyproject.toml",
            "old_string": "name = 'myapp'",
            "new_string": "name = 'my-app'",
        },
    }
    output = handle_pre_tool_use(event, project_root)
    assert output is None


def test_pre_tool_use_pyproject_relevant_decisions(project_root: Path, store: DecisionStore):
    import datetime
    pyproject = project_root / "pyproject.toml"
    pyproject.write_text('[project]\ndependencies = []\n')
    d = Decision(
        id="ADR-0001",
        title="Uses redis",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
    )
    store.save(d)
    event = {
        "session_id": "sess-001",
        "tool_name": "Edit",
        "tool_input": {
            "path": "pyproject.toml",
            "old_string": "dependencies = []",
            "new_string": 'dependencies = ["redis>=5.0"]',
        },
    }
    output = handle_pre_tool_use(event, project_root)
    assert "ADR-0001" in output


def test_pre_tool_use_python_file_observed_import(project_root: Path, store: DecisionStore):
    import datetime
    observed = Decision(
        id="ADR-0001",
        title="Uses redis",
        status=Status.OBSERVED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        observed_via=ObservedVia.SEED,
    )
    store.save(observed)
    event = {
        "session_id": "sess-001",
        "tool_name": "Edit",
        "tool_input": {
            "path": "src/cache.py",
            "old_string": "x = 1",
            "new_string": "import redis\nx = 1",
        },
    }
    output = handle_pre_tool_use(event, project_root)
    assert output is not None
    assert "redis" in output
    assert "lex-align promote" in output


def test_pre_tool_use_observed_fires_once_per_session(project_root: Path, store: DecisionStore):
    import datetime
    observed = Decision(
        id="ADR-0001",
        title="Uses redis",
        status=Status.OBSERVED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        observed_via=ObservedVia.SEED,
    )
    store.save(observed)
    event = {
        "session_id": "sess-001",
        "tool_name": "Edit",
        "tool_input": {
            "path": "src/cache.py",
            "old_string": "x = 1",
            "new_string": "import redis\nx = 1",
        },
    }
    output1 = handle_pre_tool_use(event, project_root)
    output2 = handle_pre_tool_use(event, project_root)
    assert output1 is not None
    assert output2 is None  # Already fired this session


def test_pre_tool_use_accepted_import_no_prompt(project_root: Path, store: DecisionStore):
    import datetime
    accepted = Decision(
        id="ADR-0001",
        title="Uses redis",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
    )
    store.save(accepted)
    event = {
        "session_id": "sess-001",
        "tool_name": "Edit",
        "tool_input": {
            "path": "src/cache.py",
            "old_string": "x = 1",
            "new_string": "import redis\nx = 1",
        },
    }
    output = handle_pre_tool_use(event, project_root)
    assert output is None


def test_post_tool_use_creates_observed(project_root: Path):
    pyproject = project_root / "pyproject.toml"
    pyproject.write_text('[project]\ndependencies = ["redis>=5.0"]\n')
    event = {
        "session_id": "sess-001",
        "tool_name": "Edit",
        "tool_input": {"path": "pyproject.toml"},
    }
    output = handle_post_tool_use(event, project_root)
    store = DecisionStore(project_root / ".lex-align" / "decisions")
    decisions = store.load_all()
    assert len(decisions) > 0
    assert decisions[0].status == Status.OBSERVED


def test_post_tool_use_shows_reminder_when_no_propose(project_root: Path):
    pyproject = project_root / "pyproject.toml"
    pyproject.write_text('[project]\ndependencies = ["redis>=5.0"]\n')
    sessions_dir = project_root / ".lex-align" / "sessions"
    state = SessionState(sessions_dir, "sess-001")
    state.record_dep_change(["redis"])

    event = {
        "session_id": "sess-001",
        "tool_name": "Edit",
        "tool_input": {"path": "pyproject.toml"},
    }
    output = handle_post_tool_use(event, project_root)
    assert output is not None
    assert "propose" in output.lower() or "promote" in output.lower()


def test_session_end_lists_unresolved(project_root: Path):
    sessions_dir = project_root / ".lex-align" / "sessions"
    state = SessionState(sessions_dir, "sess-001")
    state.record_dep_change(["redis", "celery"])
    set_current_session_id(sessions_dir, "sess-001")

    event = {"session_id": "sess-001"}
    output = handle_session_end(event, project_root)
    assert output is not None
    assert "redis" in output
    assert "celery" in output


def test_session_end_no_unresolved(project_root: Path):
    sessions_dir = project_root / ".lex-align" / "sessions"
    set_current_session_id(sessions_dir, "sess-001")
    event = {"session_id": "sess-001"}
    output = handle_session_end(event, project_root)
    assert output is None


def test_session_end_clears_current_session(project_root: Path):
    from lex_align.session import get_current_session_id
    sessions_dir = project_root / ".lex-align" / "sessions"
    set_current_session_id(sessions_dir, "sess-001")
    handle_session_end({"session_id": "sess-001"}, project_root)
    assert get_current_session_id(sessions_dir) is None


def test_extract_imports():
    code = """\
import redis
import os
from fastapi import FastAPI
from sqlalchemy.orm import Session
"""
    imports = _extract_imports(code)
    assert "redis" in imports
    assert "os" in imports
    assert "fastapi" in imports
    assert "sqlalchemy" in imports


def test_extract_imports_invalid_syntax():
    imports = _extract_imports("this is not valid python @@@")
    assert imports == set()
