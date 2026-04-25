"""Enterprise registry: package policies, license rules, CVE threshold.

The registry is loaded from a local JSON file (compiled from the
human-authored YAML by `tools/compile_registry.py`). The server consults it
on every `/evaluate` call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class PackageStatus(str, Enum):
    PREFERRED = "preferred"
    APPROVED = "approved"
    DEPRECATED = "deprecated"
    VERSION_CONSTRAINED = "version-constrained"
    BANNED = "banned"


class Action(str, Enum):
    ALLOW = "allow"
    REQUIRE_PROPOSE = "require_propose"
    BLOCK = "block"
    UNKNOWN = "unknown"


@dataclass
class PackageRule:
    status: PackageStatus
    reason: Optional[str] = None
    replacement: Optional[str] = None
    min_version: Optional[str] = None
    max_version: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "PackageRule":
        return cls(
            status=PackageStatus(d["status"]),
            reason=d.get("reason"),
            replacement=d.get("replacement"),
            min_version=d.get("min_version"),
            max_version=d.get("max_version"),
        )

    def version_constraint_str(self) -> Optional[str]:
        parts = []
        if self.min_version:
            parts.append(f">={self.min_version}")
        if self.max_version:
            parts.append(f"<{self.max_version}")
        return ",".join(parts) if parts else None


@dataclass
class GlobalPolicies:
    auto_approve_licenses: list[str] = field(default_factory=list)
    hard_ban_licenses: list[str] = field(default_factory=list)
    require_human_review_licenses: list[str] = field(default_factory=list)
    unknown_license_policy: str = "block"  # block | warn | allow
    # CVSS-fraction threshold (0–1). A vulnerability whose CVSS score / 10
    # meets or exceeds this value is treated as a hard block.
    cve_threshold: float = 0.9

    @classmethod
    def from_dict(cls, d: dict) -> "GlobalPolicies":
        return cls(
            auto_approve_licenses=list(d.get("auto_approve_licenses") or []),
            hard_ban_licenses=list(d.get("hard_ban_licenses") or []),
            require_human_review_licenses=list(d.get("require_human_review_licenses") or []),
            unknown_license_policy=d.get("unknown_license_policy", "block"),
            cve_threshold=float(d.get("cve_threshold", 0.9)),
        )

    def effective_block_licenses(self) -> set[str]:
        return {lic.upper() for lic in self.hard_ban_licenses} | {
            lic.upper() for lic in self.require_human_review_licenses
        }

    def is_auto_approved(self, license: str) -> bool:
        return license.upper() in {lic.upper() for lic in self.auto_approve_licenses}

    def is_blocked(self, license: str) -> bool:
        return license.upper() in self.effective_block_licenses()

    def cve_blocks(self, max_cvss_score: Optional[float]) -> bool:
        if max_cvss_score is None:
            return False
        return max_cvss_score >= self.cve_threshold * 10.0


@dataclass
class PackageVerdict:
    action: Action
    status: Optional[PackageStatus] = None
    reason: Optional[str] = None
    replacement: Optional[str] = None
    version_constraint: Optional[str] = None


@dataclass
class Registry:
    version: str
    global_policies: GlobalPolicies
    packages: dict[str, PackageRule] = field(default_factory=dict)
    source_path: Optional[Path] = None

    @classmethod
    def load(cls, path: Path) -> "Registry":
        data = json.loads(path.read_text())
        return cls.from_dict(data, source_path=path)

    @classmethod
    def from_dict(cls, data: dict, source_path: Optional[Path] = None) -> "Registry":
        return cls(
            version=str(data.get("version", "0")),
            global_policies=GlobalPolicies.from_dict(data.get("global_policies") or {}),
            packages={
                normalize_name(name): PackageRule.from_dict(rule)
                for name, rule in (data.get("packages") or {}).items()
            },
            source_path=source_path,
        )

    def lookup(self, name: str, version: Optional[str] = None) -> PackageVerdict:
        rule = self.packages.get(normalize_name(name))
        if rule is None:
            return PackageVerdict(action=Action.UNKNOWN)

        vc = rule.version_constraint_str()

        if rule.status is PackageStatus.PREFERRED:
            return PackageVerdict(action=Action.ALLOW, status=rule.status, reason=rule.reason)
        if rule.status is PackageStatus.APPROVED:
            return PackageVerdict(
                action=Action.REQUIRE_PROPOSE, status=rule.status, reason=rule.reason
            )
        if rule.status is PackageStatus.DEPRECATED:
            return PackageVerdict(
                action=Action.BLOCK,
                status=rule.status,
                reason=rule.reason,
                replacement=rule.replacement,
            )
        if rule.status is PackageStatus.BANNED:
            return PackageVerdict(action=Action.BLOCK, status=rule.status, reason=rule.reason)
        if rule.status is PackageStatus.VERSION_CONSTRAINED:
            if version is not None and not _version_satisfies(version, rule):
                return PackageVerdict(
                    action=Action.BLOCK,
                    status=rule.status,
                    reason=rule.reason,
                    version_constraint=vc,
                )
            return PackageVerdict(
                action=Action.ALLOW,
                status=rule.status,
                reason=rule.reason,
                version_constraint=vc,
            )
        return PackageVerdict(action=Action.UNKNOWN)


def normalize_name(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(".", "_")


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = v.split("+", 1)[0].split("-", 1)[0].split(".")
    out: list[int] = []
    for p in parts:
        digits = ""
        for ch in p:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _version_satisfies(version: str, rule: PackageRule) -> bool:
    v = _version_tuple(version)
    if rule.min_version and v < _version_tuple(rule.min_version):
        return False
    if rule.max_version and v >= _version_tuple(rule.max_version):
        return False
    return True


def load_registry(path: Optional[Path]) -> Optional[Registry]:
    if path is None or not path.exists():
        return None
    return Registry.load(path)
