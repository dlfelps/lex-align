"""Tests for tools/compile_registry.py."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def compile_registry_module():
    # Load the tool by path since it isn't an installed package.
    path = Path(__file__).resolve().parents[1] / "tools" / "compile_registry.py"
    spec = importlib.util.spec_from_file_location("compile_registry", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_validate_registry_happy_path(compile_registry_module):
    doc = {
        "version": "1.2",
        "global_policies": {
            "auto_approve_licenses": ["MIT"],
            "hard_ban_licenses": ["GPL-3.0"],
        },
        "packages": {
            "httpx": {"status": "preferred", "reason": "ok"},
            "requests": {"status": "deprecated", "replacement": "httpx"},
            "crypto": {"status": "version-constrained", "min_version": "1.0"},
        },
    }
    compiled = compile_registry_module.validate_registry(doc)
    assert compiled["version"] == "1.2"
    assert compiled["global_policies"]["unknown_license_policy"] == "block"
    assert set(compiled["packages"]) == {"httpx", "requests", "crypto"}


def test_validate_registry_rejects_missing_version(compile_registry_module):
    with pytest.raises(compile_registry_module.ValidationError, match="version"):
        compile_registry_module.validate_registry({})


def test_validate_registry_rejects_invalid_status(compile_registry_module):
    doc = {"version": "1", "packages": {"foo": {"status": "bogus"}}}
    with pytest.raises(compile_registry_module.ValidationError, match="invalid status"):
        compile_registry_module.validate_registry(doc)


def test_validate_registry_rejects_deprecated_without_replacement(compile_registry_module):
    doc = {"version": "1", "packages": {"foo": {"status": "deprecated"}}}
    with pytest.raises(compile_registry_module.ValidationError, match="replacement"):
        compile_registry_module.validate_registry(doc)


def test_validate_registry_rejects_version_constrained_without_bounds(compile_registry_module):
    doc = {"version": "1", "packages": {"foo": {"status": "version-constrained"}}}
    with pytest.raises(compile_registry_module.ValidationError, match="min_version"):
        compile_registry_module.validate_registry(doc)


def test_validate_registry_rejects_unknown_field(compile_registry_module):
    doc = {"version": "1", "packages": {"foo": {"status": "preferred", "stray_field": 1}}}
    with pytest.raises(compile_registry_module.ValidationError, match="unknown fields"):
        compile_registry_module.validate_registry(doc)


def test_validate_registry_rejects_invalid_unknown_license_policy(compile_registry_module):
    doc = {
        "version": "1",
        "global_policies": {"unknown_license_policy": "nope"},
        "packages": {},
    }
    with pytest.raises(compile_registry_module.ValidationError, match="unknown_license_policy"):
        compile_registry_module.validate_registry(doc)


def test_compile_registry_main_writes_file(tmp_path: Path, compile_registry_module, monkeypatch):
    yml = tmp_path / "r.yml"
    yml.write_text("""\
version: "1.2"
global_policies:
  auto_approve_licenses: [MIT]
  hard_ban_licenses: [GPL-3.0]
packages:
  httpx:
    status: preferred
    reason: ok
""")
    out = tmp_path / "out.json"
    monkeypatch.setattr("sys.argv", ["compile_registry.py", str(yml), str(out)])
    rc = compile_registry_module.main()
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["version"] == "1.2"
    assert data["packages"]["httpx"]["status"] == "preferred"


def test_compile_registry_main_reports_missing_input(tmp_path: Path, compile_registry_module, monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv", ["compile_registry.py", str(tmp_path / "nope.yml"), str(tmp_path / "out.json")]
    )
    rc = compile_registry_module.main()
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err
