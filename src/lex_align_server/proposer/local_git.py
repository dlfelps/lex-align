"""Local-git proposer — commits to a working tree, no remote, no PR.

Same flow as ``local_file`` but with a ``git commit`` after each write
so the operator gets ``git log`` as the audit trail. Useful for teams
that want history without standing up a GitHub/GitLab integration.

We shell out to the system ``git`` rather than pulling in a Python git
library — keeps the dependency surface small and the operator's normal
git tooling (signed commits, hooks, config) keeps working unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Optional

from .base import (
    ProposalContext,
    ProposalResult,
    ProposedRule,
    Proposer,
    ProposerError,
)
from .local_file import LocalFileProposer


logger = logging.getLogger(__name__)


class LocalGitProposer(Proposer):
    backend_name = "local_git"

    def __init__(
        self,
        registry_path: Path,
        *,
        author_name: str = "lex-align bot",
        author_email: str = "lex-align@localhost",
    ):
        self._file = LocalFileProposer(registry_path)
        self.path = registry_path
        self.author_name = author_name
        self.author_email = author_email
        if not shutil.which("git"):
            raise ProposerError(
                "REGISTRY_PROPOSER=local_git requires the `git` binary on PATH."
            )

    async def propose(
        self, rule: ProposedRule, context: ProposalContext
    ) -> ProposalResult:
        # Write through the local-file proposer's atomic-write logic first,
        # then take a git commit. If the write fails the git step is skipped
        # and the caller sees the underlying ProposerError.
        file_result = await self._file.propose(rule, context)

        commit_msg = self._commit_message(rule, context)
        try:
            sha = await asyncio.to_thread(self._commit, commit_msg)
        except ProposerError:
            raise
        except Exception as exc:
            raise ProposerError(f"git commit failed: {exc}") from exc

        return ProposalResult(
            backend=self.backend_name,
            status="applied",
            url=f"file://{self.path}",
            commit_sha=sha,
            detail=(
                f"Wrote {rule.name} and committed as {sha[:8]}. "
                "The atomic file replace will trigger the registry reload "
                "watcher; you can also POST /api/v1/registry/reload to force it."
            ),
        )

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _commit_message(rule: ProposedRule, context: ProposalContext) -> str:
        agent = " ".join(filter(None, [context.agent_model, context.agent_version]))
        head = (
            f"lex-align: {rule.name} → {rule.status} "
            f"(via {context.source})"
        )
        body_lines = [
            "",
            f"Project: {context.project}",
            f"Requester: {context.requester}",
        ]
        if agent:
            body_lines.append(f"Agent: {agent}")
        if rule.replacement:
            body_lines.append(f"Replacement: {rule.replacement}")
        if rule.min_version or rule.max_version:
            body_lines.append(
                f"Version constraint: "
                f"{rule.min_version or ''}..{rule.max_version or ''}"
            )
        if context.rationale:
            body_lines += ["", "Rationale:", context.rationale.strip()]
        return "\n".join([head, *body_lines])

    def _commit(self, message: str) -> str:
        import subprocess
        repo = self._git_root()
        # Stage only our file; we never assume the operator wants other
        # modified files in the working tree to ride along.
        rel = self.path.resolve().relative_to(repo)
        subprocess.check_call(["git", "-C", str(repo), "add", "--", str(rel)])
        env = {
            **_minimal_env(),
            "GIT_AUTHOR_NAME": self.author_name,
            "GIT_AUTHOR_EMAIL": self.author_email,
            "GIT_COMMITTER_NAME": self.author_name,
            "GIT_COMMITTER_EMAIL": self.author_email,
        }
        # Use --allow-empty-message=False default; if the file content didn't
        # actually change (re-proposing the same rule) we'd hit "nothing to
        # commit" — treat that as a no-op success.
        result = subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", message],
            env=env, capture_output=True, text=True,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").lower()
            if "nothing to commit" in stderr or "no changes added" in stderr:
                # Idempotent: rule already matches. Return the current HEAD.
                return self._head_sha(repo)
            raise ProposerError(
                f"git commit failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        return self._head_sha(repo)

    def _git_root(self) -> Path:
        import subprocess
        try:
            out = subprocess.check_output(
                ["git", "-C", str(self.path.parent), "rev-parse", "--show-toplevel"],
                text=True, env=_minimal_env(),
            ).strip()
        except subprocess.CalledProcessError as exc:
            raise ProposerError(
                f"{self.path} is not inside a git working tree; either "
                "`git init` the parent directory or set "
                "REGISTRY_PROPOSER=local_file."
            ) from exc
        return Path(out)

    def _head_sha(self, repo: Path) -> str:
        import subprocess
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            text=True, env=_minimal_env(),
        ).strip()


def _minimal_env() -> dict:
    """Strip the git env down to PATH so user-level config can't leak in
    (e.g. an interactive credential helper that would hang the request)."""
    import os
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "GIT_TERMINAL_PROMPT": "0",
    }
