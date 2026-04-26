"""Tests for the YAML→JSON registry compiler.

Exercises both the validator (`lex_align_server.registry_schema.validate_registry`)
and the CLI surface (`lex-align-server registry compile`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lex_align_server.cli import main as server_cli
from lex_align_server.registry_schema import ValidationError, validate_registry


def test_validate_minimal_registry():
    out = validate_registry({
        "version": "1.0",
        "global_policies": {},
        "packages": {},
    })
    assert out["version"] == "1.0"
    assert out["global_policies"]["cve_threshold"] == pytest.approx(0.9)


def test_validate_rejects_bad_status():
    with pytest.raises(ValidationError):
        validate_registry({
            "version": "1",
            "packages": {"x": {"status": "wat"}},
        })


def test_validate_deprecated_requires_replacement():
    with pytest.raises(ValidationError):
        validate_registry({
            "version": "1",
            "packages": {"x": {"status": "deprecated"}},
        })


def test_validate_version_constrained_requires_a_bound():
    with pytest.raises(ValidationError):
        validate_registry({
            "version": "1",
            "packages": {"x": {"status": "version-constrained"}},
        })


def test_validate_rejects_unknown_global_field():
    with pytest.raises(ValidationError):
        validate_registry({
            "version": "1",
            "global_policies": {"made_up": True},
        })


@pytest.mark.parametrize("bad", [-0.1, 1.1, "x", True])
def test_validate_rejects_bad_cve_threshold(bad):
    with pytest.raises(ValidationError):
        validate_registry({
            "version": "1",
            "global_policies": {"cve_threshold": bad},
        })


def test_validate_accepts_custom_cve_threshold():
    out = validate_registry({
        "version": "1",
        "global_policies": {"cve_threshold": 0.7},
        "packages": {},
    })
    assert out["global_policies"]["cve_threshold"] == pytest.approx(0.7)


def test_compile_round_trip(tmp_path: Path):
    yml = tmp_path / "in.yml"
    out = tmp_path / "out.json"
    yml.write_text("""
version: "1.0"
global_policies:
  auto_approve_licenses: [MIT]
  cve_threshold: 0.85
packages:
  httpx:
    status: preferred
    reason: standard
""")
    runner = CliRunner()
    result = runner.invoke(server_cli, ["registry", "compile", str(yml), str(out)])
    assert result.exit_code == 0, result.output
    compiled = json.loads(out.read_text())
    assert compiled["packages"]["httpx"]["status"] == "preferred"
    assert compiled["global_policies"]["cve_threshold"] == pytest.approx(0.85)


def test_compile_surfaces_validation_errors(tmp_path: Path):
    yml = tmp_path / "in.yml"
    out = tmp_path / "out.json"
    yml.write_text("""
version: "1.0"
packages:
  x:
    status: wat
""")
    runner = CliRunner()
    result = runner.invoke(server_cli, ["registry", "compile", str(yml), str(out)])
    assert result.exit_code != 0
    assert "validation failed" in result.output.lower()
    assert not out.exists()
