"""Tests for the `lex-align-client audit` command and helper module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from click.testing import CliRunner

from lex_align_client import audit as audit_module
from lex_align_client import cli
from lex_align_client.api import Verdict
from lex_align_client.config import ClientConfig, save_config


def _verdict(verdict: str, package: str, **kwargs) -> Verdict:
    base = dict(
        verdict=verdict, reason=kwargs.get("reason", ""), package=package,
        version=None, resolved_version=None, registry_status=None,
        replacement=None, version_constraint=None, license=None,
        cve_ids=[], max_cvss=None, is_requestable=False,
        needs_rationale=False, transport_error=False,
    )
    base.update(kwargs)
    return Verdict(**base)


class _StubClient:
    def __init__(self, by_package: dict[str, Verdict]):
        self._verdicts = by_package

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def check(self, package: str, version: Optional[str] = None) -> Verdict:
        return self._verdicts.get(package, _verdict("ALLOWED", package))


def _seed(tmp_path: Path, deps: list[str]) -> ClientConfig:
    config = ClientConfig(project="demo")
    save_config(tmp_path, config)
    deps_str = "[" + ", ".join(f'"{d}"' for d in deps) + "]"
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "p"\ndependencies = {deps_str}\n'
    )
    return config


def test_audit_returns_zero_when_all_allowed(tmp_path: Path, monkeypatch):
    config = _seed(tmp_path, ["httpx", "click"])
    monkeypatch.setattr(
        audit_module, "LexAlignClient",
        lambda cfg, **_: _StubClient({}),
    )
    rc = audit_module.run(tmp_path, config, as_json=False)
    assert rc == 0


def test_audit_exits_2_on_denied(tmp_path: Path, monkeypatch, capsys):
    config = _seed(tmp_path, ["requests"])
    monkeypatch.setattr(
        audit_module, "LexAlignClient",
        lambda cfg, **_: _StubClient({
            "requests": _verdict(
                "DENIED", "requests", reason="deprecated", replacement="httpx",
            ),
        }),
    )
    rc = audit_module.run(tmp_path, config, as_json=False)
    assert rc == 2
    out = capsys.readouterr().out
    assert "DENIED" in out
    assert "requests" in out
    assert "use instead: httpx" in out


def test_audit_json_output(tmp_path: Path, monkeypatch, capsys):
    config = _seed(tmp_path, ["httpx"])
    monkeypatch.setattr(
        audit_module, "LexAlignClient",
        lambda cfg, **_: _StubClient({}),
    )
    rc = audit_module.run(tmp_path, config, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project"] == "demo"
    assert payload["deps_total"] == 1
    assert payload["summary"]["allowed"] == 1
    assert payload["summary"]["denied"] == 0


def test_audit_handles_no_deps(tmp_path: Path, monkeypatch, capsys):
    config = ClientConfig(project="demo")
    save_config(tmp_path, config)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\n')
    monkeypatch.setattr(
        audit_module, "LexAlignClient",
        lambda cfg, **_: _StubClient({}),
    )
    rc = audit_module.run(tmp_path, config, as_json=False)
    assert rc == 0
    assert "No `[project].dependencies`" in capsys.readouterr().out


def test_audit_command_via_cli(tmp_path: Path, monkeypatch):
    _seed(tmp_path, ["httpx"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        audit_module, "LexAlignClient",
        lambda cfg, **_: _StubClient({}),
    )
    runner = CliRunner()
    result = runner.invoke(cli.main, ["audit"])
    assert result.exit_code == 0, result.output
    assert "Audited 1 runtime dep" in result.output
