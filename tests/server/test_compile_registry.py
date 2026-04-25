"""Tests for the YAML→JSON registry compiler."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# tools/ isn't a package; load the script directly.
_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(_TOOLS_DIR))
import compile_registry as cr  # noqa: E402


def test_validate_minimal_registry():
    out = cr.validate_registry({
        "version": "1.0",
        "global_policies": {},
        "packages": {},
    })
    assert out["version"] == "1.0"
    assert out["global_policies"]["cve_threshold"] == pytest.approx(0.9)


def test_validate_rejects_bad_status():
    with pytest.raises(cr.ValidationError):
        cr.validate_registry({
            "version": "1",
            "packages": {"x": {"status": "wat"}},
        })


def test_validate_deprecated_requires_replacement():
    with pytest.raises(cr.ValidationError):
        cr.validate_registry({
            "version": "1",
            "packages": {"x": {"status": "deprecated"}},
        })


def test_validate_version_constrained_requires_a_bound():
    with pytest.raises(cr.ValidationError):
        cr.validate_registry({
            "version": "1",
            "packages": {"x": {"status": "version-constrained"}},
        })


def test_validate_rejects_unknown_global_field():
    with pytest.raises(cr.ValidationError):
        cr.validate_registry({
            "version": "1",
            "global_policies": {"made_up": True},
        })


@pytest.mark.parametrize("bad", [-0.1, 1.1, "x", True])
def test_validate_rejects_bad_cve_threshold(bad):
    with pytest.raises(cr.ValidationError):
        cr.validate_registry({
            "version": "1",
            "global_policies": {"cve_threshold": bad},
        })


def test_validate_accepts_custom_cve_threshold():
    out = cr.validate_registry({
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
    rc = cr.main_with_args([str(yml), str(out)]) if hasattr(cr, "main_with_args") else None
    if rc is None:
        # Compile via the public main() with monkeypatched argv.
        sys_argv_backup = sys.argv
        sys.argv = ["compile_registry.py", str(yml), str(out)]
        try:
            rc = cr.main()
        finally:
            sys.argv = sys_argv_backup
    assert rc == 0
    compiled = json.loads(out.read_text())
    assert compiled["packages"]["httpx"]["status"] == "preferred"
    assert compiled["global_policies"]["cve_threshold"] == pytest.approx(0.85)
