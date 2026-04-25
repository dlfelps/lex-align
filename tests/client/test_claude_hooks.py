"""Tests for the Claude Code hooks (Advisor surface)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from lex_align_client import claude_hooks
from lex_align_client.api import Verdict
from lex_align_client.config import ClientConfig, save_config


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
    def __init__(self, by_package: dict[str, Verdict]):
        self._verdicts = by_package

    def __enter__(self) -> "_StubClient":
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def check(self, package: str, version: Optional[str] = None) -> Verdict:
        return self._verdicts.get(package, _verdict("ALLOWED", package))

    def health(self) -> dict:
        return {"redis": "ok", "db": "ok", "registry_loaded": True}


def _seed(tmp_path: Path, deps: list[str]):
    save_config(tmp_path, ClientConfig(project="demo"))
    deps_str = "[" + ", ".join(f'"{d}"' for d in deps) + "]"
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "p"\ndependencies = {deps_str}\n'
    )


def test_pre_tool_use_allows_clean_edit(tmp_path: Path, monkeypatch):
    _seed(tmp_path, ["click"])
    monkeypatch.setattr(
        claude_hooks, "LexAlignClient",
        lambda cfg, **_: _StubClient({}),
    )
    config = ClientConfig(project="demo")
    event = {
        "tool_name": "Edit",
        "tool_input": {
            "path": str(tmp_path / "pyproject.toml"),
            "old_string": '"click"',
            "new_string": '"click", "httpx"',
        },
    }
    outcome = claude_hooks.handle_pre_tool_use(event, tmp_path, config)
    assert outcome is not None
    decision, msg = outcome
    assert decision == "allow"
    assert "+ httpx" in msg


def test_pre_tool_use_blocks_denied(tmp_path: Path, monkeypatch):
    _seed(tmp_path, ["click"])
    monkeypatch.setattr(
        claude_hooks, "LexAlignClient",
        lambda cfg, **_: _StubClient({
            "requests": _verdict("DENIED", "requests",
                                 reason="deprecated", replacement="httpx"),
        }),
    )
    config = ClientConfig(project="demo")
    event = {
        "tool_name": "Edit",
        "tool_input": {
            "path": str(tmp_path / "pyproject.toml"),
            "old_string": '"click"',
            "new_string": '"click", "requests"',
        },
    }
    outcome = claude_hooks.handle_pre_tool_use(event, tmp_path, config)
    assert outcome is not None
    decision, msg = outcome
    assert decision == "block"
    assert "requests" in msg
    assert "use instead: httpx" in msg


def test_pre_tool_use_ignores_unrelated_files(tmp_path: Path):
    save_config(tmp_path, ClientConfig(project="demo"))
    config = ClientConfig(project="demo")
    event = {
        "tool_name": "Edit",
        "tool_input": {"path": str(tmp_path / "src" / "main.py")},
    }
    assert claude_hooks.handle_pre_tool_use(event, tmp_path, config) is None


def test_pre_tool_use_fail_open_on_server_error(tmp_path: Path, monkeypatch):
    _seed(tmp_path, [])
    from lex_align_client.api import ServerUnreachable

    class Boom:
        def __enter__(self): raise ServerUnreachable("down")
        def __exit__(self, *a): return None

    monkeypatch.setattr(claude_hooks, "LexAlignClient", lambda cfg, **_: Boom())
    config = ClientConfig(project="demo", fail_open=True)
    event = {
        "tool_name": "Write",
        "tool_input": {
            "path": str(tmp_path / "pyproject.toml"),
            "content": '[project]\nname = "p"\ndependencies = ["new"]\n',
        },
    }
    outcome = claude_hooks.handle_pre_tool_use(event, tmp_path, config)
    assert outcome is not None
    decision, msg = outcome
    assert decision == "allow"
    assert "fail_open" in msg


def test_session_start_brief_includes_project(tmp_path: Path, monkeypatch):
    _seed(tmp_path, ["click", "httpx"])
    monkeypatch.setattr(
        claude_hooks, "LexAlignClient",
        lambda cfg, **_: _StubClient({}),
    )
    config = ClientConfig(project="demo", server_url="http://srv")
    text = claude_hooks.handle_session_start({}, tmp_path, config)
    assert "project: demo" in text
    assert "Tracked runtime dependencies: 2" in text
