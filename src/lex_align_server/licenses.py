"""License lookup, normalization, and policy evaluation.

For packages not listed in the registry, the server fetches the license from
PyPI, normalizes it to an SPDX-ish token, and checks it against
`global_policies`. Results are cached in Redis keyed by package name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import httpx

from .cache import JsonCache
from .registry import Action, GlobalPolicies


PYPI_URL_TEMPLATE = "{base}/{package}/json"
PYPI_VERSIONED_URL_TEMPLATE = "{base}/{package}/{version}/json"


@dataclass
class LicenseVerdict:
    action: Action
    license: Optional[str]
    reason: str
    needs_human_review: bool = False


@dataclass
class LicenseInfo:
    license_raw: Optional[str]
    license_normalized: str

    def to_dict(self) -> dict:
        return {
            "license_raw": self.license_raw,
            "license_normalized": self.license_normalized,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LicenseInfo":
        return cls(
            license_raw=d.get("license_raw"),
            license_normalized=d.get("license_normalized") or "UNKNOWN",
        )


# Ordered most-specific first so LGPL does not accidentally match "GPL".
_LICENSE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AGPL-3.0",     re.compile(r"\bagpl[-_ ]?v?3|affero.*general.*public", re.I)),
    ("LGPL-3.0",     re.compile(r"\blgpl[-_ ]?v?3", re.I)),
    ("LGPL-2.1",     re.compile(r"\blgpl[-_ ]?v?2\.?1", re.I)),
    ("GPL-3.0",      re.compile(r"\bgpl[-_ ]?v?3|gnu general public.*3", re.I)),
    ("GPL-2.0",      re.compile(r"\bgpl[-_ ]?v?2|gnu general public.*2", re.I)),
    ("MPL-2.0",      re.compile(r"\bmpl[-_ ]?v?2|mozilla public license.*2", re.I)),
    ("Apache-2.0",   re.compile(r"\bapache(.*2|[-_ ]?license[-_ ]?2)?", re.I)),
    ("BSD-3-Clause", re.compile(r"\bbsd[-_ ]?3|new bsd|modified bsd", re.I)),
    ("BSD-2-Clause", re.compile(r"\bbsd[-_ ]?2|simplified bsd|freebsd", re.I)),
    ("BSD",          re.compile(r"\bbsd\b", re.I)),
    ("MIT",          re.compile(r"\bmit\b", re.I)),
    ("ISC",          re.compile(r"\bisc\b", re.I)),
    ("Unlicense",    re.compile(r"\bunlicense\b", re.I)),
    ("CC0-1.0",      re.compile(r"\bcc0\b", re.I)),
    ("Proprietary",  re.compile(r"\bproprietary|commercial[-_ ]?license\b", re.I)),
]
_CANONICAL_TOKENS = {token for token, _ in _LICENSE_PATTERNS}


def normalize_license(raw: Optional[str]) -> str:
    if not raw:
        return "UNKNOWN"
    s = raw.strip()
    if not s:
        return "UNKNOWN"
    for token in _CANONICAL_TOKENS:
        if s.lower() == token.lower():
            return token
    for token, pattern in _LICENSE_PATTERNS:
        if pattern.search(s):
            return token
    return "UNKNOWN"


def _extract_license_from_pypi_json(payload: dict) -> Optional[str]:
    info = payload.get("info") or {}
    license_val = info.get("license")
    if license_val and isinstance(license_val, str) and license_val.strip():
        return license_val.strip()
    classifiers = info.get("classifiers") or []
    for c in classifiers:
        if isinstance(c, str) and c.startswith("License ::"):
            return c.split("::")[-1].strip()
    return None


async def fetch_license_from_pypi(
    package: str,
    version: Optional[str],
    pypi_base: str,
    client: httpx.AsyncClient,
) -> tuple[Optional[str], Optional[str]]:
    """Return (raw_license_string, latest_version) from PyPI, or (None, None) on error."""
    if version:
        url = PYPI_VERSIONED_URL_TEMPLATE.format(base=pypi_base, package=package, version=version)
    else:
        url = PYPI_URL_TEMPLATE.format(base=pypi_base, package=package)
    try:
        resp = await client.get(url)
    except (httpx.HTTPError, httpx.TimeoutException):
        return None, None
    if resp.status_code != 200:
        return None, None
    try:
        data = resp.json()
    except ValueError:
        return None, None
    raw = _extract_license_from_pypi_json(data)
    info = data.get("info") or {}
    latest = info.get("version")
    return raw, latest if isinstance(latest, str) else None


def evaluate_license(
    license_normalized: str, policies: GlobalPolicies
) -> LicenseVerdict:
    if policies.is_blocked(license_normalized):
        return LicenseVerdict(
            action=Action.BLOCK,
            license=license_normalized,
            reason=f"License {license_normalized} is hard-banned by the enterprise policy.",
        )
    if policies.is_auto_approved(license_normalized):
        return LicenseVerdict(
            action=Action.ALLOW,
            license=license_normalized,
            reason=f"License {license_normalized} is on the auto-approve list.",
        )
    if license_normalized == "UNKNOWN":
        policy = (policies.unknown_license_policy or "pending_approval").lower()
        if policy == "allow":
            return LicenseVerdict(
                action=Action.ALLOW,
                license=license_normalized,
                reason="License could not be determined; unknown_license_policy=allow.",
            )
        if policy == "pending_approval":
            return LicenseVerdict(
                action=Action.ALLOW,
                license=license_normalized,
                reason=(
                    "License could not be determined from PyPI; "
                    "provisionally allowed pending human review."
                ),
                needs_human_review=True,
            )
        return LicenseVerdict(
            action=Action.BLOCK,
            license=license_normalized,
            reason=(
                "License could not be determined from PyPI; "
                f"unknown_license_policy={policy}."
            ),
        )
    return LicenseVerdict(
        action=Action.BLOCK,
        license=license_normalized,
        reason=(
            f"License {license_normalized} is not on the auto-approve list; "
            "add it to global_policies.auto_approve_licenses if it should be permitted."
        ),
    )


async def resolve_license(
    package: str,
    version: Optional[str],
    cache: JsonCache,
    cache_ttl: int,
    pypi_base: str,
    http_client: httpx.AsyncClient,
) -> tuple[LicenseInfo, Optional[str]]:
    """Resolve (cache-or-fetch) a package's license. Returns (info, latest_version)."""
    cache_key = f"license:{package.lower()}"
    cached = await cache.get(cache_key)
    if cached is not None:
        try:
            info = LicenseInfo.from_dict(cached["info"])
            return info, cached.get("latest_version")
        except (KeyError, TypeError):
            pass

    raw, latest = await fetch_license_from_pypi(package, version, pypi_base, http_client)
    info = LicenseInfo(
        license_raw=raw,
        license_normalized=normalize_license(raw),
    )
    await cache.set(
        cache_key,
        {"info": info.to_dict(), "latest_version": latest},
        cache_ttl,
    )
    return info, latest
