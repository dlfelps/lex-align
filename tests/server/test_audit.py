"""Unit tests for the SQLite audit + approval store."""

from __future__ import annotations

import pytest

from lex_align_server.audit import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    ApprovalRequest,
    AuditRecord,
    AuditStore,
    DENIAL_CVE,
    DENIAL_LICENSE,
    DENIAL_NONE,
    VERDICT_ALLOWED,
    VERDICT_DENIED,
)


@pytest.fixture
async def store(tmp_path):
    s = AuditStore(tmp_path / "audit.sqlite")
    await s.init()
    return s


@pytest.mark.asyncio
async def test_record_and_health(store):
    rec_id = await store.record_evaluation(AuditRecord(
        project="proj", requester="anon", package="redis", version=None,
        resolved_version="5.0.0", verdict=VERDICT_ALLOWED,
        denial_category=DENIAL_NONE, reason="ok",
    ))
    assert rec_id
    assert await store.health() is True


@pytest.mark.asyncio
async def test_legal_report_filters_to_license_denials(store):
    await store.record_evaluation(AuditRecord(
        project="p1", requester="a", package="gplpkg", version=None,
        resolved_version=None, verdict=VERDICT_DENIED,
        denial_category=DENIAL_LICENSE, reason="GPL", license="GPL-3.0",
    ))
    await store.record_evaluation(AuditRecord(
        project="p1", requester="a", package="cvepkg", version=None,
        resolved_version=None, verdict=VERDICT_DENIED,
        denial_category=DENIAL_CVE, reason="critical", cve_ids=["CVE-1"],
        max_cvss=9.5,
    ))
    legal = await store.legal_report()
    sec = await store.security_report()
    assert legal["total_denials"] == 1
    assert legal["recent"][0]["package"] == "gplpkg"
    assert sec["total_denials"] == 1
    assert sec["recent"][0]["cve_ids"] == ["CVE-1"]


@pytest.mark.asyncio
async def test_reports_filter_by_project(store):
    for project in ("alpha", "beta"):
        await store.record_evaluation(AuditRecord(
            project=project, requester="a", package="p", version=None,
            resolved_version=None, verdict=VERDICT_DENIED,
            denial_category=DENIAL_LICENSE, reason="x", license="GPL-3.0",
        ))
    alpha = await store.legal_report(project="alpha")
    assert alpha["total_denials"] == 1
    assert alpha["recent"][0]["project"] == "alpha"


@pytest.mark.asyncio
async def test_approval_request_dedupe(store):
    req = ApprovalRequest(project="proj", requester="anon", package="numpy",
                          rationale="needed for math")
    rid1 = await store.upsert_approval_request(req)
    # Second call with same (project, package, requester) updates rather than
    # inserts a duplicate.
    req2 = ApprovalRequest(project="proj", requester="anon", package="numpy",
                           rationale="updated rationale")
    rid2 = await store.upsert_approval_request(req2)
    assert rid1 == rid2
    rows = await store.list_approval_requests(project="proj")
    assert len(rows) == 1
    assert rows[0]["rationale"] == "updated rationale"
    assert rows[0]["status"] == APPROVAL_PENDING


@pytest.mark.asyncio
async def test_list_pending_by_package_groups_and_filters_status(store):
    # Same package requested twice across projects → one grouped row.
    await store.upsert_approval_request(ApprovalRequest(
        project="p1", requester="alice", package="numpy", rationale="math",
    ))
    await store.upsert_approval_request(ApprovalRequest(
        project="p2", requester="bob", package="numpy", rationale="more math",
    ))
    # A different package, also pending.
    await store.upsert_approval_request(ApprovalRequest(
        project="p1", requester="alice", package="scipy", rationale="science",
    ))
    # An approved request must NOT show up.
    await store.upsert_approval_request(ApprovalRequest(
        project="p1", requester="alice", package="oldpkg", rationale="legacy",
        status=APPROVAL_APPROVED,
    ))
    grouped = await store.list_pending_by_package()
    by_name = {g["package"]: g for g in grouped}
    assert set(by_name) == {"numpy", "scipy"}
    assert by_name["numpy"]["request_count"] == 2
    assert by_name["scipy"]["request_count"] == 1
    assert by_name["numpy"]["normalized_name"] == "numpy"


@pytest.mark.asyncio
async def test_list_pending_by_package_normalizes_names(store):
    # `lex_align_server.registry.normalize_name` lowercases and maps
    # `-`/`.` → `_`. Two requests that differ only in case + hyphen vs.
    # underscore must collapse onto the same key.
    await store.upsert_approval_request(ApprovalRequest(
        project="p1", requester="alice", package="Some-Pkg", rationale="parse",
    ))
    await store.upsert_approval_request(ApprovalRequest(
        project="p2", requester="bob", package="some_pkg", rationale="also parse",
    ))
    grouped = await store.list_pending_by_package()
    assert len(grouped) == 1
    assert grouped[0]["request_count"] == 2
    assert grouped[0]["normalized_name"] == "some_pkg"


@pytest.mark.asyncio
async def test_projects_summary(store):
    await store.record_evaluation(AuditRecord(
        project="p1", requester="a", package="x", version=None,
        resolved_version=None, verdict=VERDICT_ALLOWED,
        denial_category=DENIAL_NONE, reason="",
    ))
    await store.record_evaluation(AuditRecord(
        project="p1", requester="a", package="y", version=None,
        resolved_version=None, verdict=VERDICT_DENIED,
        denial_category=DENIAL_LICENSE, reason="x",
    ))
    await store.upsert_approval_request(ApprovalRequest(
        project="p2", requester="a", package="z", rationale="r",
    ))
    summary = {row["project"]: row for row in await store.projects_summary()}
    assert summary["p1"]["evaluations"] == 2
    assert summary["p1"]["denials"] == 1
    assert summary["p2"]["approval_requests"] == 1


@pytest.mark.asyncio
async def test_audit_record_persists_agent_identity(store):
    """The agent_model + agent_version columns survive a round-trip and
    surface in the legal/security report rows so the dashboard can render
    them without an extra join."""
    await store.record_evaluation(AuditRecord(
        project="p1", requester="anon", package="gplpkg", version=None,
        resolved_version=None, verdict=VERDICT_DENIED,
        denial_category=DENIAL_LICENSE, reason="GPL", license="GPL-3.0",
        agent_model="opus", agent_version="4.7",
    ))
    legal = await store.legal_report()
    assert legal["recent"][0]["agent_model"] == "opus"
    assert legal["recent"][0]["agent_version"] == "4.7"


@pytest.mark.asyncio
async def test_agents_report_aggregates_by_agent(store):
    """agents_report groups by (model, version) and counts denials +
    provisional separately so operators can see which agent generates
    which kinds of friction."""
    from lex_align_server.audit import VERDICT_PROVISIONALLY_ALLOWED

    common = dict(
        project="p1", requester="anon", version=None, resolved_version=None,
        denial_category=DENIAL_NONE, reason="",
    )
    await store.record_evaluation(AuditRecord(
        package="x", verdict=VERDICT_ALLOWED,
        agent_model="opus", agent_version="4.7", **common,
    ))
    await store.record_evaluation(AuditRecord(
        package="y", verdict=VERDICT_PROVISIONALLY_ALLOWED,
        agent_model="opus", agent_version="4.7", **common,
    ))
    denied_kwargs = {**common, "denial_category": DENIAL_LICENSE, "reason": "x"}
    await store.record_evaluation(AuditRecord(
        package="z", verdict=VERDICT_DENIED,
        agent_model="opus", agent_version="4.7", **denied_kwargs,
    ))
    await store.record_evaluation(AuditRecord(
        package="x", verdict=VERDICT_ALLOWED,
        agent_model="sonnet", agent_version="4.6", **common,
    ))
    # Anonymous (no agent headers) — must bucket under (None, None) rather
    # than dropping the row.
    await store.record_evaluation(AuditRecord(
        package="x", verdict=VERDICT_ALLOWED, **common,
    ))

    report = await store.agents_report()
    by_key = {(a["agent_model"], a["agent_version"]): a for a in report["agents"]}
    assert by_key[("opus", "4.7")]["evaluations"] == 3
    assert by_key[("opus", "4.7")]["denials"] == 1
    assert by_key[("opus", "4.7")]["provisional"] == 1
    assert by_key[("sonnet", "4.6")]["evaluations"] == 1
    assert by_key[(None, None)]["evaluations"] == 1


@pytest.mark.asyncio
async def test_mark_approved_by_package_flips_pending_for_normalized_name(store):
    """Classifying a package via the dashboard should approve every
    pending request whose normalized name matches — including names that
    differ only in case or hyphen vs. underscore — and leave unrelated
    requests untouched."""
    await store.upsert_approval_request(ApprovalRequest(
        project="p1", requester="alice", package="Some-Pkg", rationale="x",
    ))
    await store.upsert_approval_request(ApprovalRequest(
        project="p2", requester="bob", package="some_pkg", rationale="y",
    ))
    await store.upsert_approval_request(ApprovalRequest(
        project="p1", requester="alice", package="other", rationale="z",
    ))

    flipped = await store.mark_approved_by_package("some_pkg")
    assert flipped == 2

    pending = await store.list_pending_by_package()
    pending_names = {p["normalized_name"] for p in pending}
    assert "some_pkg" not in pending_names
    assert "other" in pending_names


@pytest.mark.asyncio
async def test_legal_report_breakdown_groups_licenses_and_unknown(store):
    """legal_report rolls up every audit row by license, splits by verdict,
    and surfaces UNKNOWN as its own bucket so the dashboard can show how
    the unknown_license_policy is performing."""
    from lex_align_server.audit import VERDICT_PROVISIONALLY_ALLOWED

    common = dict(project="p1", requester="anon", version=None, resolved_version=None)
    await store.record_evaluation(AuditRecord(
        package="a", verdict=VERDICT_ALLOWED, denial_category=DENIAL_NONE,
        reason="ok", license="MIT", **common,
    ))
    await store.record_evaluation(AuditRecord(
        package="b", verdict=VERDICT_ALLOWED, denial_category=DENIAL_NONE,
        reason="ok", license="MIT", **common,
    ))
    await store.record_evaluation(AuditRecord(
        package="c", verdict=VERDICT_DENIED, denial_category=DENIAL_LICENSE,
        reason="GPL", license="GPL-3.0", **common,
    ))
    await store.record_evaluation(AuditRecord(
        package="d", verdict=VERDICT_PROVISIONALLY_ALLOWED,
        denial_category=DENIAL_NONE, reason="unknown",
        license="UNKNOWN", **common,
    ))

    legal = await store.legal_report()
    by_license = {r["license"]: r for r in legal["license_breakdown"]}
    assert by_license["MIT"]["allowed"] == 2
    assert by_license["GPL-3.0"]["denied"] == 1
    assert by_license["UNKNOWN"]["provisional"] == 1
    assert legal["unknown_license"] == {
        "total": 1, "allowed": 0, "provisional": 1, "denied": 0,
    }
    assert legal["top_projects"][0] == {"project": "p1", "denials": 1}


@pytest.mark.asyncio
async def test_security_report_severity_buckets_and_top_packages(store):
    """security_report buckets CVE-denied rows by CVSS and ranks packages /
    CVE ids by max CVSS first, denial count second."""
    common = dict(
        project="p1", requester="anon", version=None, resolved_version=None,
        verdict=VERDICT_DENIED, denial_category=DENIAL_CVE, reason="cve",
    )
    await store.record_evaluation(AuditRecord(
        package="critpkg", cve_ids=["CVE-1"], max_cvss=9.5, **common,
    ))
    await store.record_evaluation(AuditRecord(
        package="critpkg", cve_ids=["CVE-1"], max_cvss=9.5, **common,
    ))
    await store.record_evaluation(AuditRecord(
        package="highpkg", cve_ids=["CVE-2"], max_cvss=7.4, **common,
    ))
    await store.record_evaluation(AuditRecord(
        package="medpkg", cve_ids=["CVE-3"], max_cvss=4.5, **common,
    ))

    sec = await store.security_report()
    assert sec["severity_distribution"]["critical"] == 2
    assert sec["severity_distribution"]["high"] == 1
    assert sec["severity_distribution"]["medium"] == 1
    # Top packages: critpkg first (max CVSS 9.5), then highpkg, then medpkg.
    assert [r["package"] for r in sec["top_packages"][:3]] == [
        "critpkg", "highpkg", "medpkg",
    ]
    assert sec["top_packages"][0]["denials"] == 2
    # Top CVEs ranks the same way.
    assert sec["top_cves"][0]["cve_id"] == "CVE-1"
    assert sec["top_cves"][0]["occurrences"] == 2
    # Without a registry passed in, the hot list is empty.
    assert sec["hot_registry_packages"] == []


@pytest.mark.asyncio
async def test_security_report_hot_registry_packages_flags_approved_with_cves(store):
    """A package the registry currently approves whose recent audit rows
    show CVE denials must surface in `hot_registry_packages` so an
    operator can pin or replace before it lands in a commit."""
    from lex_align_server.registry import (
        GlobalPolicies,
        PackageRule,
        PackageStatus,
        Registry,
    )

    registry = Registry(
        version="1",
        global_policies=GlobalPolicies(),
        packages={
            "redis": PackageRule(status=PackageStatus.APPROVED),
            "banned_pkg": PackageRule(status=PackageStatus.BANNED),
        },
    )

    common = dict(
        project="p1", requester="anon", version=None, resolved_version=None,
        verdict=VERDICT_DENIED, denial_category=DENIAL_CVE, reason="cve",
    )
    # Approved package newly hit by a critical → should surface.
    await store.record_evaluation(AuditRecord(
        package="redis", cve_ids=["CVE-9"], max_cvss=9.8, **common,
    ))
    # Banned package — also has CVEs, but registry already says no, so it
    # must not surface here (the dashboard would drown in noise otherwise).
    await store.record_evaluation(AuditRecord(
        package="banned_pkg", cve_ids=["CVE-1"], max_cvss=8.0, **common,
    ))
    # Unknown-to-registry package — also out of scope for "hot" panel.
    await store.record_evaluation(AuditRecord(
        package="unknown_pkg", cve_ids=["CVE-2"], max_cvss=8.0, **common,
    ))

    sec = await store.security_report(registry=registry)
    hot = {r["package"]: r for r in sec["hot_registry_packages"]}
    assert "redis" in hot
    assert hot["redis"]["registry_status"] == "approved"
    assert hot["redis"]["cve_ids"] == ["CVE-9"]
    assert hot["redis"]["denials"] == 1
    assert "banned_pkg" not in hot
    assert "unknown_pkg" not in hot


@pytest.mark.asyncio
async def test_audit_migration_adds_agent_columns_to_old_db(tmp_path):
    """A pre-Phase-3 SQLite file (without agent_* columns) must upgrade
    cleanly when the new server boots against it."""
    import aiosqlite
    db_path = tmp_path / "old.sqlite"
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE audit_log (
                id TEXT PRIMARY KEY, ts TEXT, project TEXT, requester TEXT,
                package TEXT, version TEXT, resolved_version TEXT,
                verdict TEXT, denial_category TEXT, reason TEXT,
                license TEXT, cve_ids TEXT, max_cvss REAL, registry_status TEXT
            );
            CREATE TABLE approval_requests (
                id TEXT PRIMARY KEY, ts TEXT, project TEXT, requester TEXT,
                package TEXT, rationale TEXT, status TEXT, last_audit_id TEXT
            );
        """)
        await db.commit()

    s = AuditStore(db_path)
    await s.init()
    # Inserting a row with an agent identity must succeed against the
    # migrated schema — proving the ALTER TABLE ran.
    await s.record_evaluation(AuditRecord(
        project="p", requester="a", package="x", version=None,
        resolved_version=None, verdict=VERDICT_ALLOWED,
        denial_category=DENIAL_NONE, reason="",
        agent_model="opus", agent_version="4.7",
    ))
    legal = await s.legal_report()
    assert legal["total_denials"] == 0  # call works against migrated schema
