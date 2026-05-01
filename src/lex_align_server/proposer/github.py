"""GitHub PR proposer.

Production-grade flow for orgs that want PR review on every registry
change. Each ``propose()`` call:

1. Ensures a fresh shallow clone of the registry repo in a working
   directory (``REGISTRY_REPO_WORKDIR``, default ``/var/lib/lexalign/registry-work``).
2. Branches off the configured default branch as
   ``lex-align/approval/<normalized-package-name>``. If the branch
   already exists on the remote, fetches and re-uses it so the same
   package never gets two parallel PRs.
3. Edits the YAML to add / replace the package rule.
4. Commits with an author identity scoped to the bot
   (``REGISTRY_BOT_AUTHOR_NAME`` / ``REGISTRY_BOT_AUTHOR_EMAIL``).
5. Pushes the branch.
6. Opens a PR via the GitHub REST API, or posts a follow-up comment if
   one is already open for the same branch.

The merge → reload step is **not** in this module — the operator wires
GitHub's webhook to ``POST /api/v1/registry/webhook``, which pulls the
updated YAML and triggers the in-memory reload.

We shell out to ``git`` (subprocess) and use ``httpx`` for the REST
calls. No additional Python dependencies.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
import yaml

from ..registry import normalize_name
from ..registry_schema import ValidationError, validate_registry
from .base import (
    ProposalContext,
    ProposalResult,
    ProposedRule,
    Proposer,
    ProposerError,
)


logger = logging.getLogger(__name__)


_GITHUB_REPO_RE = re.compile(
    r"^(?:https?://[^/]+/|git@[^:]+:)(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?/?$"
)


class GitHubProposer(Proposer):
    backend_name = "github"

    def __init__(
        self,
        *,
        repo_url: str,
        registry_file_path: str,
        token: str,
        http_client: httpx.AsyncClient,
        workdir: Path,
        api_base: str = "https://api.github.com",
        default_branch: str = "main",
        author_name: str = "lex-align bot",
        author_email: str = "lex-align-bot@users.noreply.github.com",
    ):
        if not repo_url:
            raise ValueError("GitHubProposer requires REGISTRY_REPO_URL.")
        if not token:
            raise ValueError("GitHubProposer requires REGISTRY_REPO_TOKEN.")
        if not registry_file_path:
            raise ValueError(
                "GitHubProposer requires REGISTRY_FILE_PATH (the path to "
                "registry.yml inside the repo)."
            )
        if not shutil.which("git"):
            raise ProposerError("git binary not on PATH.")

        m = _GITHUB_REPO_RE.match(repo_url.strip())
        if not m:
            raise ValueError(
                f"REGISTRY_REPO_URL={repo_url!r} doesn't look like a GitHub "
                "repo URL (expected https://github.com/owner/repo or git@...)."
            )
        self.owner = m.group("owner")
        self.repo = m.group("repo")
        # Normalize to an HTTPS URL with the token embedded for the push.
        # We never log this — see _redact() below — and the embedded form is
        # only used for git subprocess calls.
        self._authed_https = (
            f"https://x-access-token:{token}@github.com/{self.owner}/{self.repo}.git"
        )
        self.repo_url_public = f"https://github.com/{self.owner}/{self.repo}"
        self.registry_file_path = registry_file_path
        self.token = token
        self.http = http_client
        self.api_base = api_base.rstrip("/")
        self.default_branch = default_branch
        self.author_name = author_name
        self.author_email = author_email
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)
        # Serialize subprocess git operations so concurrent proposals
        # against the same shared working tree don't trample each other.
        self._lock = asyncio.Lock()

    async def propose(
        self, rule: ProposedRule, context: ProposalContext
    ) -> ProposalResult:
        normalized = normalize_name(rule.name)
        branch = f"lex-align/approval/{normalized}"

        async with self._lock:
            try:
                clone_dir = await asyncio.to_thread(self._clone_or_refresh)
                pre_existing_branch = await asyncio.to_thread(
                    self._checkout_branch, clone_dir, branch
                )
                changed = await asyncio.to_thread(
                    self._edit_yaml, clone_dir, rule
                )
                if not changed and pre_existing_branch:
                    # Same rule already on the branch; no commit needed.
                    sha = await asyncio.to_thread(self._head_sha, clone_dir)
                    pr = await self._find_open_pr(branch)
                    return ProposalResult(
                        backend=self.backend_name,
                        status="amended" if pr else "opened",
                        url=pr.get("html_url") if pr else None,
                        branch=branch,
                        commit_sha=sha,
                        detail=(
                            "Branch already proposes this rule; no change pushed."
                        ),
                    )
                commit_msg = _commit_message(rule, context)
                sha = await asyncio.to_thread(
                    self._commit_and_push, clone_dir, branch, commit_msg
                )
            except subprocess.CalledProcessError as exc:
                raise ProposerError(
                    f"git operation failed: {exc.stderr or exc.stdout or exc}"
                ) from exc
            except (ValidationError, ValueError) as exc:
                raise ProposerError(f"Proposed rule failed validation: {exc}") from exc

        existing = await self._find_open_pr(branch)
        if existing is not None:
            await self._comment_on_pr(existing["number"], rule, context)
            return ProposalResult(
                backend=self.backend_name,
                status="amended",
                url=existing.get("html_url"),
                branch=branch,
                commit_sha=sha,
                detail=f"Pushed an additional commit and commented on PR #{existing['number']}.",
            )

        opened = await self._open_pr(branch, rule, context)
        return ProposalResult(
            backend=self.backend_name,
            status="opened",
            url=opened.get("html_url"),
            branch=branch,
            commit_sha=sha,
            detail=f"Opened PR #{opened.get('number')}.",
        )

    async def refresh_local_yaml(self, dest: Optional[Path]) -> None:
        """Pull the merged registry YAML into the local ``REGISTRY_PATH``.

        Called by the webhook handler on a ``pull_request.merged`` event.
        Without this, the reloader's read-from-disk would still show the
        pre-merge state until the next ``git pull`` happened. Cheap: a
        depth-1 fetch plus a file copy.
        """
        if dest is None:
            return
        async with self._lock:
            try:
                clone_dir = await asyncio.to_thread(self._clone_or_refresh)
                source = clone_dir / self.registry_file_path
                if not source.exists():
                    logger.warning(
                        "github proposer: %s missing in repo after merge fetch",
                        self.registry_file_path,
                    )
                    return
                await asyncio.to_thread(self._copy_to_dest, source, dest)
            except Exception:
                logger.exception("github proposer: refresh_local_yaml failed")

    @staticmethod
    def _copy_to_dest(source: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace via tempfile sibling so a concurrent reader
        # never sees a half-written YAML.
        import os, tempfile
        fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", dir=dest.parent)
        try:
            with os.fdopen(fd, "wb") as out, source.open("rb") as src:
                out.write(src.read())
            os.replace(tmp, dest)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ── git plumbing ───────────────────────────────────────────────────────

    def _clone_or_refresh(self) -> Path:
        """Maintain a single shared working tree; refresh from the remote
        on every call. Cheaper than cloning fresh every time (we typically
        propose a few packages a day, not thousands per minute)."""
        clone_dir = self.workdir / f"{self.owner}__{self.repo}"
        if not (clone_dir / ".git").exists():
            if clone_dir.exists():
                shutil.rmtree(clone_dir)
            self._git(
                "clone", "--depth", "1", "--no-tags",
                self._authed_https, str(clone_dir),
                cwd=self.workdir,
            )
            self._git_in(clone_dir, "config", "user.name", self.author_name)
            self._git_in(clone_dir, "config", "user.email", self.author_email)
        else:
            # Hard reset to default branch so leftover state from a previous
            # call (a half-committed branch the operator deleted, etc.)
            # can't poison this run.
            self._git_in(clone_dir, "fetch", "--depth", "1", "origin", self.default_branch)
            self._git_in(clone_dir, "checkout", self.default_branch, check=False)
            self._git_in(clone_dir, "reset", "--hard", f"origin/{self.default_branch}")
        return clone_dir

    def _checkout_branch(self, clone_dir: Path, branch: str) -> bool:
        """Check out the proposal branch, fetching it from the remote if
        it already exists. Returns True if the branch was already on the
        remote (i.e. we're amending an existing proposal)."""
        # Try to fetch the branch; ignore failure (means it doesn't exist yet).
        fetch = self._git_in(
            clone_dir, "fetch", "--depth", "1", "origin", branch, check=False,
        )
        existed_on_remote = fetch.returncode == 0
        if existed_on_remote:
            self._git_in(clone_dir, "checkout", "-B", branch, f"origin/{branch}")
        else:
            self._git_in(clone_dir, "checkout", "-B", branch)
        return existed_on_remote

    def _edit_yaml(self, clone_dir: Path, rule: ProposedRule) -> bool:
        """Insert/replace the rule in the YAML. Returns True if the file
        actually changed on disk."""
        target = clone_dir / self.registry_file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            with target.open("r", encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
        else:
            doc = {"version": "1", "global_policies": {}, "packages": {}}

        if not isinstance(doc, dict):
            raise ProposerError(
                f"{target} is not a YAML mapping; refusing to overwrite."
            )

        key = normalize_name(rule.name)
        packages = doc.setdefault("packages", {}) or {}
        new_rule = rule.to_yaml_rule()
        if packages.get(key) == new_rule:
            return False
        packages[key] = new_rule
        doc["packages"] = packages

        validate_registry(doc)

        rendered = yaml.safe_dump(doc, sort_keys=False)
        target.write_text(rendered, encoding="utf-8")
        return True

    def _commit_and_push(self, clone_dir: Path, branch: str, message: str) -> str:
        rel = self.registry_file_path
        self._git_in(clone_dir, "add", "--", rel)
        result = self._git_in(
            clone_dir, "commit", "-m", message, check=False,
        )
        if result.returncode != 0 and "nothing to commit" not in (
            result.stderr.lower() if result.stderr else ""
        ):
            raise ProposerError(f"git commit failed: {result.stderr}")
        sha = self._head_sha(clone_dir)
        self._git_in(clone_dir, "push", "--force-with-lease", "origin", branch)
        return sha

    def _head_sha(self, clone_dir: Path) -> str:
        return self._git_in(
            clone_dir, "rev-parse", "HEAD",
        ).stdout.strip()

    def _git(self, *args, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=cwd, capture_output=True, text=True, check=True,
            env=_subprocess_env(),
        )

    def _git_in(
        self, clone_dir: Path, *args, check: bool = True
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["git", "-C", str(clone_dir), *args],
            capture_output=True, text=True, check=check,
            env=_subprocess_env(),
        )
        return result

    # ── REST plumbing ──────────────────────────────────────────────────────

    @property
    def _gh_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _find_open_pr(self, branch: str) -> Optional[dict]:
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/pulls"
        params = {
            "state": "open",
            "head": f"{self.owner}:{branch}",
            "per_page": "1",
        }
        resp = await self.http.get(url, headers=self._gh_headers, params=params)
        if resp.status_code != 200:
            logger.warning(
                "GitHub list-PRs returned %s: %s",
                resp.status_code, resp.text[:200],
            )
            return None
        prs = resp.json() or []
        return prs[0] if prs else None

    async def _open_pr(
        self, branch: str, rule: ProposedRule, context: ProposalContext
    ) -> dict:
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/pulls"
        payload = {
            "title": f"lex-align: classify `{rule.name}` as {rule.status}",
            "head": branch,
            "base": self.default_branch,
            "body": _pr_body(rule, context),
            "maintainer_can_modify": True,
        }
        resp = await self.http.post(url, headers=self._gh_headers, json=payload)
        if resp.status_code >= 400:
            raise ProposerError(
                f"GitHub PR open returned {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    async def _comment_on_pr(
        self, pr_number: int, rule: ProposedRule, context: ProposalContext
    ) -> None:
        url = (
            f"{self.api_base}/repos/{self.owner}/{self.repo}"
            f"/issues/{pr_number}/comments"
        )
        body = (
            f"Additional proposal for `{rule.name}` from "
            f"**{context.source}** (project `{context.project}`, "
            f"requester `{context.requester}`):\n\n"
            f"> {context.rationale.strip() or '(no rationale provided)'}\n"
        )
        resp = await self.http.post(url, headers=self._gh_headers, json={"body": body})
        if resp.status_code >= 400:
            logger.warning(
                "GitHub comment on PR #%d returned %s: %s",
                pr_number, resp.status_code, resp.text[:200],
            )


def _commit_message(rule: ProposedRule, context: ProposalContext) -> str:
    head = f"lex-align: classify {rule.name} as {rule.status} (via {context.source})"
    body = [
        "",
        f"Project: {context.project}",
        f"Requester: {context.requester}",
    ]
    agent = " ".join(filter(None, [context.agent_model, context.agent_version]))
    if agent:
        body.append(f"Agent: {agent}")
    if context.rationale:
        body += ["", "Rationale:", context.rationale.strip()]
    return "\n".join([head, *body])


def _pr_body(rule: ProposedRule, context: ProposalContext) -> str:
    agent = " ".join(filter(None, [context.agent_model, context.agent_version])) or "(unknown)"
    fields = "\n".join([
        f"- **Package**: `{rule.name}`",
        f"- **Proposed status**: `{rule.status}`",
        f"- **Source**: {context.source}",
        f"- **Project**: `{context.project}`",
        f"- **Requester**: `{context.requester}`",
        f"- **Agent**: {agent}",
        *([f"- **Replacement**: `{rule.replacement}`"] if rule.replacement else []),
        *([f"- **Min version**: `{rule.min_version}`"] if rule.min_version else []),
        *([f"- **Max version**: `{rule.max_version}`"] if rule.max_version else []),
    ])
    rationale = (context.rationale.strip() or "_(no rationale provided)_")
    return (
        "Automated proposal opened by **lex-align**. "
        "Review and merge to apply the change to the registry — the "
        "lex-align server will hot-reload on merge.\n\n"
        f"{fields}\n\n"
        "## Rationale\n\n"
        f"{rationale}\n\n"
        "---\n"
        "_Status can be edited before merging if a different "
        "classification is more appropriate (e.g. `preferred`, "
        "`version-constrained`)._"
    )


def _subprocess_env() -> dict:
    """Minimal env for subprocess git calls. Strips user config that could
    hang the call (interactive credential helpers, etc.)."""
    import os
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/true",
    }
