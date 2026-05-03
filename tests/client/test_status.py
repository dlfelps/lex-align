"""Tests for the `lex-align-client status` command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from lex_align_client import cli
from lex_align_client import status as status_module
from lex_align_client.config import ClientConfig, save_config
from lex_align_client.settings import install_claude_hooks, install_precommit


class _StubClient:
    def __init__(
        self,
        *,
        health: dict | None = None,
        pending: list | None = None,
        security: dict | None = None,
        raise_on_health: bool = False,
    ):
        self._health = health or {
            "redis": "ok", "db": "ok", "registry_loaded": True
        }
        self._pending = pending or []
        self._security = security or {}
        self._raise_on_health = raise_on_health

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def health(self):
        if self._raise_on_health:
            raise RuntimeError("server down")
        return self._health

    def pending_approvals(self, project=None):
        return list(self._pending)

    def security_report(self, project=None):
        return dict(self._security)


def _seed(tmp_path: Path, deps: list[str]) -> ClientConfig:
    config = ClientConfig(project="demo")
    save_config(tmp_path, config)
    deps_str = "[" + ", ".join(f'"{d}"' for d in deps) + "]"
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "p"\ndependencies = {deps_str}\n'
    )
    return config


def test_status_collects_health_pending_and_hooks(tmp_path: Path, monkeypatch):
    config = _seed(tmp_path, ["httpx"])
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    install_claude_hooks(tmp_path)
    install_precommit(tmp_path)

    monkeypatch.setattr(
        status_module, "LexAlignClient",
        lambda cfg, **_: _StubClient(
            pending=[
                {"package": "newpkg", "rationale": "x"},
                {"package": "newpkg", "rationale": "y"},
                {"package": "another", "rationale": "z"},
            ],
            security={"severity_distribution": {"critical": 1, "high": 0}},
        ),
    )
    report = status_module.collect(tmp_path, config)
    assert report.server_reachable is True
    assert report.deps_total == 1
    assert report.pending_approvals == 3
    assert set(report.pending_packages) == {"newpkg", "another"}
    assert report.cve_severity["critical"] == 1
    assert report.precommit_installed is True
    assert report.claude_hooks["PreToolUse"] is True


def test_status_handles_unreachable_server(tmp_path: Path, monkeypatch):
    config = _seed(tmp_path, ["httpx"])
    monkeypatch.setattr(
        status_module, "LexAlignClient",
        lambda cfg, **_: _StubClient(raise_on_health=True),
    )
    report = status_module.collect(tmp_path, config)
    assert report.server_reachable is False
    assert "server down" in (report.server_error or "")
    # Local sections still populated.
    assert report.deps_total == 1


def test_status_command_emits_text(tmp_path: Path, monkeypatch):
    _seed(tmp_path, ["httpx"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        status_module, "LexAlignClient",
        lambda cfg, **_: _StubClient(),
    )
    runner = CliRunner()
    result = runner.invoke(cli.main, ["status"])
    assert result.exit_code == 0, result.output
    assert "lex-align status" in result.output
    assert "demo" in result.output


def test_status_command_emits_json(tmp_path: Path, monkeypatch):
    _seed(tmp_path, ["httpx"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        status_module, "LexAlignClient",
        lambda cfg, **_: _StubClient(),
    )
    runner = CliRunner()
    result = runner.invoke(cli.main, ["status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["project"] == "demo"
    assert payload["server"]["reachable"] is True
