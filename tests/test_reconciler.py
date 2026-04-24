"""Tests for pyproject.toml reconciliation."""
from __future__ import annotations

import pytest

from lex_align.models import Provenance, Status
from lex_align.reconciler import (
    _normalize_name,
    apply_edit,
    diff_deps,
    find_uncovered,
    get_runtime_deps,
    reconcile,
)
from lex_align.store import DecisionStore


def test_get_runtime_deps(pyproject_toml):
    deps = get_runtime_deps(pyproject_toml)
    assert "fastapi" in deps
    assert "redis" in deps
    assert "sqlalchemy" in deps


def test_get_runtime_deps_missing_file(tmp_path):
    deps = get_runtime_deps(tmp_path / "pyproject.toml")
    assert deps == set()


def test_get_runtime_deps_no_dependencies_key(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text("[project]\nname = 'myapp'\nversion = '0.1.0'\n")
    deps = get_runtime_deps(path)
    assert deps == set()


def test_normalize_name_strips_version():
    assert _normalize_name("redis>=5.0") == "redis"
    assert _normalize_name("fastapi>=0.100,<1.0") == "fastapi"
    assert _normalize_name("sqlalchemy==2.0.0") == "sqlalchemy"


def test_normalize_name_replaces_dashes():
    assert _normalize_name("python-dotenv") == "python_dotenv"


def test_normalize_name_extras():
    assert _normalize_name("pydantic[email]>=2.0") == "pydantic"


def test_find_uncovered_all_uncovered(store: DecisionStore):
    uncovered = find_uncovered({"redis", "fastapi"}, store)
    assert uncovered == {"redis", "fastapi"}


def test_find_uncovered_some_covered(store: DecisionStore, sample_decision):
    # sample_decision has tag "testing" and title "Use Pytest for testing"
    store.save(sample_decision)
    uncovered = find_uncovered({"pytest", "redis"}, store)
    assert "pytest" not in uncovered
    assert "redis" in uncovered


def test_reconcile_creates_observed_entries(store: DecisionStore, pyproject_toml):
    created = reconcile(pyproject_toml, store)
    assert len(created) > 0
    assert "fastapi" in created or "redis" in created
    decisions = store.load_all()
    assert all(d.status == Status.OBSERVED for d in decisions)
    assert all(d.provenance == Provenance.RECONCILIATION for d in decisions)


def test_reconcile_idempotent(store: DecisionStore, pyproject_toml):
    reconcile(pyproject_toml, store)
    first_count = len(store.load_all())
    reconcile(pyproject_toml, store)
    second_count = len(store.load_all())
    assert first_count == second_count


def test_reconcile_seed_via(store: DecisionStore, pyproject_toml):
    created = reconcile(pyproject_toml, store, provenance=Provenance.RECONCILIATION)
    decisions = store.load_all()
    assert all(d.provenance == Provenance.RECONCILIATION for d in decisions)


def test_diff_deps_added():
    old = "[project]\ndependencies = ['fastapi>=0.100']\n"
    new = "[project]\ndependencies = ['fastapi>=0.100', 'redis>=5.0']\n"
    added, removed = diff_deps(old, new)
    assert "redis" in added
    assert removed == set()


def test_diff_deps_removed():
    old = "[project]\ndependencies = ['fastapi>=0.100', 'redis>=5.0']\n"
    new = "[project]\ndependencies = ['fastapi>=0.100']\n"
    added, removed = diff_deps(old, new)
    assert added == set()
    assert "redis" in removed


def test_diff_deps_no_change():
    content = "[project]\ndependencies = ['fastapi>=0.100']\n"
    added, removed = diff_deps(content, content)
    assert added == set()
    assert removed == set()


def test_apply_edit_write():
    result = apply_edit("old content", "Write", {"content": "new content"})
    assert result == "new content"


def test_apply_edit_edit():
    result = apply_edit(
        "dependencies = ['redis>=4.0']",
        "Edit",
        {"old_string": "redis>=4.0", "new_string": "redis>=5.0"},
    )
    assert "redis>=5.0" in result


def test_apply_edit_multiedit():
    result = apply_edit(
        "redis>=4.0 fastapi>=0.100",
        "MultiEdit",
        {
            "edits": [
                {"old_string": "redis>=4.0", "new_string": "redis>=5.0"},
                {"old_string": "fastapi>=0.100", "new_string": "fastapi>=0.110"},
            ]
        },
    )
    assert "redis>=5.0" in result
    assert "fastapi>=0.110" in result


def test_apply_edit_unknown_tool():
    result = apply_edit("original", "UnknownTool", {})
    assert result == "original"
