"""Registry hot-reload coordinator.

Three triggers all funnel through ``reload_registry``:

* the GitHub webhook handler (``POST /api/v1/registry/webhook``) on PR
  merge — it pulls the merged YAML to disk first, then calls in;
* the operator's manual ``POST /api/v1/registry/reload`` fallback for
  when the webhook gets lost;
* the periodic ``RegistryPoller`` that watches ``REGISTRY_PATH`` mtime
  every ``REGISTRY_RELOAD_INTERVAL`` seconds.

The reload is **atomic**: in-flight ``/evaluate`` calls finish against
the old ``Registry``; the next call sees the new one. We also flip
matching ``PENDING_REVIEW`` approval rows to ``APPROVED`` for every
package the new registry now contains — so the dashboard's pending
queue auto-trims when a PR merges or a YAML edit goes through.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import yaml

from .registry import Registry, normalize_name
from .registry_schema import ValidationError, validate_registry


if TYPE_CHECKING:
    from .audit import AuditStore
    from .state import AppState


logger = logging.getLogger(__name__)


@dataclass
class ReloadResult:
    """Outcome of a reload attempt; surfaced through the manual reload
    endpoint and recorded in the server log."""
    ok: bool
    previous_version: Optional[str] = None
    new_version: Optional[str] = None
    package_count: int = 0
    added_packages: int = 0
    approved_requests: int = 0
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "previous_version": self.previous_version,
            "new_version": self.new_version,
            "package_count": self.package_count,
            "added_packages": self.added_packages,
            "approved_requests": self.approved_requests,
            "detail": self.detail,
        }


async def reload_registry(state: "AppState") -> ReloadResult:
    """Re-read ``REGISTRY_PATH`` from disk, validate, and atomically swap
    the in-memory ``Registry``. Idempotent: a no-op when the file hasn't
    changed.

    Caller is responsible for putting the latest YAML on disk (e.g. the
    webhook handler runs ``git pull`` first).
    """
    path = state.settings.registry_path
    if path is None:
        return ReloadResult(
            ok=False,
            detail="REGISTRY_PATH is not configured; nothing to reload.",
        )
    if not path.exists():
        return ReloadResult(
            ok=False,
            detail=f"REGISTRY_PATH {path} does not exist.",
        )

    try:
        doc = await asyncio.to_thread(_read_yaml, path)
        compiled = validate_registry(doc)
    except ValidationError as exc:
        logger.error("registry reload rejected (validation): %s", exc)
        return ReloadResult(
            ok=False,
            detail=f"Validation failed; keeping previous registry: {exc}",
        )
    except Exception as exc:
        logger.exception("registry reload failed reading %s", path)
        return ReloadResult(
            ok=False,
            detail=f"Failed reading {path}: {exc}",
        )

    new_registry = Registry.from_dict(compiled, source_path=path)
    previous = state.registry
    previous_keys = set(previous.packages.keys()) if previous else set()
    new_keys = set(new_registry.packages.keys())
    added = new_keys - previous_keys

    # Atomic swap. Per the GIL, a single attribute assignment is atomic;
    # any concurrent reader of ``state.registry`` either sees the old or
    # the new object, never a half-built one. We don't bother with a lock.
    state.registry = new_registry

    approved_total = 0
    if added:
        for normalized in added:
            approved_total += await state.audit.mark_approved_by_package(normalized)

    logger.info(
        "registry reloaded: version %s → %s, packages %d → %d "
        "(added %d, auto-approved %d pending requests)",
        previous.version if previous else None, new_registry.version,
        len(previous_keys), len(new_keys), len(added), approved_total,
    )
    return ReloadResult(
        ok=True,
        previous_version=previous.version if previous else None,
        new_version=new_registry.version,
        package_count=len(new_keys),
        added_packages=len(added),
        approved_requests=approved_total,
        detail="ok",
    )


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, dict):
        raise ValidationError(f"{path} is not a YAML mapping")
    return doc


# ── periodic poller ────────────────────────────────────────────────────────


class RegistryPoller:
    """Background task that reloads on ``REGISTRY_PATH`` mtime change.

    Cheap: a stat() per tick, recompile only when mtime advances. The
    webhook is the primary reload signal in production; this exists as
    a backstop for missed webhooks and for local-file / local-git modes
    where there is no webhook at all.
    """

    def __init__(self, state: "AppState"):
        self.state = state
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._last_mtime: Optional[float] = None

    def start(self) -> None:
        interval = self.state.settings.registry_reload_interval
        if interval <= 0:
            logger.info("registry poller disabled (REGISTRY_RELOAD_INTERVAL=0)")
            return
        if self.state.settings.registry_path is None:
            logger.info("registry poller disabled (REGISTRY_PATH unset)")
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _run(self) -> None:
        interval = self.state.settings.registry_reload_interval
        path: Path = self.state.settings.registry_path  # type: ignore[assignment]
        while not self._stop.is_set():
            try:
                if path.exists():
                    mtime = path.stat().st_mtime
                    if self._last_mtime is None:
                        # First tick: don't reload, just remember.
                        self._last_mtime = mtime
                    elif mtime > self._last_mtime:
                        logger.info("registry file mtime advanced; reloading")
                        result = await reload_registry(self.state)
                        if result.ok:
                            self._last_mtime = mtime
            except Exception:
                logger.exception("registry poller tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
