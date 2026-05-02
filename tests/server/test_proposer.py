"""Unit tests for the proposer backends + loader."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

from lex_align_server.config import Settings
from lex_align_server.proposer import (
    ProposalContext,
    ProposedRule,
    Proposer,
    ProposerError,
    load_proposer,
)
from lex_align_server.proposer.github import GitHubProposer
from lex_align_server.proposer.local_file import LocalFileProposer
from lex_align_server.proposer.local_git import LocalGitProposer
from lex_align_server.proposer.log_only import LogOnlyProposer


GIT_AVAILABLE = bool(shutil.which("git"))


def _ctx(rationale: str = "") -> ProposalContext:
    return ProposalContext(
        source="operator", project="demo", requester="alice@example.com",
        rationale=rationale,
    )


# ── log_only ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_only_proposer_is_a_no_op():
    proposer = LogOnlyProposer()
    result = await proposer.propose(
        ProposedRule(name="numpy", status="approved"), _ctx(),
    )
    assert result.backend == "log_only"
    assert result.status == "logged"
    assert "REGISTRY_PROPOSER" in result.detail


# ── local_file ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_file_proposer_creates_yaml(tmp_path):
    """Calling propose against a fresh path bootstraps the YAML."""
    path = tmp_path / "registry.yml"
    proposer = LocalFileProposer(path)
    await proposer.propose(
        ProposedRule(name="numpy", status="approved", reason="needed"), _ctx(),
    )
    doc = yaml.safe_load(path.read_text())
    assert doc["packages"]["numpy"]["status"] == "approved"
    assert doc["packages"]["numpy"]["reason"] == "needed"


@pytest.mark.asyncio
async def test_local_file_proposer_overwrites_existing_rule(tmp_path):
    path = tmp_path / "registry.yml"
    path.write_text(
        "version: '1'\n"
        "global_policies: {}\n"
        "packages:\n"
        "  numpy:\n"
        "    status: approved\n"
    )
    proposer = LocalFileProposer(path)
    await proposer.propose(
        ProposedRule(name="numpy", status="banned", reason="policy"), _ctx(),
    )
    doc = yaml.safe_load(path.read_text())
    assert doc["packages"]["numpy"]["status"] == "banned"


@pytest.mark.asyncio
async def test_local_file_proposer_rejects_invalid_rule_atomically(tmp_path):
    """A proposal that fails validation must NOT corrupt the file on disk."""
    path = tmp_path / "registry.yml"
    path.write_text(
        "version: '1'\n"
        "global_policies: {}\n"
        "packages:\n"
        "  numpy:\n"
        "    status: approved\n"
    )
    before = path.read_text()
    proposer = LocalFileProposer(path)
    with pytest.raises(ProposerError):
        # `deprecated` requires a `replacement`. Missing one fails validation.
        await proposer.propose(
            ProposedRule(name="numpy", status="deprecated"), _ctx(),
        )
    # File on disk is unchanged.
    assert path.read_text() == before


@pytest.mark.asyncio
async def test_local_file_proposer_normalizes_name(tmp_path):
    """The YAML key is the normalized form so case / hyphen / dot
    differences collapse onto one rule (matches lookup semantics)."""
    path = tmp_path / "registry.yml"
    proposer = LocalFileProposer(path)
    await proposer.propose(
        ProposedRule(name="Some-Pkg", status="approved"), _ctx(),
    )
    doc = yaml.safe_load(path.read_text())
    assert "some_pkg" in doc["packages"]
    assert "Some-Pkg" not in doc["packages"]


# ── local_git ────────────────────────────────────────────────────────────


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git binary not on PATH")
@pytest.mark.asyncio
async def test_local_git_proposer_commits_to_repo(tmp_path):
    """A successful propose() creates a commit on HEAD of the local repo."""
    subprocess.check_call(["git", "init", "-q", str(tmp_path)])
    # Repo-level config so we don't depend on global user identity / signing
    # config in the test environment.
    for key, value in [
        ("user.email", "test@example.com"),
        ("user.name", "test"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ]:
        subprocess.check_call(["git", "-C", str(tmp_path), "config", key, value])
    # Make an initial commit so HEAD exists.
    (tmp_path / "README.md").write_text("init")
    subprocess.check_call(["git", "-C", str(tmp_path), "add", "README.md"])
    subprocess.check_call(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"]
    )

    path = tmp_path / "registry.yml"
    proposer = LocalGitProposer(path)
    result = await proposer.propose(
        ProposedRule(name="numpy", status="approved"),
        _ctx(rationale="we need numpy for the matmul case"),
    )
    assert result.backend == "local_git"
    assert result.status == "applied"
    assert result.commit_sha
    # The latest commit message includes the rationale.
    msg = subprocess.check_output(
        ["git", "-C", str(tmp_path), "log", "-1", "--pretty=%B"], text=True,
    )
    assert "numpy" in msg
    assert "we need numpy" in msg


# ── loader auto-detect ───────────────────────────────────────────────────


def test_loader_picks_log_only_when_nothing_configured():
    settings = Settings()
    proposer = load_proposer(settings, http_client=None)  # type: ignore[arg-type]
    assert isinstance(proposer, LogOnlyProposer)


def test_loader_picks_local_file_when_path_set(tmp_path):
    settings = Settings(registry_path=tmp_path / "registry.yml")
    proposer = load_proposer(settings, http_client=None)  # type: ignore[arg-type]
    assert isinstance(proposer, LocalFileProposer)


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git binary not on PATH")
def test_loader_picks_local_git_when_inside_working_tree(tmp_path):
    subprocess.check_call(["git", "init", "-q", str(tmp_path)])
    settings = Settings(registry_path=tmp_path / "registry.yml")
    proposer = load_proposer(settings, http_client=None)  # type: ignore[arg-type]
    assert isinstance(proposer, LocalGitProposer)


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git binary not on PATH")
def test_loader_picks_github_when_local_git_has_github_remote(tmp_path, monkeypatch):
    """A local git repo whose origin points to GitHub, combined with a token
    in the environment, should auto-select the GitHub proposer so approval
    requests open PRs instead of just committing locally."""
    subprocess.check_call(["git", "init", "-q", str(tmp_path)])
    subprocess.check_call([
        "git", "-C", str(tmp_path), "remote", "add", "origin",
        "https://github.com/acme/policy.git",
    ])
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_autodetected")
    workdir = tmp_path / "workdir"
    settings = Settings(
        registry_path=tmp_path / "registry.yml",
        registry_repo_workdir=workdir,
    )
    http = httpx.AsyncClient()
    proposer = load_proposer(settings, http_client=http)
    assert isinstance(proposer, GitHubProposer)
    assert proposer.owner == "acme"
    assert proposer.repo == "policy"
    assert proposer.token == "ghp_autodetected"


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git binary not on PATH")
def test_loader_picks_local_git_when_no_github_token(tmp_path, monkeypatch):
    """Without a token, fall back to local_git even when origin is on GitHub."""
    subprocess.check_call(["git", "init", "-q", str(tmp_path)])
    subprocess.check_call([
        "git", "-C", str(tmp_path), "remote", "add", "origin",
        "https://github.com/acme/policy.git",
    ])
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    settings = Settings(
        registry_path=tmp_path / "registry.yml",
        registry_repo_token="",
    )
    proposer = load_proposer(settings, http_client=None)  # type: ignore[arg-type]
    assert isinstance(proposer, LocalGitProposer)


def test_loader_picks_github_when_repo_url_set(tmp_path):
    settings = Settings(
        registry_repo_url="https://github.com/acme/policy",
        registry_repo_token="ghp_test",
        registry_repo_workdir=tmp_path / "workdir",
    )
    # Construction only stashes the client; no network calls happen here.
    http = httpx.AsyncClient()
    try:
        proposer = load_proposer(settings, http_client=http)
        assert isinstance(proposer, GitHubProposer)
        assert proposer.owner == "acme"
        assert proposer.repo == "policy"
    finally:
        # Sync close is fine — no requests issued.
        pass


def test_loader_explicit_override_wins(tmp_path):
    """If the operator explicitly sets REGISTRY_PROPOSER, ignore the
    auto-detection signals (a writable path that would normally pick
    local_file)."""
    settings = Settings(
        registry_proposer="log_only",
        registry_path=tmp_path / "registry.yml",
    )
    proposer = load_proposer(settings, http_client=None)  # type: ignore[arg-type]
    assert isinstance(proposer, LogOnlyProposer)


def test_loader_rejects_unknown_backend():
    settings = Settings(registry_proposer="bogus")
    with pytest.raises(ValueError, match="Unknown REGISTRY_PROPOSER"):
        load_proposer(settings, http_client=None)  # type: ignore[arg-type]


def test_loader_rejects_module_class_that_isnt_a_proposer(tmp_path, monkeypatch):
    pkg = tmp_path / "fakeprop"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "thing.py").write_text(
        "class NotAProposer:\n"
        "    def __init__(self, **kwargs): pass\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    settings = Settings(registry_proposer="fakeprop.thing:NotAProposer")
    with pytest.raises(TypeError, match="subclass"):
        load_proposer(settings, http_client=None)  # type: ignore[arg-type]


# ── github proposer URL parsing ──────────────────────────────────────────


def test_github_url_parsing_https(tmp_path):
    p = GitHubProposer(
        repo_url="https://github.com/acme/policy.git",
        registry_file_path="registry.yml",
        token="t", http_client=httpx.AsyncClient(),
        workdir=tmp_path / "wd",
    )
    assert (p.owner, p.repo) == ("acme", "policy")


def test_github_url_parsing_ssh(tmp_path):
    p = GitHubProposer(
        repo_url="git@github.com:acme/policy.git",
        registry_file_path="registry.yml",
        token="t", http_client=httpx.AsyncClient(),
        workdir=tmp_path / "wd",
    )
    assert (p.owner, p.repo) == ("acme", "policy")


def test_github_url_parsing_rejects_garbage(tmp_path):
    with pytest.raises(ValueError, match="GitHub repo URL"):
        GitHubProposer(
            repo_url="not-a-url",
            registry_file_path="registry.yml",
            token="t", http_client=httpx.AsyncClient(),
            workdir=tmp_path / "wd",
        )
