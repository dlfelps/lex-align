"""Unit tests for the pyproject diff helpers."""

from __future__ import annotations

from pathlib import Path

from lex_align_client.pyproject_utils import (
    apply_edit,
    detect_project_name,
    diff_deps,
    extract_pinned_version,
    get_runtime_deps,
    normalize_name,
    parse_deps_from_content,
)


def test_normalize_name_matches_server_normalization():
    assert normalize_name("Pillow") == "pillow"
    assert normalize_name("python-frontmatter") == "python_frontmatter"
    assert normalize_name("redis>=5.0") == "redis"
    assert normalize_name("redis[hiredis]") == "redis"


def test_get_runtime_deps_returns_specs(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["redis>=5.0", "click"]\n'
    )
    deps = get_runtime_deps(pyproject)
    assert deps == {"redis": "redis>=5.0", "click": "click"}


def test_diff_deps_added_and_removed():
    old = '[project]\ndependencies = ["a", "b"]\n'
    new = '[project]\ndependencies = ["a", "c>=1.0"]\n'
    added, removed = diff_deps(old, new)
    assert added == {"c": "c>=1.0"}
    assert removed == {"b"}


def test_extract_pinned_version_picks_first_constraint():
    assert extract_pinned_version("redis>=5.0,<6") == "5.0"
    assert extract_pinned_version("httpx==0.28.1") == "0.28.1"
    assert extract_pinned_version("click") is None


def test_apply_edit_handles_each_tool():
    base = "x = 1\n"
    assert apply_edit(base, "Write", {"content": "y = 2\n"}) == "y = 2\n"
    assert apply_edit(base, "Edit", {"old_string": "x", "new_string": "y"}) == "y = 1\n"
    multi = apply_edit(base + "z = 3\n", "MultiEdit", {"edits": [
        {"old_string": "x", "new_string": "x1"},
        {"old_string": "z", "new_string": "z1"},
    ]})
    assert multi == "x1 = 1\nz1 = 3\n"


def test_detect_project_name_prefers_pyproject(tmp_path):
    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nname = "my-app"\n')
    assert detect_project_name(py, "fallback") == "my-app"


def test_detect_project_name_falls_back(tmp_path):
    assert detect_project_name(tmp_path / "missing.toml", "fallback") == "fallback"


def test_parse_deps_from_content_handles_invalid_toml():
    assert parse_deps_from_content("this is not toml [[[") == {}
