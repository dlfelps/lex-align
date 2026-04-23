from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Status(str, Enum):
    ACCEPTED = "accepted"
    OBSERVED = "observed"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Outcome(str, Enum):
    CHOSEN = "chosen"
    NOT_CHOSEN = "not-chosen"
    REJECTED = "rejected"


class Reversible(str, Enum):
    CHEAP = "cheap"
    COSTLY = "costly"
    NO = "no"


class Provenance(str, Enum):
    """How a decision entered the store."""
    RECONCILIATION = "reconciliation"              # observed, auto-created post-edit
    MANUAL = "manual"                              # observed or accepted, direct agent/user action
    REGISTRY_PREFERRED = "registry_preferred"      # accepted, auto-written for a registry "preferred" pkg
    REGISTRY_APPROVED = "registry_approved"        # accepted, agent proposed on a registry "approved" pkg
    LICENSE_AUTO_APPROVE = "license_auto_approve"  # accepted, auto-written under license policy
    REGISTRY_BLOCKED = "registry_blocked"          # rejected, paper trail of a blocked attempt


@dataclass
class Alternative:
    name: str
    outcome: Outcome
    reason: str
    reversible: Reversible
    constraint: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "reversible": self.reversible.value,
        }
        if self.constraint:
            d["constraint"] = self.constraint
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Alternative":
        return cls(
            name=d["name"],
            outcome=Outcome(d["outcome"]),
            reason=d["reason"],
            reversible=Reversible(d["reversible"]),
            constraint=d.get("constraint"),
        )


@dataclass
class Scope:
    tags: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"tags": self.tags, "paths": self.paths}

    @classmethod
    def from_dict(cls, d: dict) -> "Scope":
        if not d:
            return cls()
        return cls(tags=list(d.get("tags") or []), paths=list(d.get("paths") or []))


@dataclass
class Decision:
    id: str
    title: str
    status: Status
    created: datetime.date
    confidence: Confidence
    scope: Scope
    alternatives: list[Alternative] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    superseded_by: list[str] = field(default_factory=list)
    constraints_depended_on: list[str] = field(default_factory=list)
    provenance: Optional[Provenance] = None
    license: Optional[str] = None
    license_checked_at: Optional[datetime.date] = None
    version_constraint: Optional[str] = None
    registry_version: Optional[str] = None
    context_text: str = ""
    decision_text: str = ""
    consequences_text: str = ""

    @property
    def num(self) -> int:
        return int(self.id.split("-")[1])

    @property
    def slug(self) -> str:
        s = self.title.lower()
        s = re.sub(r"[^a-z0-9\s-]", "", s)
        s = re.sub(r"\s+", "-", s.strip())
        s = re.sub(r"-+", "-", s)
        return s[:60]

    @property
    def filename(self) -> str:
        return f"{self.id}-{self.slug}.md"

    def to_frontmatter(self) -> dict:
        fm: dict = {
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "created": self.created.isoformat(),
            "confidence": self.confidence.value,
            "scope": self.scope.to_dict(),
        }
        if self.supersedes:
            fm["supersedes"] = self.supersedes
        if self.superseded_by:
            fm["superseded_by"] = self.superseded_by
        if self.constraints_depended_on:
            fm["constraints_depended_on"] = self.constraints_depended_on
        if self.alternatives:
            fm["alternatives"] = [a.to_dict() for a in self.alternatives]
        if self.provenance is not None:
            fm["provenance"] = self.provenance.value
        if self.license is not None:
            fm["license"] = self.license
        if self.license_checked_at is not None:
            fm["license_checked_at"] = self.license_checked_at.isoformat()
        if self.version_constraint is not None:
            fm["version_constraint"] = self.version_constraint
        if self.registry_version is not None:
            fm["registry_version"] = self.registry_version
        return fm

    @classmethod
    def from_frontmatter(cls, fm: dict, body: str) -> "Decision":
        context_text, decision_text, consequences_text = _parse_body(body)
        return cls(
            id=fm["id"],
            title=fm["title"],
            status=Status(fm["status"]),
            created=_parse_date(fm["created"]),
            confidence=Confidence(fm.get("confidence", "medium")),
            scope=Scope.from_dict(fm.get("scope") or {}),
            alternatives=[Alternative.from_dict(a) for a in fm.get("alternatives") or []],
            supersedes=list(fm.get("supersedes") or []),
            superseded_by=list(fm.get("superseded_by") or []),
            constraints_depended_on=list(fm.get("constraints_depended_on") or []),
            provenance=Provenance(fm["provenance"]) if fm.get("provenance") else None,
            license=fm.get("license"),
            license_checked_at=_parse_date(fm["license_checked_at"]) if fm.get("license_checked_at") else None,
            version_constraint=fm.get("version_constraint"),
            registry_version=fm.get("registry_version"),
            context_text=context_text,
            decision_text=decision_text,
            consequences_text=consequences_text,
        )


def _parse_date(value) -> datetime.date:
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value))


def _parse_body(body: str) -> tuple[str, str, str]:
    """Extract ## Context, ## Decision, ## Consequences sections from markdown body."""
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current = stripped[3:].strip().lower()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)

    def _text(key: str) -> str:
        return "\n".join(sections.get(key, [])).strip()

    return _text("context"), _text("decision"), _text("consequences")
