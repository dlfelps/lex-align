"""Tests for the background CVE re-scan scheduler.

The scanner walks every package in the live registry on a fixed cadence,
re-queries OSV, and writes a CVE_ALERT row to the audit log when the
package's max CVSS now crosses the configured threshold. It must NOT
auto-deny — the operator decides what to do with an alert.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from lex_align_server.audit import (
    AuditStore,
    DENIAL_CVE_ALERT,
    VERDICT_CVE_ALERT,
)
from lex_align_server.cache import JsonCache
from lex_align_server.config import Settings
from lex_align_server.cve_scanner import CveScanner
from lex_align_server.registry import (
    GlobalPolicies,
    PackageRule,
    PackageStatus,
    Registry,
)
from lex_align_server.state import AppState


class _MemCache(JsonCache):
    def __init__(self) -> None:  # type: ignore[no-untyped-def]
        self._store: dict = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ttl_seconds):
        self._store[key] = value

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def _registry_with(packages: dict[str, PackageRule], threshold: float = 0.7) -> Registry:
    """Build a registry whose policies use the given CVSS-fraction threshold."""
    return Registry(
        version="1",
        global_policies=GlobalPolicies(cve_threshold=threshold),
        packages=packages,
    )


def _osv_handler(by_package: dict[str, dict]):
    """Return a MockTransport handler that serves canned OSV responses
    keyed by lowercase package name."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        # Crude but adequate: pull the package name out of the JSON body.
        for name, payload in by_package.items():
            if f'"{name}"'.encode() in body:
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"vulns": []})

    return handler


async def _build_state(
    tmp_path,
    *,
    registry: Registry,
    handler,
    settings_overrides: dict | None = None,
) -> tuple[AppState, httpx.AsyncClient]:
    settings_kwargs = dict(
        database_path=tmp_path / "audit.sqlite",
        osv_api_url="https://osv.test/v1/query",
        cve_cache_ttl=60,
        cve_scan_interval_hours=24.0,
    )
    if settings_overrides:
        settings_kwargs.update(settings_overrides)
    settings = Settings(**settings_kwargs)
    audit = AuditStore(settings.database_path)
    await audit.init()
    cache = _MemCache()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    state = AppState(
        settings=settings,
        cache=cache,
        audit=audit,
        http=client,
        registry=registry,
        authenticator=None,  # type: ignore[arg-type]
        proposer=None,       # type: ignore[arg-type]
    )
    return state, client


@pytest.mark.asyncio
async def test_record_cve_alert_writes_distinct_audit_row(tmp_path):
    """A CVE_ALERT row uses ``DENIAL_CVE_ALERT`` so it doesn't pollute
    the existing CVE-denial rollup, but stays in the same audit_log
    table so the dashboard's event stream is uniform."""
    audit = AuditStore(tmp_path / "audit.sqlite")
    await audit.init()
    rec_id = await audit.record_cve_alert(
        package="redis",
        cve_ids=["CVE-9999-0001"],
        max_cvss=9.4,
        registry_status="approved",
    )
    assert rec_id

    sec = await audit.security_report()
    # Alerts must NOT count as denials — the operator decides what to do.
    assert sec["total_denials"] == 0
    assert len(sec["cve_alerts"]) == 1
    alert = sec["cve_alerts"][0]
    assert alert["package"] == "redis"
    assert alert["cve_ids"] == ["CVE-9999-0001"]
    assert alert["max_cvss"] == pytest.approx(9.4)
    assert alert["registry_status"] == "approved"


@pytest.mark.asyncio
async def test_recent_cve_alerts_filters_by_project(tmp_path):
    """A project-scoped query only returns alerts written under that
    project so the dashboard can filter by repo."""
    audit = AuditStore(tmp_path / "audit.sqlite")
    await audit.init()
    await audit.record_cve_alert(
        package="redis", cve_ids=["CVE-1"], max_cvss=9.0, project="alpha",
    )
    await audit.record_cve_alert(
        package="numpy", cve_ids=["CVE-2"], max_cvss=9.5, project="beta",
    )

    alpha = await audit.recent_cve_alerts(project="alpha")
    assert [a["package"] for a in alpha] == ["redis"]
    beta = await audit.recent_cve_alerts(project="beta")
    assert [a["package"] for a in beta] == ["numpy"]


@pytest.mark.asyncio
async def test_scan_once_writes_alert_when_threshold_crossed(tmp_path):
    """A registered package whose OSV response now reports a CVSS above
    the policy threshold should produce exactly one CVE_ALERT row."""
    handler = _osv_handler({
        "redis": {
            "vulns": [
                {"id": "GHSA-rrr", "database_specific": {"cvss": {"score": 9.6}}},
            ],
        },
        "click": {"vulns": []},  # clean, must NOT alert
    })
    registry = _registry_with(
        {
            "redis": PackageRule(status=PackageStatus.APPROVED),
            "click": PackageRule(status=PackageStatus.PREFERRED),
        },
        threshold=0.7,
    )

    state, client = await _build_state(tmp_path, registry=registry, handler=handler)
    try:
        scanner = CveScanner(state)
        wrote = await scanner.scan_once()
    finally:
        await client.aclose()

    assert wrote == 1
    sec = await state.audit.security_report()
    pkgs = [a["package"] for a in sec["cve_alerts"]]
    assert pkgs == ["redis"]
    # The alert verdict is distinct from DENIED so we can tell them apart.
    alerts = await state.audit.recent_cve_alerts()
    assert alerts[0]["max_cvss"] == pytest.approx(9.6)


@pytest.mark.asyncio
async def test_scan_once_does_not_alert_below_threshold(tmp_path):
    """A package whose CVSS is below the policy threshold must NOT
    produce a CVE_ALERT row — only events above the line escalate."""
    handler = _osv_handler({
        "redis": {
            "vulns": [
                {"id": "GHSA-low", "database_specific": {"cvss": {"score": 5.0}}},
            ],
        },
    })
    registry = _registry_with(
        {"redis": PackageRule(status=PackageStatus.APPROVED)},
        threshold=0.7,  # 7.0 in absolute CVSS
    )
    state, client = await _build_state(tmp_path, registry=registry, handler=handler)
    try:
        wrote = await CveScanner(state).scan_once()
    finally:
        await client.aclose()
    assert wrote == 0
    assert await state.audit.recent_cve_alerts() == []


@pytest.mark.asyncio
async def test_scan_once_does_not_modify_registry(tmp_path):
    """Alert-only contract: a high-CVSS hit must NOT flip the registry
    rule to BANNED or anything else. Policy decisions stay with the
    operator."""
    handler = _osv_handler({
        "redis": {
            "vulns": [
                {"id": "GHSA-x", "database_specific": {"cvss": {"score": 9.9}}},
            ],
        },
    })
    rule = PackageRule(status=PackageStatus.APPROVED)
    registry = _registry_with({"redis": rule}, threshold=0.7)
    state, client = await _build_state(tmp_path, registry=registry, handler=handler)
    try:
        await CveScanner(state).scan_once()
    finally:
        await client.aclose()
    # Registry contents are untouched; the scanner only writes to the
    # audit log.
    assert state.registry is registry
    assert registry.packages["redis"].status is PackageStatus.APPROVED


@pytest.mark.asyncio
async def test_scan_once_with_no_registry_is_a_noop(tmp_path):
    """Scanner must behave gracefully when REGISTRY_PATH is unset and
    the in-memory registry is None."""
    settings = Settings(
        database_path=tmp_path / "audit.sqlite",
        osv_api_url="https://osv.test/v1/query",
    )
    audit = AuditStore(settings.database_path)
    await audit.init()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_osv_handler({})),
    )
    state = AppState(
        settings=settings, cache=_MemCache(), audit=audit, http=client,
        registry=None,
        authenticator=None,  # type: ignore[arg-type]
        proposer=None,       # type: ignore[arg-type]
    )
    try:
        wrote = await CveScanner(state).scan_once()
    finally:
        await client.aclose()
    assert wrote == 0


@pytest.mark.asyncio
async def test_scan_once_records_registry_status_on_alert(tmp_path):
    """The CVE_ALERT row carries the package's current registry_status
    so the dashboard can group alerts by ``approved`` / ``preferred``."""
    handler = _osv_handler({
        "redis": {
            "vulns": [
                {"id": "GHSA-z", "database_specific": {"cvss": {"score": 9.5}}},
            ],
        },
    })
    registry = _registry_with(
        {"redis": PackageRule(status=PackageStatus.PREFERRED)},
        threshold=0.7,
    )
    state, client = await _build_state(tmp_path, registry=registry, handler=handler)
    try:
        await CveScanner(state).scan_once()
    finally:
        await client.aclose()
    alerts = await state.audit.recent_cve_alerts()
    assert alerts[0]["registry_status"] == "preferred"


@pytest.mark.asyncio
async def test_scanner_lifecycle_starts_and_stops_cleanly(tmp_path):
    """``start()`` schedules the loop on the running event loop and
    ``stop()`` drains it without leaking the task."""
    handler = _osv_handler({})
    registry = _registry_with({"x": PackageRule(status=PackageStatus.APPROVED)})
    state, client = await _build_state(
        tmp_path, registry=registry, handler=handler,
        # Long interval so the loop blocks on the wait, never re-enters
        # the OSV path during the test window.
        settings_overrides={"cve_scan_interval_hours": 24.0},
    )
    try:
        scanner = CveScanner(state)
        scanner.start()
        # Yield once so the task is actually scheduled.
        await asyncio.sleep(0)
        assert scanner._task is not None and not scanner._task.done()
        await scanner.stop()
        assert scanner._task is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_scanner_disabled_when_interval_zero(tmp_path):
    """Setting the interval to 0 disables the scheduler entirely — the
    background task never starts."""
    handler = _osv_handler({})
    registry = _registry_with({"x": PackageRule(status=PackageStatus.APPROVED)})
    state, client = await _build_state(
        tmp_path, registry=registry, handler=handler,
        settings_overrides={"cve_scan_interval_hours": 0.0},
    )
    try:
        scanner = CveScanner(state)
        scanner.start()
        assert scanner._task is None
        # stop() on a never-started scheduler is a no-op, not an error.
        await scanner.stop()
    finally:
        await client.aclose()


def test_settings_reads_lexalign_cve_scan_interval_hours(monkeypatch):
    """The env var name documented in CLAUDE.md / the task spec must
    actually bind to the field."""
    monkeypatch.setenv("LEXALIGN_CVE_SCAN_INTERVAL_HOURS", "0.25")
    s = Settings()
    assert s.cve_scan_interval_hours == pytest.approx(0.25)


def test_audit_constants_are_distinct():
    """``DENIAL_CVE_ALERT`` and ``VERDICT_CVE_ALERT`` must not collide
    with the existing CVE-denial constants — otherwise the security
    report would double-count alerts as denials."""
    from lex_align_server.audit import DENIAL_CVE, VERDICT_DENIED
    assert DENIAL_CVE_ALERT != DENIAL_CVE
    assert VERDICT_CVE_ALERT != VERDICT_DENIED
