"""Tests for `lex-align-server quickstart`."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from lex_align_server import cli, quickstart


def test_materialize_writes_registry_and_marker(tmp_path: Path):
    target = tmp_path / "lexalign"
    result = quickstart.materialize(target, bind_host="127.0.0.1", bind_port=8765)
    assert result.target == target.resolve()
    assert result.registry_yml.exists()
    assert result.registry_json.exists()
    assert (result.target / quickstart.QUICKSTART_MARKER).exists()
    # Sanity-check the marker content.
    marker_text = (result.target / quickstart.QUICKSTART_MARKER).read_text()
    assert "bind_port = 8765" in marker_text


def test_materialize_is_idempotent(tmp_path: Path):
    target = tmp_path / "lexalign"
    first = quickstart.materialize(target)
    yml_mtime = first.registry_yml.stat().st_mtime
    second = quickstart.materialize(target)
    # The YAML should not be overwritten.
    assert second.registry_yml.stat().st_mtime == yml_mtime
    assert first.registry_yml in second.skipped or first.registry_yml in [
        p for p in second.skipped
    ]


def test_materialize_force_overwrites(tmp_path: Path):
    target = tmp_path / "lexalign"
    first = quickstart.materialize(target)
    first.registry_yml.write_text("# user edit\npackages: {}\n")
    second = quickstart.materialize(target, force=True)
    # With --force the YAML is overwritten by the bundled example.
    assert "packages:" in second.registry_yml.read_text()
    assert second.registry_yml in second.written


def test_apply_env_returns_overrides(tmp_path: Path):
    target = tmp_path / "lexalign"
    result = quickstart.materialize(target)
    env = quickstart.apply_env(result)
    assert env["REGISTRY_PATH"] == str(result.registry_yml)
    assert env["DATABASE_PATH"] == str(result.database_path)
    assert env["BIND_HOST"] == "127.0.0.1"
    assert env["BIND_PORT"] == "8765"


def test_cli_quickstart_no_serve_materializes_only(tmp_path: Path):
    """`quickstart --no-serve` should write the bundle but not call uvicorn."""
    target = tmp_path / "lexalign"
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        ["quickstart", "--target", str(target), "--no-serve"],
    )
    assert result.exit_code == 0, result.output
    assert (target / "registry.yml").exists()
    assert (target / quickstart.QUICKSTART_MARKER).exists()
    assert "Skipping `serve`" in result.output
