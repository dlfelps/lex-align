"""CLI surface tests using Click's CliRunner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from lex_align_client import cli
from lex_align_client.api import Verdict
from lex_align_client.config import ClientConfig, load_config, save_config


def test_init_writes_config_with_yes(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "myapp"\n')
    runner = CliRunner()
    result = runner.invoke(cli.main, [
        "init", "--yes", "--no-claude-hooks", "--no-precommit",
    ])
    assert result.exit_code == 0, result.output
    cfg = load_config(tmp_path)
    assert cfg is not None
    assert cfg.project == "myapp"
    assert cfg.mode == "single-user"
    assert cfg.fail_open is True


def test_init_org_mode_flips_fail_open(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli.main, [
        "init", "--yes", "--mode", "org", "--project", "demo",
        "--no-claude-hooks", "--no-precommit",
    ])
    assert result.exit_code == 0, result.output
    cfg = load_config(tmp_path)
    assert cfg.mode == "org"
    assert cfg.fail_open is False


def _verdict(verdict: str, package: str, **kwargs) -> Verdict:
    base = dict(
        verdict=verdict, reason=kwargs.get("reason", ""), package=package,
        version=None, resolved_version=None, registry_status=None,
        replacement=None, version_constraint=None, license=None,
        cve_ids=[], max_cvss=None, is_requestable=False, needs_rationale=False,
        transport_error=False,
    )
    base.update(kwargs)
    return Verdict(**base)


class _StubClient:
    def __init__(self, verdict: Verdict):
        self.verdict = verdict
        self.approval = None

    def __enter__(self): return self

    def __exit__(self, *a): return None

    def check(self, package, version=None):
        return self.verdict

    def request_approval(self, package, rationale):
        self.approval = (package, rationale)
        return {"request_id": "abc", "status": "PENDING_REVIEW",
                "package": package, "project": "demo"}


def test_check_command_emits_json_and_exits_2_on_denial(tmp_path: Path, monkeypatch):
    save_config(tmp_path, ClientConfig(project="demo"))
    monkeypatch.chdir(tmp_path)
    stub = _StubClient(_verdict("DENIED", "requests", reason="deprecated"))
    monkeypatch.setattr(cli, "LexAlignClient", lambda cfg, **_: stub)
    runner = CliRunner()
    result = runner.invoke(cli.main, ["check", "--package", "requests"])
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output.strip().splitlines()[-1] if False else result.output[result.output.index("{"):])
    # JSON is dumped with indent=2; just confirm contents.
    assert '"verdict": "DENIED"' in result.output
    assert '"package": "requests"' in result.output


def test_check_command_exits_0_on_allow(tmp_path: Path, monkeypatch):
    save_config(tmp_path, ClientConfig(project="demo"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli, "LexAlignClient",
        lambda cfg, **_: _StubClient(_verdict("ALLOWED", "httpx")),
    )
    runner = CliRunner()
    result = runner.invoke(cli.main, ["check", "--package", "httpx"])
    assert result.exit_code == 0, result.output


def test_check_command_requires_init(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli.main, ["check", "--package", "x"])
    assert result.exit_code != 0
    assert "init" in result.output


def test_request_approval_command(tmp_path: Path, monkeypatch):
    save_config(tmp_path, ClientConfig(project="demo"))
    monkeypatch.chdir(tmp_path)
    stub = _StubClient(_verdict("ALLOWED", "x"))
    monkeypatch.setattr(cli, "LexAlignClient", lambda cfg, **_: stub)
    runner = CliRunner()
    result = runner.invoke(cli.main, [
        "request-approval", "--package", "newpkg", "--rationale", "needed",
    ])
    assert result.exit_code == 0, result.output
    assert stub.approval == ("newpkg", "needed")


def test_uninstall_strips_hooks(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    runner = CliRunner()
    result = runner.invoke(cli.main, [
        "init", "--yes", "--project", "demo",
    ])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".claude" / "settings.json").exists()
    result = runner.invoke(cli.main, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    settings = (tmp_path / ".claude" / "settings.json").read_text()
    assert "lex-align" not in settings
