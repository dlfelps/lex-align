"""Direct-write proposer — edits ``REGISTRY_PATH`` in place.

Best fit for single-user mode and small teams without a git host
integration. The flow is:

  1. Read the current YAML (creating an empty registry if absent).
  2. Insert / replace the package rule.
  3. Validate the merged document via ``validate_registry`` so an
     invalid proposal can never produce a corrupt file on disk.
  4. Write atomically (write-to-temp + rename).
  5. Recompile and reload the in-memory ``Registry`` so the next
     ``/evaluate`` call sees the change immediately.

There is no review step — the dashboard / agent's request *is* the
authorization. Use ``local_git`` if you want a `git log` audit trail,
or the GitHub backend if you want PR review.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

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


class LocalFileProposer(Proposer):
    backend_name = "local_file"

    def __init__(self, registry_path: Path):
        if not registry_path:
            raise ValueError(
                "LocalFileProposer requires REGISTRY_PATH to point at the "
                "registry YAML."
            )
        self.path = registry_path
        # Single in-process lock — concurrent writers would race on the
        # read-modify-write. Operators running multiple replicas need the
        # GitHub backend instead (its remote is the synchronization point).
        self._lock = asyncio.Lock()

    async def propose(
        self, rule: ProposedRule, context: ProposalContext
    ) -> ProposalResult:
        async with self._lock:
            try:
                doc = await asyncio.to_thread(self._read_or_init)
                key = normalize_name(rule.name)
                packages = doc.setdefault("packages", {}) or {}
                pre_existing = key in packages
                packages[key] = rule.to_yaml_rule()
                doc["packages"] = packages

                # Validate the *whole* document so we can't write a corrupt
                # registry by overwriting a previously-valid file.
                validate_registry(doc)

                await asyncio.to_thread(self._atomic_write, doc)
            except ValidationError as exc:
                raise ProposerError(f"Proposed rule failed validation: {exc}") from exc
            except OSError as exc:
                raise ProposerError(f"Failed writing {self.path}: {exc}") from exc

        logger.info(
            "[local-file proposer] %s %s as %s in %s (source=%s, requester=%s)",
            "updated" if pre_existing else "added",
            key, rule.status, self.path, context.source, context.requester,
        )
        return ProposalResult(
            backend=self.backend_name,
            status="applied",
            url=f"file://{self.path}",
            detail=(
                f"Wrote {key} to {self.path}. Reload picks it up on the next "
                "registry refresh tick (or call POST /api/v1/registry/reload)."
            ),
        )

    # ── helpers ────────────────────────────────────────────────────────────

    def _read_or_init(self) -> dict:
        if not self.path.exists():
            return {
                "version": "1",
                "global_policies": {},
                "packages": {},
            }
        with self.path.open("r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        if not isinstance(doc, dict):
            raise ProposerError(
                f"{self.path} did not parse as a YAML mapping; refusing to overwrite."
            )
        return doc

    def _atomic_write(self, doc: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling temp file then rename — POSIX rename is atomic
        # on the same filesystem, so a reader (the file watcher) never
        # observes a half-written YAML.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(doc, f, sort_keys=False)
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
