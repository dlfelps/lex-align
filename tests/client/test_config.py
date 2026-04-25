"""Unit tests for `.lexalign.toml` config IO."""

from __future__ import annotations

from pathlib import Path

from lex_align_client.config import (
    CONFIG_FILENAME,
    ClientConfig,
    config_path,
    find_project_root,
    load_config,
    save_config,
)


def test_save_and_load_round_trip(tmp_path: Path):
    cfg = ClientConfig(
        project="demo", server_url="http://1.2.3.4:8765",
        mode="org", fail_open=False, api_key_env_var="OTHER_VAR",
    )
    save_config(tmp_path, cfg)
    loaded = load_config(tmp_path)
    assert loaded == cfg


def test_load_missing_returns_none(tmp_path: Path):
    assert load_config(tmp_path) is None


def test_find_project_root_walks_upwards(tmp_path: Path):
    # Place a config in the parent and search from a deep nested dir.
    save_config(tmp_path, ClientConfig(project="proj"))
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == tmp_path


def test_find_project_root_falls_back_to_cwd(tmp_path: Path):
    # No config anywhere → falls back to the start dir.
    assert find_project_root(tmp_path) == tmp_path


def test_config_path_constant(tmp_path: Path):
    assert config_path(tmp_path).name == CONFIG_FILENAME
