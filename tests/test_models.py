"""Tests for the Decision data model."""
from __future__ import annotations

import datetime

import pytest

from lex_align.models import (
    Alternative,
    Confidence,
    Decision,
    Provenance,
    Outcome,
    Reversible,
    Scope,
    Status,
    _parse_body,
)


def test_decision_slug():
    d = Decision(
        id="ADR-0001",
        title="Use Redis for Session Storage",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(),
    )
    assert d.slug == "use-redis-for-session-storage"


def test_decision_slug_strips_special_chars():
    d = Decision(
        id="ADR-0002",
        title="HTTP handlers: return Result[T]",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.HIGH,
        scope=Scope(),
    )
    assert "[" not in d.slug
    assert "]" not in d.slug


def test_decision_num():
    d = Decision(
        id="ADR-0047",
        title="Test",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.LOW,
        scope=Scope(),
    )
    assert d.num == 47


def test_decision_filename():
    d = Decision(
        id="ADR-0001",
        title="Use Pytest",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.HIGH,
        scope=Scope(),
    )
    assert d.filename == "ADR-0001-use-pytest.md"


def test_alternative_to_dict_roundtrip():
    alt = Alternative(
        name="Postgres sessions",
        outcome=Outcome.NOT_CHOSEN,
        reason="Too much load",
        reversible=Reversible.CHEAP,
        constraint=None,
    )
    d = alt.to_dict()
    restored = Alternative.from_dict(d)
    assert restored.name == alt.name
    assert restored.outcome == alt.outcome
    assert restored.reason == alt.reason
    assert restored.reversible == alt.reversible
    assert restored.constraint is None


def test_alternative_with_constraint():
    alt = Alternative(
        name="Sticky sessions",
        outcome=Outcome.REJECTED,
        reason="Incompatible with blue-green",
        reversible=Reversible.NO,
        constraint="blue-green-deploys",
    )
    d = alt.to_dict()
    assert d["constraint"] == "blue-green-deploys"
    restored = Alternative.from_dict(d)
    assert restored.constraint == "blue-green-deploys"


def test_scope_from_dict_empty():
    scope = Scope.from_dict({})
    assert scope.tags == []
    assert scope.paths == []


def test_scope_from_dict():
    scope = Scope.from_dict({"tags": ["auth", "session"], "paths": ["src/auth/**"]})
    assert scope.tags == ["auth", "session"]
    assert scope.paths == ["src/auth/**"]


def test_parse_body_extracts_sections():
    body = """## Context
Some background here.

## Decision
We decided to use X.

## Consequences
Good and bad results."""

    context, decision, consequences = _parse_body(body)
    assert context == "Some background here."
    assert decision == "We decided to use X."
    assert "Good and bad results" in consequences


def test_parse_body_missing_sections():
    context, decision, consequences = _parse_body("No sections here.")
    assert context == ""
    assert decision == ""
    assert consequences == ""


def test_decision_to_frontmatter_excludes_empty_lists():
    d = Decision(
        id="ADR-0001",
        title="Test",
        status=Status.ACCEPTED,
        created=datetime.date(2026, 1, 1),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["test"]),
    )
    fm = d.to_frontmatter()
    assert "supersedes" not in fm
    assert "superseded_by" not in fm
    assert "constraints_depended_on" not in fm
    assert "provenance" not in fm
    assert "license" not in fm


def test_decision_to_frontmatter_provenance():
    d = Decision(
        id="ADR-0002",
        title="Uses redis",
        status=Status.OBSERVED,
        created=datetime.date(2026, 1, 1),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        provenance=Provenance.RECONCILIATION,
    )
    fm = d.to_frontmatter()
    assert fm["provenance"] == "reconciliation"
    assert fm["status"] == "observed"


def test_decision_to_frontmatter_license_and_version():
    d = Decision(
        id="ADR-0010",
        title="Use httpx",
        status=Status.ACCEPTED,
        created=datetime.date(2026, 4, 1),
        confidence=Confidence.HIGH,
        scope=Scope(tags=["httpx"]),
        provenance=Provenance.REGISTRY_PREFERRED,
        license="BSD-3-Clause",
        license_checked_at=datetime.date(2026, 4, 1),
        version_constraint=">=0.28,<1.0",
        registry_version="1.2",
    )
    fm = d.to_frontmatter()
    assert fm["license"] == "BSD-3-Clause"
    assert fm["license_checked_at"] == "2026-04-01"
    assert fm["version_constraint"] == ">=0.28,<1.0"
    assert fm["registry_version"] == "1.2"
    # Round-trip
    restored = Decision.from_frontmatter(fm, "")
    assert restored.license == "BSD-3-Clause"
    assert restored.license_checked_at == datetime.date(2026, 4, 1)
    assert restored.version_constraint == ">=0.28,<1.0"
    assert restored.registry_version == "1.2"
    assert restored.provenance == Provenance.REGISTRY_PREFERRED


def test_decision_from_frontmatter():
    fm = {
        "id": "ADR-0005",
        "title": "Use FastAPI",
        "status": "accepted",
        "created": "2026-02-01",
        "confidence": "high",
        "scope": {"tags": ["api"], "paths": []},
        "alternatives": [
            {
                "name": "Flask",
                "outcome": "not-chosen",
                "reason": "No async support",
                "reversible": "cheap",
            }
        ],
    }
    body = "## Context\nWe needed a web framework.\n\n## Decision\nFastAPI chosen."
    decision = Decision.from_frontmatter(fm, body)
    assert decision.id == "ADR-0005"
    assert decision.status == Status.ACCEPTED
    assert decision.confidence == Confidence.HIGH
    assert len(decision.alternatives) == 1
    assert decision.alternatives[0].name == "Flask"
    assert decision.context_text == "We needed a web framework."
    assert "FastAPI chosen" in decision.decision_text
