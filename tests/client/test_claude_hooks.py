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
        self.approvals: list[tuple[str, str]] = []
        self.approval_should_raise: Optional[Exception] = None

    def __enter__(self) -> "_StubClient":
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def check(self, package: str, version: Optional[str] = None) -> Verdict:
        return self._verdicts.get(package, _verdict("ALLOWED", package))

    def health(self) -> dict:
        return {"redis": "ok", "db": "ok", "registry_loaded": True}

    def pending_approvals(self, project=None) -> list[dict]:
        return []

    def security_report(self, project=None) -> dict:
        return {}

    def request_approval(self, package: str, rationale: str) -> dict:
        if self.approval_should_raise is not None:
            raise self.approval_should_raise
        self.approvals.append((package, rationale))
        return {"request_id": "x", "status": "PENDING_REVIEW", "package": package}


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


def test_detect_agent_prefers_explicit_env(monkeypatch):
    """LEXALIGN_AGENT_* env vars are the explicit, operator-set source of
    truth — they win over anything the event payload claims."""
    monkeypatch.setenv("LEXALIGN_AGENT_MODEL", "opus")
    monkeypatch.setenv("LEXALIGN_AGENT_VERSION", "4.7")
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    model, version = claude_hooks._detect_agent({"model": "claude-sonnet-4-6"})
    assert (model, version) == ("opus", "4.7")


def test_detect_agent_parses_event_model_id(monkeypatch):
    """A raw model id like `claude-opus-4-7-20251001` should normalize to
    `opus` / `4.7` — strip the build tag, drop the `claude-` prefix, and
    convert `-` to `.` in the version."""
    monkeypatch.delenv("LEXALIGN_AGENT_MODEL", raising=False)
    monkeypatch.delenv("LEXALIGN_AGENT_VERSION", raising=False)
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    model, version = claude_hooks._detect_agent({"model": "claude-opus-4-7-20251001"})
    assert model == "opus"
    assert version == "4.7"


def test_detect_agent_returns_none_when_nothing_known(monkeypatch):
    monkeypatch.delenv("LEXALIGN_AGENT_MODEL", raising=False)
    monkeypatch.delenv("LEXALIGN_AGENT_VERSION", raising=False)
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_MODEL", raising=False)
    assert claude_hooks._detect_agent({}) == (None, None)


def test_pre_tool_use_passes_agent_to_client(tmp_path: Path, monkeypatch):
    """The hook should hand the detected agent identity into LexAlignClient
    so the X-LexAlign-Agent-* headers get attached to /evaluate calls."""
    _seed(tmp_path, ["click"])
    monkeypatch.setenv("LEXALIGN_AGENT_MODEL", "opus")
    monkeypatch.setenv("LEXALIGN_AGENT_VERSION", "4.7")

    captured: dict = {}

    def factory(cfg, **kwargs):
        captured["kwargs"] = kwargs
        return _StubClient({})

    monkeypatch.setattr(claude_hooks, "LexAlignClient", factory)
    config = ClientConfig(project="demo")
    event = {
        "tool_name": "Edit",
        "tool_input": {
            "path": str(tmp_path / "pyproject.toml"),
            "old_string": '"click"',
            "new_string": '"click", "httpx"',
        },
    }
    claude_hooks.handle_pre_tool_use(event, tmp_path, config)
    assert captured["kwargs"]["agent_model"] == "opus"
    assert captured["kwargs"]["agent_version"] == "4.7"


def test_pre_tool_use_auto_enqueues_provisional(tmp_path: Path, monkeypatch):
    """In single-user mode, a PROVISIONALLY_ALLOWED verdict should
    auto-fire request-approval so the user-as-reviewer flow stays a
    single tool call from Claude's perspective."""
    _seed(tmp_path, ["click"])
    stub = _StubClient({
        "newpkg": _verdict(
            "PROVISIONALLY_ALLOWED", "newpkg",
            reason="not in registry; license + CVE pass",
            license="MIT", is_requestable=True,
        ),
    })
    monkeypatch.setattr(claude_hooks, "LexAlignClient", lambda cfg, **_: stub)
    config = ClientConfig(project="demo", auto_request_approval=True)
    event = {
        "tool_name": "Edit",
        "tool_input": {
            "path": str(tmp_path / "pyproject.toml"),
            "old_string": '"click"',
            "new_string": '"click", "newpkg"',
        },
    }
    decision, msg = claude_hooks.handle_pre_tool_use(event, tmp_path, config)
    assert decision == "allow"
    assert "auto-enqueued for review" in msg
    assert len(stub.approvals) == 1
    pkg, rationale = stub.approvals[0]
    assert pkg == "newpkg"
    assert "newpkg" in rationale
    assert "MIT" in rationale


def test_pre_tool_use_skips_auto_enqueue_when_disabled(tmp_path: Path, monkeypatch):
    """With auto_request_approval=False the hook reverts to the
    advisory message and does not POST anything."""
    _seed(tmp_path, ["click"])
    stub = _StubClient({
        "newpkg": _verdict(
            "PROVISIONALLY_ALLOWED", "newpkg",
            reason="not in registry; license + CVE pass",
            is_requestable=True,
        ),
    })
    monkeypatch.setattr(claude_hooks, "LexAlignClient", lambda cfg, **_: stub)
    config = ClientConfig(project="demo", auto_request_approval=False)
    event = {
        "tool_name": "Edit",
        "tool_input": {
            "path": str(tmp_path / "pyproject.toml"),
            "old_string": '"click"',
            "new_string": '"click", "newpkg"',
        },
    }
    decision, msg = claude_hooks.handle_pre_tool_use(event, tmp_path, config)
    assert decision == "allow"
    assert stub.approvals == []
    assert "request-approval" in msg


def test_pre_tool_use_auto_enqueue_failure_does_not_block(tmp_path: Path, monkeypatch):
    """If the proposer is briefly unavailable the edit should still go
    through — the rationale is informational, not a gate."""
    from lex_align_client.api import ServerUnreachable

    _seed(tmp_path, ["click"])
    stub = _StubClient({
        "newpkg": _verdict(
            "PROVISIONALLY_ALLOWED", "newpkg", reason="ok",
            is_requestable=True,
        ),
    })
    stub.approval_should_raise = ServerUnreachable("connection refused")
    monkeypatch.setattr(claude_hooks, "LexAlignClient", lambda cfg, **_: stub)
    config = ClientConfig(project="demo", auto_request_approval=True)
    event = {
        "tool_name": "Edit",
        "tool_input": {
            "path": str(tmp_path / "pyproject.toml"),
            "old_string": '"click"',
            "new_string": '"click", "newpkg"',
        },
    }
    decision, msg = claude_hooks.handle_pre_tool_use(event, tmp_path, config)
    assert decision == "allow"
    assert "auto-enqueue failed" in msg


def test_session_start_brief_includes_pending_approvals(tmp_path: Path, monkeypatch):
    _seed(tmp_path, ["click"])

    class _BriefStub(_StubClient):
        def pending_approvals(self, project=None):
            return [{"package": "alpha"}, {"package": "beta"}]

        def security_report(self, project=None):
            return {"severity_distribution": {"critical": 2, "high": 1}}

    monkeypatch.setattr(
        claude_hooks, "LexAlignClient",
        lambda cfg, **_: _BriefStub({}),
    )
    config = ClientConfig(project="demo")
    text = claude_hooks.handle_session_start({}, tmp_path, config)
    assert "Pending approvals: 2" in text
    assert "alpha" in text
    assert "CVE pressure: critical=2" in text
