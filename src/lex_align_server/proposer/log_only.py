"""Log-only proposer — does nothing durable.

Selected automatically when no registry write target is configured
(neither ``REGISTRY_REPO_URL`` nor a writable ``REGISTRY_PATH``).
Useful for read-only demos and for the "is this thing even talking to
my server?" smoke test phase of evaluation. The dashboard's "Approve"
button still works — it just won't make the change stick.
"""

from __future__ import annotations

import logging

from .base import ProposalContext, ProposalResult, ProposedRule, Proposer


logger = logging.getLogger(__name__)


class LogOnlyProposer(Proposer):
    backend_name = "log_only"

    async def propose(
        self, rule: ProposedRule, context: ProposalContext
    ) -> ProposalResult:
        logger.info(
            "[log-only proposer] would have proposed %s as %s "
            "(source=%s, project=%s, requester=%s, agent=%s/%s): %s",
            rule.name, rule.status, context.source, context.project,
            context.requester, context.agent_model or "?",
            context.agent_version or "?", context.rationale,
        )
        return ProposalResult(
            backend=self.backend_name,
            status="logged",
            detail=(
                "No registry write target configured (REGISTRY_PROPOSER=log_only). "
                "Set REGISTRY_PATH for local-file mode or REGISTRY_REPO_URL for "
                "the GitHub mode."
            ),
        )
