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
    registry_status TEXT,
    agent_model     TEXT,
    agent_version   TEXT
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
    last_audit_id   TEXT,
    agent_model     TEXT,
    agent_version   TEXT
);
CREATE INDEX IF NOT EXISTS approval_requests_project ON approval_requests(project);
CREATE INDEX IF NOT EXISTS approval_requests_status ON approval_requests(status);
CREATE UNIQUE INDEX IF NOT EXISTS approval_requests_dedupe
    ON approval_requests(project, package, requester);
"""

# Indexes that reference Phase-3 columns. Created after `_MIGRATIONS` runs
# so old databases get the columns first.
_POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS audit_log_agent
    ON audit_log(agent_model, agent_version);
"""

# Columns added after the initial schema; applied at startup via ALTER TABLE.
# Each (table, column, ddl) tuple is applied if the column is missing.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("audit_log",         "agent_model",   "TEXT"),
    ("audit_log",         "agent_version", "TEXT"),
    ("approval_requests", "agent_model",   "TEXT"),
    ("approval_requests", "agent_version", "TEXT"),
]


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
    agent_model: Optional[str] = None
    agent_version: Optional[str] = None
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
    agent_model: Optional[str] = None
    agent_version: Optional[str] = None
    ts: Optional[datetime.datetime] = None
    id: Optional[str] = None


class AuditStore:
    def __init__(self, db_path: Path):
        self._db_path = db_path

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(SCHEMA)
            await self._migrate(db)
            # Indexes that depend on the migrated columns can run only
            # after the ALTER TABLEs above.
            await db.executescript(_POST_MIGRATION_INDEXES)
            await db.commit()

    @staticmethod
    async def _migrate(db: aiosqlite.Connection) -> None:
        """Apply additive `ALTER TABLE` migrations for older databases."""
        for table, column, ddl in _MIGRATIONS:
            cur = await db.execute(f"PRAGMA table_info({table})")
            cols = {row[1] for row in await cur.fetchall()}
            await cur.close()
            if column not in cols:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    async def record_evaluation(self, record: AuditRecord) -> str:
        record.id = record.id or str(uuid.uuid4())
        record.ts = record.ts or datetime.datetime.now(tz=datetime.timezone.utc)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT INTO audit_log
                   (id, ts, project, requester, package, version, resolved_version,
                    verdict, denial_category, reason, license, cve_ids, max_cvss,
                    registry_status, agent_model, agent_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    record.agent_model,
                    record.agent_version,
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
                       last_audit_id = ?, agent_model = ?, agent_version = ?
                       WHERE id = ?""",
                    (
                        req.ts.isoformat(),
                        req.rationale,
                        req.status,
                        req.last_audit_id,
                        req.agent_model,
                        req.agent_version,
                        req.id,
                    ),
                )
            else:
                await db.execute(
                    """INSERT INTO approval_requests
                       (id, ts, project, requester, package, rationale, status,
                        last_audit_id, agent_model, agent_version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        req.id,
                        req.ts.isoformat(),
                        req.project,
                        req.requester,
                        req.package,
                        req.rationale,
                        req.status,
                        req.last_audit_id,
                        req.agent_model,
                        req.agent_version,
                    ),
                )
            await db.commit()
        return req.id

    async def mark_approved_by_package(self, normalized_name: str) -> int:
        """Move every PENDING_REVIEW request for `normalized_name` to APPROVED.

        Called when an operator classifies a pending package via the dashboard
        and adds it to the in-memory registry. Returns the number of rows
        flipped, which the dashboard can display in its toast.
        """
        from .registry import normalize_name as _norm
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, package FROM approval_requests WHERE status = ?",
                (APPROVAL_PENDING,),
            )
            rows = [dict(r) for r in await cur.fetchall()]
            await cur.close()
            ids = [r["id"] for r in rows if _norm(r["package"]) == normalized_name]
            if not ids:
                return 0
            placeholders = ",".join("?" * len(ids))
            await db.execute(
                f"UPDATE approval_requests SET status = ? WHERE id IN ({placeholders})",
                [APPROVAL_APPROVED, *ids],
            )
            await db.commit()
        return len(ids)

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
                           cve_ids, max_cvss, registry_status, agent_model, agent_version
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

    async def list_pending_by_package(self) -> list[dict]:
        """All PENDING_REVIEW approval requests grouped by package.

        The dashboard uses this to surface "things developers asked for" as a
        triage queue. Multiple requests for the same package (across
        projects/requesters) collapse into a single row carrying the count,
        the most recent rationale, and the most recent timestamp. Callers
        are expected to further filter against the live registry.
        """
        # Imported lazily to avoid a circular import at module load.
        from .registry import normalize_name

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT package, rationale, project, requester, ts
                   FROM approval_requests
                   WHERE status = ?
                   ORDER BY ts DESC""",
                (APPROVAL_PENDING,),
            )
            rows = [dict(r) for r in await cur.fetchall()]
            await cur.close()

        grouped: dict[str, dict] = {}
        for r in rows:
            key = normalize_name(r["package"])
            entry = grouped.get(key)
            if entry is None:
                grouped[key] = {
                    "package": r["package"],
                    "normalized_name": key,
                    "latest_rationale": r["rationale"],
                    "latest_ts": r["ts"],
                    "latest_project": r["project"],
                    "latest_requester": r["requester"],
                    "request_count": 1,
                }
            else:
                entry["request_count"] += 1
                # Rows are ordered DESC by ts so the first one wins for "latest".
        return list(grouped.values())

    async def agents_report(self, project: Optional[str] = None) -> dict[str, Any]:
        """Aggregate evaluations by (agent_model, agent_version).

        Powers the "agents" dashboard, so operators can see exactly which
        Claude (or other agent) version is making which kinds of requests.
        Rows where the agent is unknown collapse into one bucket.
        """
        where = ""
        params: list[Any] = []
        if project:
            where = " WHERE project = ?"
            params.append(project)

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            agg_cur = await db.execute(
                f"""SELECT agent_model, agent_version,
                          COUNT(*) AS evaluations,
                          SUM(CASE WHEN verdict = ? THEN 1 ELSE 0 END) AS denials,
                          SUM(CASE WHEN verdict = ? THEN 1 ELSE 0 END) AS provisional,
                          MAX(ts) AS last_seen
                   FROM audit_log{where}
                   GROUP BY agent_model, agent_version
                   ORDER BY evaluations DESC""",
                [VERDICT_DENIED, VERDICT_PROVISIONALLY_ALLOWED, *params],
            )
            agents = [dict(r) for r in await agg_cur.fetchall()]
            await agg_cur.close()

            recent_cur = await db.execute(
                f"""SELECT id, ts, project, requester, package, version, verdict,
                          denial_category, reason, agent_model, agent_version
                   FROM audit_log{where}
                   ORDER BY ts DESC LIMIT 50""",
                params,
            )
            recent = [dict(r) for r in await recent_cur.fetchall()]
            await recent_cur.close()
        return {
            "project": project,
            "agents": agents,
            "recent": recent,
        }

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
