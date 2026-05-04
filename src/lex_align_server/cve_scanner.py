"""Background CVE re-scan scheduler.

Walks every package in the live registry on a fixed cadence, re-queries
OSV via :func:`lex_align_server.cve.resolve_cves`, and writes a
``CVE_ALERT`` row to the audit log whenever the package's max CVSS now
crosses ``GlobalPolicies.cve_threshold``.

The scanner is **alert-only**. It does not flip registry entries to
``banned`` or otherwise change verdict policy — that decision belongs
to the operator. The dashboard and the client's ``status`` command
surface the alerts via the existing ``security_report`` endpoint.

Lifecycle mirrors ``RegistryPoller``: ``start()`` schedules an asyncio
task on the running loop, ``stop()`` drains it. Wired into the FastAPI
``lifespan`` so the task starts and stops with the server.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import TYPE_CHECKING, Optional

from .cve import resolve_cves


if TYPE_CHECKING:
    from .state import AppState


logger = logging.getLogger(__name__)


class CveScanner:
    """Periodic OSV re-scan of every registered package.

    The first scan runs after the configured interval, not at startup —
    server bring-up should not block on a wave of OSV requests, and the
    ``/evaluate`` path already covers fresh additions.
    """

    def __init__(self, state: "AppState"):
        self.state = state
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._last_run_at: Optional[datetime.datetime] = None
        self._last_alert_count: int = 0
        self._last_packages_scanned: int = 0

    @property
    def last_run_at(self) -> Optional[datetime.datetime]:
        return self._last_run_at

    @property
    def last_alert_count(self) -> int:
        return self._last_alert_count

    def start(self) -> None:
        interval_hours = self.state.settings.cve_scan_interval_hours
        if interval_hours <= 0:
            logger.info(
                "cve scanner disabled (cve_scan_interval_hours=%s)",
                interval_hours,
            )
            return
        logger.info(
            "cve scanner started (interval=%.2fh)", interval_hours,
        )
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        interval_seconds = self.state.settings.cve_scan_interval_hours * 3600
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=interval_seconds
                )
                # _stop was signalled while we slept — exit cleanly.
                break
            except asyncio.TimeoutError:
                pass
            try:
                await self.scan_once()
            except Exception:
                logger.exception("cve scanner: scan tick failed")

    async def scan_once(self) -> int:
        """Run one scan pass over the live registry.

        Returns the number of CVE_ALERT rows written. Public so tests
        and the operator's manual reload paths can drive it.
        """
        registry = self.state.registry
        if registry is None:
            logger.debug("cve scanner: no registry loaded; skipping")
            return 0
        packages = list(registry.packages.items())
        if not packages:
            return 0

        threshold_score = registry.global_policies.cve_threshold * 10.0
        alerts = 0
        scanned = 0
        for normalized_name, rule in packages:
            if self._stop.is_set():
                break
            try:
                info = await resolve_cves(
                    normalized_name,
                    None,  # scan latest known versions
                    self.state.cache,
                    self.state.settings.cve_cache_ttl,
                    self.state.settings.osv_api_url,
                    self.state.http,
                )
            except Exception:
                logger.exception(
                    "cve scanner: lookup failed for %s", normalized_name,
                )
                continue
            scanned += 1
            if info.max_score is None or not info.ids:
                continue
            if info.max_score < threshold_score:
                continue
            await self.state.audit.record_cve_alert(
                package=normalized_name,
                cve_ids=info.ids,
                max_cvss=info.max_score,
                registry_status=(
                    rule.status.value if rule.status is not None else None
                ),
            )
            alerts += 1
            logger.warning(
                "cve scanner: %s now exceeds threshold "
                "(max_cvss=%.1f, ids=%s)",
                normalized_name, info.max_score, info.ids[:3],
            )

        self._last_run_at = datetime.datetime.now(tz=datetime.timezone.utc)
        self._last_alert_count = alerts
        self._last_packages_scanned = scanned
        logger.info(
            "cve scanner: scan complete (packages=%d, alerts=%d)",
            scanned, alerts,
        )
        return alerts
