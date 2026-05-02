"""Pluggable registry-update proposers.

When an agent calls ``request-approval`` or an operator clicks "Approve"
on the dashboard, the server emits a *proposal* to update
``registry.yml`` with the new (or amended) package rule. How that
proposal lands depends entirely on the configured proposer:

* ``log_only`` — log it; don't touch anything. Safe default for evaluation
  / read-only demos. Selected when no registry write target is configured.
* ``local_file`` — write directly to ``REGISTRY_PATH``, recompile, reload
  in-memory. Best fit for single-user mode and local evaluation.
* ``local_git`` — commit to a local git working tree (no remote, no PR).
  Useful when the operator wants the audit trail of git history without
  hosting a remote.
* ``github`` — clone, branch, push, open a PR against the configured
  remote (``REGISTRY_REPO_URL``). The merge webhook on
  ``/api/v1/registry/webhook`` triggers the in-memory reload.
* ``module:path:Class`` — escape hatch for custom CI/CD portals.

Selection happens via ``load_proposer(settings, http_client)`` — the
loader auto-detects from existing settings, so ``REGISTRY_PROPOSER`` is
optional. See ``proposer/loader.py`` for the precedence rules.

The dependency surface for endpoints (``ProposedRule``,
``ProposalContext``, ``ProposalResult``) is stable across backends; the
endpoint code never branches on which proposer is active.
"""

from __future__ import annotations

from .base import (
    ProposalContext,
    ProposalResult,
    ProposedRule,
    Proposer,
    ProposerError,
)
from .loader import load_proposer

__all__ = [
    "ProposalContext",
    "ProposalResult",
    "ProposedRule",
    "Proposer",
    "ProposerError",
    "load_proposer",
]
