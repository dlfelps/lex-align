"""License lookup, normalization, cache, and policy evaluation.

For packages not listed in the enterprise registry, lex-align fetches the
package's license from PyPI, normalizes it to an SPDX-ish token, and checks
it against `global_policies`. Results are cached to
`.lex-align/license-cache.json` so repeat lookups are cheap.
"""

from __future__ import annotations

import datetime
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .registry import Action, GlobalPolicies


PYPI_URL_TEMPLATE = "https://pypi.org/pypi/{package}/json"
PYPI_URL_WITH_VERSION_TEMPLATE = "https://pypi.org/pypi/{package}/{version}/json"
LICENSE_CACHE_FILENAME = "license-cache.json"
FETCH_TIMEOUT_SECONDS = 5.0


@dataclass
class LicenseVerdict:
    action: Action
    license: Optional[str]   # normalized token or "UNKNOWN"
    reason: str


@dataclass
class LicenseInfo:
    license_raw: Optional[str]       # exactly what PyPI reported
    license_normalized: str          # SPDX-ish token or "UNKNOWN"
    fetched_at: datetime.date
    source: str                      # "pypi"

    def to_dict(self) -> dict:
        return {
            "license_raw": self.license_raw,
            "license_normalized": self.license_normalized,
            "fetched_at": self.fetched_at.isoformat(),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LicenseInfo":
        return cls(
            license_raw=d.get("license_raw"),
            license_normalized=d.get("license_normalized") or "UNKNOWN",
            fetched_at=datetime.date.fromisoformat(d["fetched_at"]),
            source=d.get("source") or "pypi",
        )


class LicenseCache:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def _key(package: str, version: Optional[str]) -> str:
        package = package.lower()
        return f"{package}=={version}" if version else package

    def get(self, package: str, version: Optional[str] = None) -> Optional[LicenseInfo]:
        raw = self._data.get(self._key(package, version))
        if raw is None:
            return None
        try:
            return LicenseInfo.from_dict(raw)
        except Exception:
            return None

    def put(self, package: str, version: Optional[str], info: LicenseInfo) -> None:
        self._data[self._key(package, version)] = info.to_dict()
        self._save()


# ── Normalization ───────────────────────────────────────────────────────────

# Ordered most-specific first so LGPL does not accidentally match "GPL".
# The digit may be preceded by an optional "v" and separated by space/hyphen/underscore.
_LICENSE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AGPL-3.0",    re.compile(r"\bagpl[-_ ]?v?3|affero.*general.*public", re.I)),
    ("LGPL-3.0",    re.compile(r"\blgpl[-_ ]?v?3", re.I)),
    ("LGPL-2.1",    re.compile(r"\blgpl[-_ ]?v?2\.?1", re.I)),
    ("GPL-3.0",     re.compile(r"\bgpl[-_ ]?v?3|gnu general public.*3", re.I)),
    ("GPL-2.0",     re.compile(r"\bgpl[-_ ]?v?2|gnu general public.*2", re.I)),
    ("MPL-2.0",     re.compile(r"\bmpl[-_ ]?v?2|mozilla public license.*2", re.I)),
    ("Apache-2.0",  re.compile(r"\bapache(.*2|[-_ ]?license[-_ ]?2)?", re.I)),
    ("BSD-3-Clause", re.compile(r"\bbsd[-_ ]?3|new bsd|modified bsd", re.I)),
    ("BSD-2-Clause", re.compile(r"\bbsd[-_ ]?2|simplified bsd|freebsd", re.I)),
    ("BSD",         re.compile(r"\bbsd\b", re.I)),
    ("MIT",         re.compile(r"\bmit\b", re.I)),
    ("ISC",         re.compile(r"\bisc\b", re.I)),
    ("Unlicense",   re.compile(r"\bunlicense\b", re.I)),
    ("CC0-1.0",     re.compile(r"\bcc0\b", re.I)),
    ("Proprietary", re.compile(r"\bproprietary|commercial[-_ ]?license\b", re.I)),
]

# Canonical SPDX-ish forms — used to preserve the registry-facing casing when
# the PyPI value is already an SPDX expression.
_CANONICAL_TOKENS = {token for token, _ in _LICENSE_PATTERNS}


def normalize_license(raw: Optional[str]) -> str:
    """Map a freeform license string to an SPDX-ish token, or 'UNKNOWN'."""
    if not raw:
        return "UNKNOWN"
    s = raw.strip()
    if not s:
        return "UNKNOWN"
    # Already an exact SPDX expression we recognize?
    for token in _CANONICAL_TOKENS:
        if s.lower() == token.lower():
            return token
    for token, pattern in _LICENSE_PATTERNS:
        if pattern.search(s):
            return token
    return "UNKNOWN"


# ── PyPI fetch ─────────────────────────────────────────────────────────────


def _extract_license_from_pypi_json(payload: dict) -> Optional[str]:
    info = payload.get("info") or {}
    # Preferred: explicit `license` field.
    license_val = info.get("license")
    if license_val and isinstance(license_val, str) and license_val.strip():
        return license_val.strip()
    # Fallback: trove classifiers like "License :: OSI Approved :: MIT License"
    classifiers = info.get("classifiers") or []
    for c in classifiers:
        if isinstance(c, str) and c.startswith("License ::"):
            return c.split("::")[-1].strip()
    return None


def fetch_license_from_pypi(
    package: str, version: Optional[str] = None, timeout: float = FETCH_TIMEOUT_SECONDS
) -> Optional[str]:
    """Return the raw license string from PyPI, or None on error.

    Kept narrow and synchronous — the hook caches results so this runs at
    most once per (package, version) per developer machine.
    """
    if version:
        url = PYPI_URL_WITH_VERSION_TEMPLATE.format(package=package, version=version)
    else:
        url = PYPI_URL_TEMPLATE.format(package=package)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        return None
    return _extract_license_from_pypi_json(data)


# ── Policy evaluation ──────────────────────────────────────────────────────


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
        policy = (policies.unknown_license_policy or "block").lower()
        if policy == "allow":
            return LicenseVerdict(
                action=Action.ALLOW,
                license=license_normalized,
                reason="License could not be determined; unknown_license_policy=allow.",
            )
        # "warn" and "block" both resolve to BLOCK here — the hook decides
        # whether to soften the message. Defaulting to block means unclassified
        # licenses never slip through silently.
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
            "add it to global_policies.auto_approve_licenses in the registry "
            "if it should be permitted."
        ),
    )


def resolve_license(
    package: str,
    version: Optional[str],
    cache: LicenseCache,
    policies: GlobalPolicies,
) -> tuple[LicenseInfo, LicenseVerdict]:
    """Resolve (cache-or-fetch) a package's license and evaluate policy."""
    cached = cache.get(package, version)
    if cached is None:
        raw = fetch_license_from_pypi(package, version)
        normalized = normalize_license(raw)
        info = LicenseInfo(
            license_raw=raw,
            license_normalized=normalized,
            fetched_at=datetime.date.today(),
            source="pypi",
        )
        cache.put(package, version, info)
    else:
        info = cached
    verdict = evaluate_license(info.license_normalized, policies)
    return info, verdict
