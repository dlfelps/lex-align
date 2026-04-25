"""Pre-commit hook tests.

We monkey-patch the LexAlignClient inside the precommit module so the test
doesn't need a running server.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import pytest

from lex_align_client import precommit
from lex_align_client.api import Verdict
from lex_align_client.config import ClientConfig, save_config


def _verdict(verdict: str = "ALLOWED", *, package: str, **kwargs) -> Verdict:
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
        self._by_package = by_package

    def __enter__(self) -> "_StubClient":
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def check(self, package: str, version: Optional[str] = None) -> Verdict:
        return self._by_package.get(package, _verdict(package=package))


def _seed_project(tmp_path: Path, deps: list[str]) -> None:
    save_config(tmp_path, ClientConfig(project="demo"))
    deps_str = "[" + ", ".join(f'"{d}"' for d in deps) + "]"
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "p"\ndependencies = {deps_str}\n'
    )
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)


def test_precommit_passes_when_all_allowed(tmp_path: Path, monkeypatch):
    _seed_project(tmp_path, ["httpx", "click"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        precommit, "LexAlignClient",
        lambda config, **_: _StubClient({}),
    )
    assert precommit.run() == 0


def test_precommit_blocks_on_denied(tmp_path: Path, monkeypatch, capsys):
    _seed_project(tmp_path, ["requests"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        precommit, "LexAlignClient",
        lambda config, **_: _StubClient({
            "requests": _verdict(
                "DENIED", package="requests", reason="deprecated",
                replacement="httpx",
            ),
        }),
    )
    rc = precommit.run()
    assert rc == 1
    err = capsys.readouterr().err
    assert "commit blocked" in err
    assert "requests" in err
    assert "use instead: httpx" in err


def test_precommit_warns_on_transport_error_but_passes(tmp_path: Path, monkeypatch, capsys):
    _seed_project(tmp_path, ["click"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        precommit, "LexAlignClient",
        lambda config, **_: _StubClient({
            "click": _verdict("ALLOWED", package="click", transport_error=True),
        }),
    )
    rc = precommit.run()
    assert rc == 0
    assert "could not be checked" in capsys.readouterr().err


def test_precommit_no_op_when_no_config(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = precommit.run()
    assert rc == 0
    assert "no .lexalign.toml" in capsys.readouterr().err


def test_precommit_reads_staged_pyproject(tmp_path: Path, monkeypatch):
    """When pyproject.toml is staged but the working copy differs, the hook
    should evaluate the staged contents."""
    _seed_project(tmp_path, ["click"])
    monkeypatch.chdir(tmp_path)

    subprocess.run(["git", "add", "pyproject.toml"], cwd=tmp_path, check=True)
    # Configure git identity and disable signing so the test works in CI envs
    # that have a gpg.format/gpgsign default we don't control.
    for k, v in (
        ("user.email", "t@t"), ("user.name", "t"),
        ("commit.gpgsign", "false"), ("tag.gpgsign", "false"),
        ("gpg.format", "openpgp"),
    ):
        subprocess.run(["git", "config", k, v], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "init", "--quiet"],
        cwd=tmp_path, check=True,
    )
    # Now stage a change adding a banned dep, but mutate the working copy
    # afterwards to a third state. The hook must evaluate the staged version.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\ndependencies = ["requests"]\n'
    )
    subprocess.run(["git", "add", "pyproject.toml"], cwd=tmp_path, check=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\ndependencies = ["click"]\n'  # working copy reverts
    )

    seen: list[str] = []

    class Capturing(_StubClient):
        def check(self, package, version=None):
            seen.append(package)
            return _verdict(package=package)

    monkeypatch.setattr(
        precommit, "LexAlignClient",
        lambda config, **_: Capturing({}),
    )
    rc = precommit.run()
    assert rc == 0
    # The hook must have evaluated the staged content (`requests`), not the
    # working-copy content (`click`).
    assert "requests" in seen
