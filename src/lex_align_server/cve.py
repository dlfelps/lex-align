"""CVE lookup via OSV (osv.dev).

OSV is the Google-sponsored aggregator that ingests GHSA, NVD, PyPA Advisory
DB, and others. The free `POST /v1/query` endpoint takes a package coordinate
and returns the list of vulnerabilities with severity scores. We parse out the
highest CVSS base score and let `GlobalPolicies.cve_blocks` decide.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from .cache import JsonCache

logger = logging.getLogger(__name__)


@dataclass
class CveInfo:
    ids: list[str]
    max_score: Optional[float]  # highest CVSS base score seen, or None
    raw_count: int

    def to_dict(self) -> dict:
        return {"ids": self.ids, "max_score": self.max_score, "raw_count": self.raw_count}

    @classmethod
    def from_dict(cls, d: dict) -> "CveInfo":
        return cls(
            ids=list(d.get("ids") or []),
            max_score=d.get("max_score"),
            raw_count=int(d.get("raw_count") or 0),
        )


_CVSS_VECTOR_BASE_SCORE = re.compile(r"CVSS:[^/]+/[^()]*")


def _score_from_severity_entry(entry: dict) -> Optional[float]:
    """Extract a CVSS base score from one OSV `severity` entry.

    OSV represents severity as a list of `{type, score}` objects. The `type`
    can be `CVSS_V2`, `CVSS_V3`, `CVSS_V4`. The `score` is a CVSS vector
    string (e.g. `CVSS:3.1/AV:N/AC:L/...`) — the base numeric score is NOT
    embedded in the vector. OSV separately exposes a per-vulnerability
    `database_specific.cvss` numeric, but availability varies.

    We accept either:
      * a numeric score (already a float),
      * a vector string with `/CVSS_BASE_SCORE=<num>` suffix that some
        ecosystems emit,
      * fall back to None and let the caller treat as "unknown severity".
    """
    score = entry.get("score")
    if isinstance(score, (int, float)):
        return float(score)
    if isinstance(score, str):
        m = re.search(r"CVSS_BASE_SCORE\s*=\s*([0-9.]+)", score)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
    return None


def _score_from_vuln(vuln: dict) -> Optional[float]:
    """Top-level CVSS extractor for a single OSV vulnerability."""
    # Preferred: explicit numeric in database_specific (GHSA, PyPA both
    # commonly include this).
    db_specific = vuln.get("database_specific") or {}
    cvss = db_specific.get("cvss")
    if isinstance(cvss, dict):
        score = cvss.get("score")
        if isinstance(score, (int, float)):
            return float(score)
    if isinstance(cvss, (int, float)):
        return float(cvss)

    # Fallback: severity[].score parsing.
    best: Optional[float] = None
    for entry in vuln.get("severity") or []:
        if not isinstance(entry, dict):
            continue
        s = _score_from_severity_entry(entry)
        if s is not None and (best is None or s > best):
            best = s
    return best


def _vuln_id(vuln: dict) -> Optional[str]:
    primary = vuln.get("id")
    if isinstance(primary, str):
        return primary
    return None


def _summarize_vulns(vulns: list[dict]) -> CveInfo:
    ids: list[str] = []
    max_score: Optional[float] = None
    for v in vulns:
        if not isinstance(v, dict):
            continue
        vid = _vuln_id(v)
        if vid:
            ids.append(vid)
        s = _score_from_vuln(v)
        if s is not None and (max_score is None or s > max_score):
            max_score = s
    return CveInfo(ids=ids, max_score=max_score, raw_count=len(vulns))


async def query_osv(
    package: str,
    version: Optional[str],
    osv_url: str,
    http_client: httpx.AsyncClient,
) -> Optional[list[dict]]:
    """Return raw `vulns` list from OSV, or None on error/timeout."""
    payload: dict = {
        "package": {"name": package, "ecosystem": "PyPI"},
    }
    if version:
        payload["version"] = version
    try:
        resp = await http_client.post(osv_url, json=payload)
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("osv: query for %s@%s failed: %s", package, version, exc)
        return None
    if resp.status_code != 200:
        logger.warning("osv: %s returned %s", package, resp.status_code)
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    vulns = data.get("vulns") or []
    return [v for v in vulns if isinstance(v, dict)]


async def resolve_cves(
    package: str,
    version: Optional[str],
    cache: JsonCache,
    cache_ttl: int,
    osv_url: str,
    http_client: httpx.AsyncClient,
) -> CveInfo:
    """Cache-or-fetch CVE data for a package@version.

    A None version is cached separately from versioned queries — OSV's
    behaviour with no version is documented as "all versions" so we treat it
    as a distinct cache entry.
    """
    cache_key = f"cve:{package.lower()}@{version or 'latest'}"
    cached = await cache.get(cache_key)
    if cached is not None:
        try:
            return CveInfo.from_dict(cached)
        except Exception:
            pass

    vulns = await query_osv(package, version, osv_url, http_client)
    if vulns is None:
        # Treat OSV outage as "no known CVEs" but do NOT cache the negative
        # result — we want a fresh attempt next time.
        return CveInfo(ids=[], max_score=None, raw_count=0)

    info = _summarize_vulns(vulns)
    await cache.set(cache_key, info.to_dict(), cache_ttl)
    return info
