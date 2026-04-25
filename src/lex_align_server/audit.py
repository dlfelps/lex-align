"""SQLite audit log + approval-request store.

Two tables:

* `audit_log` — one row per `/evaluate` call. Backs the legal report
  (license-driven denials) and security report (CVE-driven denials).
* `approval_requests` — one row per `/approval-requests` POST. Phase 3 will
  attach a PR-creation workflow; for now we just persist them.

We use plain `aiosqlite` rather than an ORM. The schema is small and the
report queries are easier to read as straight SQL.
"""

from __future__ import annotations

import datetime
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiosqlite


# Verdict labels surfaced in API responses and audit rows.
VERDICT_ALLOWED = "ALLOWED"
VERDICT_DENIED = "DENIED"
VERDICT_PROVISIONALLY_ALLOWED = "PROVISIONALLY_ALLOWED"

# Reason category — drives report grouping. Stored on every audit row.
DENIAL_REGISTRY = "registry"
DENIAL_LICENSE = "license"
DENIAL_CVE = "cve"
DENIAL_NONE = ""  # ALLOW / PROVISIONALLY_ALLOWED


SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id              TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    project         TEXT NOT NULL,
    requester       TEXT NOT NULL,
    package         TEXT NOT NULL,
    version         TEXT,
    resolved_version TEXT,
    verdict         TEXT NOT NULL,
    denial_category TEXT NOT NULL,
    reason          TEXT,
    license         TEXT,
    cve_ids         TEXT,
    max_cvss        REAL,
    registry_status TEXT
);
CREATE INDEX IF NOT EXISTS audit_log_project_ts ON audit_log(project, ts);
CREATE INDEX IF NOT EXISTS audit_log_package ON audit_log(package);
CREATE INDEX IF NOT EXISTS audit_log_denial ON audit_log(denial_category, verdict);

CREATE TABLE IF NOT EXISTS approval_requests (
    id              TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    project         TEXT NOT NULL,
    requester       TEXT NOT NULL,
    package         TEXT NOT NULL,
    rationale       TEXT NOT NULL,
    status          TEXT NOT NULL,
    last_audit_id   TEXT
);
CREATE INDEX IF NOT EXISTS approval_requests_project ON approval_requests(project);
CREATE UNIQUE INDEX IF NOT EXISTS approval_requests_dedupe
    ON approval_requests(project, package, requester);
"""


APPROVAL_PENDING = "PENDING_REVIEW"
APPROVAL_APPROVED = "APPROVED"
APPROVAL_REJECTED = "REJECTED"


@dataclass
class AuditRecord:
    project: str
    requester: str
    package: str
    version: Optional[str]
    resolved_version: Optional[str]
    verdict: str
    denial_category: str
    reason: Optional[str]
    license: Optional[str] = None
    cve_ids: list[str] = field(default_factory=list)
    max_cvss: Optional[float] = None
    registry_status: Optional[str] = None
    ts: Optional[datetime.datetime] = None
    id: Optional[str] = None


@dataclass
class ApprovalRequest:
    project: str
    requester: str
    package: str
    rationale: str
    status: str = APPROVAL_PENDING
    last_audit_id: Optional[str] = None
    ts: Optional[datetime.datetime] = None
    id: Optional[str] = None


class AuditStore:
    def __init__(self, db_path: Path):
        self._db_path = db_path

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def record_evaluation(self, record: AuditRecord) -> str:
        record.id = record.id or str(uuid.uuid4())
        record.ts = record.ts or datetime.datetime.now(tz=datetime.timezone.utc)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT INTO audit_log
                   (id, ts, project, requester, package, version, resolved_version,
                    verdict, denial_category, reason, license, cve_ids, max_cvss,
                    registry_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.ts.isoformat(),
                    record.project,
                    record.requester,
                    record.package,
                    record.version,
                    record.resolved_version,
                    record.verdict,
                    record.denial_category,
                    record.reason,
                    record.license,
                    json.dumps(record.cve_ids) if record.cve_ids else None,
                    record.max_cvss,
                    record.registry_status,
                ),
            )
            await db.commit()
        return record.id

    async def upsert_approval_request(self, req: ApprovalRequest) -> str:
        req.id = req.id or str(uuid.uuid4())
        req.ts = req.ts or datetime.datetime.now(tz=datetime.timezone.utc)
        async with aiosqlite.connect(self._db_path) as db:
            row = await self._existing_request(db, req.project, req.package, req.requester)
            if row is not None:
                req.id = row["id"]
                await db.execute(
                    """UPDATE approval_requests SET ts = ?, rationale = ?, status = ?,
                       last_audit_id = ? WHERE id = ?""",
                    (
                        req.ts.isoformat(),
                        req.rationale,
                        req.status,
                        req.last_audit_id,
                        req.id,
                    ),
                )
            else:
                await db.execute(
                    """INSERT INTO approval_requests
                       (id, ts, project, requester, package, rationale, status, last_audit_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        req.id,
                        req.ts.isoformat(),
                        req.project,
                        req.requester,
                        req.package,
                        req.rationale,
                        req.status,
                        req.last_audit_id,
                    ),
                )
            await db.commit()
        return req.id

    @staticmethod
    async def _existing_request(
        db: aiosqlite.Connection, project: str, package: str, requester: str
    ) -> Optional[dict]:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id FROM approval_requests WHERE project = ? AND package = ? AND requester = ?",
            (project, package, requester),
        )
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    # ── reports ────────────────────────────────────────────────────────────

    async def legal_report(self, project: Optional[str] = None) -> dict[str, Any]:
        return await self._denial_report(DENIAL_LICENSE, project)

    async def security_report(self, project: Optional[str] = None) -> dict[str, Any]:
        return await self._denial_report(DENIAL_CVE, project)

    async def _denial_report(
        self, category: str, project: Optional[str]
    ) -> dict[str, Any]:
        where = "denial_category = ?"
        params: list[Any] = [category]
        if project:
            where += " AND project = ?"
            params.append(project)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            total_cur = await db.execute(
                f"SELECT COUNT(*) AS n FROM audit_log WHERE {where}",
                params,
            )
            total = (await total_cur.fetchone())["n"]
            await total_cur.close()

            recent_cur = await db.execute(
                f"""SELECT id, ts, project, requester, package, version, reason, license,
                           cve_ids, max_cvss, registry_status
                    FROM audit_log
                    WHERE {where}
                    ORDER BY ts DESC LIMIT 100""",
                params,
            )
            recent = [dict(r) for r in await recent_cur.fetchall()]
            await recent_cur.close()
        for row in recent:
            if row.get("cve_ids"):
                try:
                    row["cve_ids"] = json.loads(row["cve_ids"])
                except json.JSONDecodeError:
                    row["cve_ids"] = []
            else:
                row["cve_ids"] = []
        return {
            "category": category,
            "project": project,
            "total_denials": total,
            "recent": recent,
        }

    async def list_approval_requests(
        self, project: Optional[str] = None, status: Optional[str] = None
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if project:
            clauses.append("project = ?")
            params.append(project)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT * FROM approval_requests{where} ORDER BY ts DESC",
                params,
            )
            rows = [dict(r) for r in await cur.fetchall()]
            await cur.close()
        return rows

    async def projects_summary(self) -> list[dict]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT project,
                          COUNT(*) AS evaluations,
                          SUM(CASE WHEN verdict = ? THEN 1 ELSE 0 END) AS denials
                   FROM audit_log GROUP BY project ORDER BY project""",
                (VERDICT_DENIED,),
            )
            audit_rows = [dict(r) for r in await cur.fetchall()]
            await cur.close()

            cur2 = await db.execute(
                "SELECT project, COUNT(*) AS approval_requests FROM approval_requests GROUP BY project"
            )
            req_rows = {dict(r)["project"]: dict(r)["approval_requests"] for r in await cur2.fetchall()}
            await cur2.close()
        out = []
        seen = set()
        for r in audit_rows:
            r["approval_requests"] = req_rows.get(r["project"], 0)
            seen.add(r["project"])
            out.append(r)
        for proj, n in req_rows.items():
            if proj not in seen:
                out.append({"project": proj, "evaluations": 0, "denials": 0, "approval_requests": n})
        return out

    async def health(self) -> bool:
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("SELECT 1")
            return True
        except Exception:
            return False
