"""Resolve the configured proposer for the running server.

Called once from the FastAPI lifespan. The result is stored on
``app.state.lex.proposer`` and reused for every approval request and
every dashboard "Approve" click.

Auto-detection lets most operators leave ``REGISTRY_PROPOSER`` unset:

  1. ``REGISTRY_REPO_URL`` set                   → ``github`` (the only
                                                    remote backend
                                                    implemented today).
  2. ``REGISTRY_PATH`` is inside a git working   → ``local_git``.
     tree.
  3. ``REGISTRY_PATH`` set, parent dir writable  → ``local_file``.
  4. Nothing configured                          → ``log_only``, with a
                                                    startup warning.
"""

from __future__ import annotations

import importlib
import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from .base import Proposer, ProposerError
from .github import GitHubProposer
from .local_file import LocalFileProposer
from .local_git import LocalGitProposer
from .log_only import LogOnlyProposer


if TYPE_CHECKING:
    from ..config import Settings


logger = logging.getLogger(__name__)


BUILTIN_BACKENDS = {"log_only", "local_file", "local_git", "github"}


def load_proposer(
    settings: "Settings", http_client: httpx.AsyncClient
) -> Proposer:
    explicit = (settings.registry_proposer or "").strip()
    backend = explicit or _autodetect(settings)
    logger.info("registry proposer: backend=%s (explicit=%s)", backend, bool(explicit))

    if backend == "log_only":
        return LogOnlyProposer()

    if backend == "local_file":
        if settings.registry_path is None:
            raise ValueError(
                "REGISTRY_PROPOSER=local_file requires REGISTRY_PATH."
            )
        return LocalFileProposer(settings.registry_path)

    if backend == "local_git":
        if settings.registry_path is None:
            raise ValueError(
                "REGISTRY_PROPOSER=local_git requires REGISTRY_PATH."
            )
        return LocalGitProposer(
            settings.registry_path,
            author_name=settings.registry_bot_author_name,
            author_email=settings.registry_bot_author_email,
        )

    if backend == "github":
        return GitHubProposer(
            repo_url=settings.registry_repo_url or "",
            registry_file_path=settings.registry_file_path or "registry.yml",
            token=settings.registry_repo_token or "",
            http_client=http_client,
            workdir=Path(settings.registry_repo_workdir),
            api_base=settings.github_api_base,
            default_branch=settings.registry_default_branch,
            author_name=settings.registry_bot_author_name,
            author_email=settings.registry_bot_author_email,
        )

    if ":" in backend:
        return _load_module_proposer(backend, settings, http_client)

    raise ValueError(
        f"Unknown REGISTRY_PROPOSER={backend!r}. Built-ins: "
        f"{sorted(BUILTIN_BACKENDS)}, or 'module.path:ClassName'."
    )


def _autodetect(settings: "Settings") -> str:
    if settings.registry_repo_url:
        return "github"
    path = settings.registry_path
    if path is not None:
        # Check whether the path (or its parent) is inside a git working tree.
        # We don't need the path to exist yet — only its parent must.
        candidate = path if path.exists() else path.parent
        if candidate.exists() and _is_git_working_tree(candidate):
            return "local_git"
        if candidate.exists() and _is_writable(candidate if candidate.is_dir() else candidate.parent):
            return "local_file"
    logger.warning(
        "no registry write target detected (REGISTRY_REPO_URL unset, "
        "REGISTRY_PATH absent or read-only); proposer falls back to log_only."
    )
    return "log_only"


def _is_git_working_tree(path: Path) -> bool:
    if not shutil.which("git"):
        return False
    try:
        subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _is_writable(path: Path) -> bool:
    import os
    try:
        return os.access(path, os.W_OK)
    except OSError:
        return False


def _load_module_proposer(
    spec: str, settings: "Settings", http_client: httpx.AsyncClient
) -> Proposer:
    try:
        module_path, _, class_name = spec.partition(":")
        if not module_path or not class_name:
            raise ValueError(f"Expected 'module:Class', got {spec!r}")
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
    except (ImportError, AttributeError, ValueError) as exc:
        raise ValueError(
            f"Could not load REGISTRY_PROPOSER={spec!r}: {exc}. "
            "The module must be importable from the server's PYTHONPATH "
            "and the class must subclass "
            "lex_align_server.proposer.Proposer."
        ) from exc

    instance = cls(settings=settings, http_client=http_client)
    if not isinstance(instance, Proposer):
        raise TypeError(
            f"{spec} produced {type(instance).__name__}, which does not "
            "subclass lex_align_server.proposer.Proposer."
        )
    return instance
