"""Enterprise registry: package policies and license rules.

The registry is the authoritative source of truth for which packages an AI
agent is allowed to add. It is loaded from a local JSON file (produced by
compiling the human-authored YAML registry source) and is consulted by the
PreToolUse hook on every pyproject.toml edit.

Registry resolution order (first hit wins):

1. `--registry <path>` flag on CLI commands.
2. `LEXALIGN_REGISTRY_FILE` environment variable.
3. `registry_file` value recorded in `.lex-align/config.json`.
4. `.lex-align/registry.json` as a convention default.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


CONFIG_FILE = "config.json"
DEFAULT_REGISTRY_FILENAME = "registry.json"
REGISTRY_ENV_VAR = "LEXALIGN_REGISTRY_FILE"


class PackageStatus(str, Enum):
    PREFERRED = "preferred"
    APPROVED = "approved"
    DEPRECATED = "deprecated"
    VERSION_CONSTRAINED = "version-constrained"
    BANNED = "banned"


class Action(str, Enum):
    """What the enforcement hook should do for a given package."""
    ALLOW = "allow"                    # write an auto-ADR and proceed
    REQUIRE_PROPOSE = "require_propose"  # allow but instruct the agent to propose
    BLOCK = "block"                    # hard-block the edit
    UNKNOWN = "unknown"                # not in registry; fall through to license check


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
        """Render min/max as a compact spec string, e.g. '>=2.0.0' or '>=2.0,<3.0'."""
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
    # Present in the v1.2 schema but treated as `hard_ban` by this implementation —
    # the human-review state is not yet wired up.
    require_human_review_licenses: list[str] = field(default_factory=list)
    unknown_license_policy: str = "block"  # block | warn | allow

    @classmethod
    def from_dict(cls, d: dict) -> "GlobalPolicies":
        return cls(
            auto_approve_licenses=list(d.get("auto_approve_licenses") or []),
            hard_ban_licenses=list(d.get("hard_ban_licenses") or []),
            require_human_review_licenses=list(d.get("require_human_review_licenses") or []),
            unknown_license_policy=d.get("unknown_license_policy", "block"),
        )

    def effective_block_licenses(self) -> set[str]:
        """All licenses that should hard-block.

        Until the review flow is built, 'require human review' licenses are
        treated as hard-bans so no package slips through without enforcement.
        """
        return {lic.upper() for lic in self.hard_ban_licenses} | {
            lic.upper() for lic in self.require_human_review_licenses
        }

    def is_auto_approved(self, license: str) -> bool:
        return license.upper() in {lic.upper() for lic in self.auto_approve_licenses}

    def is_blocked(self, license: str) -> bool:
        return license.upper() in self.effective_block_licenses()


@dataclass
class PackageVerdict:
    """Outcome of evaluating a package against the registry."""
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
        return cls(
            version=str(data.get("version", "0")),
            global_policies=GlobalPolicies.from_dict(data.get("global_policies") or {}),
            packages={
                _normalize(name): PackageRule.from_dict(rule)
                for name, rule in (data.get("packages") or {}).items()
            },
            source_path=path,
        )

    def lookup(self, name: str, version: Optional[str] = None) -> PackageVerdict:
        """Evaluate a package (and optional version) against registry rules.

        Does NOT consult license policy — that is a separate concern handled
        after `lookup` returns `Action.UNKNOWN`.
        """
        rule = self.packages.get(_normalize(name))
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
        # Shouldn't be reachable — new statuses should extend this.
        return PackageVerdict(action=Action.UNKNOWN)


def _normalize(name: str) -> str:
    """Match reconciler._normalize_name so registry keys and dep names align."""
    return name.strip().lower().replace("-", "_").replace(".", "_")


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse a version string into a numeric tuple, ignoring pre-release suffixes."""
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


# ── Config & discovery ──────────────────────────────────────────────────────


def _config_path(project_root: Path) -> Path:
    return project_root / ".lex-align" / CONFIG_FILE


def load_config(project_root: Path) -> dict:
    path = _config_path(project_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_config(project_root: Path, config: dict) -> None:
    path = _config_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")


def resolve_registry_path(
    project_root: Path, cli_flag: Optional[str] = None
) -> Optional[Path]:
    """Resolve the registry file to use, in the documented order."""
    if cli_flag:
        return Path(cli_flag).expanduser().resolve()
    env_val = os.environ.get(REGISTRY_ENV_VAR)
    if env_val:
        return Path(env_val).expanduser().resolve()
    config = load_config(project_root)
    configured = config.get("registry_file")
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = (project_root / candidate).resolve()
        return candidate
    default = project_root / ".lex-align" / DEFAULT_REGISTRY_FILENAME
    if default.exists():
        return default
    return None


def load_registry(
    project_root: Path, cli_flag: Optional[str] = None
) -> Optional[Registry]:
    """Resolve and load the registry, returning None if none is configured."""
    path = resolve_registry_path(project_root, cli_flag)
    if path is None or not path.exists():
        return None
    return Registry.load(path)
