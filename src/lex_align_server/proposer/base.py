"""Proposer contract.

The dataclasses are deliberately small and serializable so dashboard
responses can carry them verbatim without an additional translation
layer. Custom backends only need to subclass :class:`Proposer` and
implement ``propose``; ``close`` is a no-op default for proposers
that don't keep persistent state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


# Status values mirror what the YAML schema accepts. We re-list them here
# rather than importing from registry_schema because the proposer module
# is also imported during config validation, and we'd like to avoid a
# circular dependency.
VALID_STATUSES = (
    "preferred", "approved", "deprecated", "version-constrained", "banned",
)


@dataclass(frozen=True)
class ProposedRule:
    """The package classification an agent or operator wants in the registry.

    ``name`` is the raw display form (``Some-Pkg``); the proposer
    normalizes it before writing to YAML. Optional fields mirror the
    YAML schema 1:1.
    """
    name: str
    status: str
    reason: Optional[str] = None
    replacement: Optional[str] = None
    min_version: Optional[str] = None
    max_version: Optional[str] = None

    def to_yaml_rule(self) -> dict:
        """Render to the dict shape the YAML schema expects."""
        out: dict = {"status": self.status}
        if self.reason:      out["reason"] = self.reason
        if self.replacement: out["replacement"] = self.replacement
        if self.min_version: out["min_version"] = self.min_version
        if self.max_version: out["max_version"] = self.max_version
        return out


@dataclass(frozen=True)
class ProposalContext:
    """Who is asking for the change and why.

    ``source`` is ``"agent"`` for the agent's ``request-approval`` flow
    and ``"operator"`` when the dashboard is the originator. Both values
    end up in commit messages / PR bodies so reviewers can tell at a
    glance whether the change came from a human triaging the queue or
    from an agent running unsupervised.
    """
    source: str  # "agent" | "operator"
    project: str
    requester: str
    rationale: str
    agent_model: Optional[str] = None
    agent_version: Optional[str] = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class ProposalResult:
    """Outcome surfaced back to the API caller.

    ``status`` values:
      * ``opened`` — a fresh PR / commit / file write was created.
      * ``amended`` — an existing open proposal for this package was
        updated (e.g. the agent re-requested with a new rationale).
      * ``applied`` — the change is live (local-file / local-git
        backends, or a webhook-driven reload after merge).
      * ``logged`` — log-only backend; no durable change happened.
      * ``failed`` — proposer hit an error. ``detail`` describes it.
    """
    backend: str
    status: str
    url: Optional[str] = None
    branch: Optional[str] = None
    commit_sha: Optional[str] = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "status": self.status,
            "url": self.url,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "detail": self.detail,
        }


class ProposerError(Exception):
    """Proposer hit a recoverable error. Endpoint code wraps this as a
    :class:`ProposalResult` with ``status="failed"`` so a transient git
    outage doesn't cascade into an HTTP 5xx for the agent."""


class Proposer(ABC):
    """Resolve a :class:`ProposedRule` into a registry change.

    Implementations must be re-entrant — multiple requests for the same
    package collapse onto the same branch/PR via the backend's own
    idempotency rules, but the framework doesn't serialize calls.
    """

    backend_name: str = "abstract"

    @abstractmethod
    async def propose(
        self, rule: ProposedRule, context: ProposalContext
    ) -> ProposalResult:
        ...

    async def close(self) -> None:
        """Optional cleanup. Override for proposers that hold long-lived
        resources (open file handles, working trees, etc.)."""
        return None
