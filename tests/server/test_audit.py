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
