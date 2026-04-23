"""Tests for DecisionStore file I/O."""
from __future__ import annotations

import datetime

import pytest

from lex_align.models import (
    Alternative,
    Confidence,
    Decision,
    ObservedVia,
    Outcome,
    Reversible,
    Scope,
    Status,
)
from lex_align.store import DecisionStore, create_observed, tokenize


def test_save_and_load(store: DecisionStore, sample_decision: Decision):
    store.save(sample_decision)
    loaded = store.load_all()
    assert len(loaded) == 1
    d = loaded[0]
    assert d.id == sample_decision.id
    assert d.title == sample_decision.title
    assert d.status == Status.ACCEPTED
    assert d.context_text == "We needed a test framework."
    assert d.decision_text == "We chose pytest."


def test_save_and_get_by_id(store: DecisionStore, sample_decision: Decision):
    store.save(sample_decision)
    d = store.get("ADR-0001")
    assert d is not None
    assert d.title == sample_decision.title


def test_get_case_insensitive(store: DecisionStore, sample_decision: Decision):
    store.save(sample_decision)
    assert store.get("adr-0001") is not None


def test_get_without_prefix(store: DecisionStore, sample_decision: Decision):
    store.save(sample_decision)
    assert store.get("0001") is not None


def test_get_nonexistent_returns_none(store: DecisionStore):
    assert store.get("ADR-9999") is None


def test_next_id_empty_store(store: DecisionStore):
    assert store.next_id() == "ADR-0001"


def test_next_id_increments(store: DecisionStore, sample_decision: Decision):
    store.save(sample_decision)
    assert store.next_id() == "ADR-0002"


def test_load_all_sorted(store: DecisionStore):
    for i, title in enumerate(["Alpha", "Beta", "Gamma"], start=1):
        d = Decision(
            id=f"ADR-{i:04d}",
            title=title,
            status=Status.ACCEPTED,
            created=datetime.date.today(),
            confidence=Confidence.MEDIUM,
            scope=Scope(),
        )
        store.save(d)
    decisions = store.load_all()
    assert [d.id for d in decisions] == ["ADR-0001", "ADR-0002", "ADR-0003"]


def test_save_with_alternatives(store: DecisionStore):
    decision = Decision(
        id="ADR-0001",
        title="Use Redis",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["session"]),
        alternatives=[
            Alternative(
                name="Postgres sessions",
                outcome=Outcome.NOT_CHOSEN,
                reason="Too slow",
                reversible=Reversible.CHEAP,
            ),
            Alternative(
                name="Memcached",
                outcome=Outcome.REJECTED,
                reason="No persistence",
                reversible=Reversible.NO,
                constraint="persistence-required",
            ),
        ],
    )
    store.save(decision)
    loaded = store.get("ADR-0001")
    assert len(loaded.alternatives) == 2
    assert loaded.alternatives[0].name == "Postgres sessions"
    assert loaded.alternatives[1].constraint == "persistence-required"


def test_save_observed_entry(store: DecisionStore, observed_decision: Decision):
    store.save(observed_decision)
    loaded = store.get("ADR-0002")
    assert loaded.status == Status.OBSERVED
    assert loaded.observed_via == ObservedVia.SEED


def test_find_covering_by_title(store: DecisionStore, sample_decision: Decision):
    store.save(sample_decision)
    # "pytest" appears in the title "Use Pytest for testing"
    results = store.find_covering("pytest")
    assert len(results) == 1
    assert results[0].id == "ADR-0001"


def test_find_covering_by_tag(store: DecisionStore):
    d = Decision(
        id="ADR-0001",
        title="Use Redis",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis", "cache"]),
    )
    store.save(d)
    results = store.find_covering("redis")
    assert len(results) == 1


def test_find_covering_excludes_superseded(store: DecisionStore):
    d = Decision(
        id="ADR-0001",
        title="Uses redis",
        status=Status.SUPERSEDED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
    )
    store.save(d)
    results = store.find_covering("redis")
    assert results == []


def test_save_updates_index(store: DecisionStore):
    d = Decision(
        id="ADR-0001",
        title="Use Redis for caching",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["cache"]),
    )
    store.save(d)
    index = store._load_index()
    assert "redis" in index
    assert "ADR-0001" in index["redis"]
    assert "cache" in index
    assert "ADR-0001" in index["cache"]


def test_rebuild_index(store: DecisionStore):
    for i, title in enumerate(["Use Redis", "Use Postgres"], start=1):
        d = Decision(
            id=f"ADR-{i:04d}",
            title=title,
            status=Status.ACCEPTED,
            created=datetime.date.today(),
            confidence=Confidence.MEDIUM,
            scope=Scope(),
        )
        store.save(d)
    # Wipe index manually, then rebuild
    store._save_index({})
    assert store._load_index() == {}
    store.rebuild_index()
    index = store._load_index()
    assert "redis" in index
    assert "postgres" in index


def test_search_by_terms_finds_matching(store: DecisionStore):
    d = Decision(
        id="ADR-0001",
        title="Use Redis for sessions",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["session"]),
    )
    store.save(d)
    results = store.search_by_terms({"redis"})
    assert len(results) == 1
    assert results[0].id == "ADR-0001"


def test_search_by_terms_no_match(store: DecisionStore, sample_decision: Decision):
    store.save(sample_decision)
    results = store.search_by_terms({"unknownxyz"})
    assert results == []


def test_search_by_terms_stemming(store: DecisionStore):
    d = Decision(
        id="ADR-0001",
        title="Use caching layer",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(),
    )
    store.save(d)
    # "caching" in title → stem → "cach"; searching "caches" should also stem → "cach"
    results = store.search_by_terms({"caching"})
    assert len(results) == 1


def test_history_by_tag(store: DecisionStore):
    for i, status in enumerate([Status.SUPERSEDED, Status.ACCEPTED], start=1):
        d = Decision(
            id=f"ADR-{i:04d}",
            title=f"Decision {i}",
            status=status,
            created=datetime.date(2026, i, 1),
            confidence=Confidence.MEDIUM,
            scope=Scope(tags=["auth"]),
        )
        store.save(d)
    results = store.history("auth")
    assert len(results) == 2
    assert results[0].created <= results[1].created


def test_check_constraint(store: DecisionStore):
    d = Decision(
        id="ADR-0001",
        title="Use Redis",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(),
        constraints_depended_on=["mobile-latency-sla"],
        alternatives=[
            Alternative("Memcached", Outcome.REJECTED, "No persistence", Reversible.NO, constraint="mobile-latency-sla"),
        ],
    )
    store.save(d)
    results = store.check_constraint("mobile-latency-sla")
    assert len(results) == 1
    decision, alt_matches = results[0]
    assert decision.id == "ADR-0001"
    assert len(alt_matches) == 1


def test_create_observed(store: DecisionStore):
    decision = create_observed("redis", store, ObservedVia.SEED)
    assert decision.status == Status.OBSERVED
    assert decision.observed_via == ObservedVia.SEED
    assert "redis" in decision.title.lower()
    loaded = store.get(decision.id)
    assert loaded is not None


def test_save_preserves_frontmatter_fields(store: DecisionStore):
    d = Decision(
        id="ADR-0001",
        title="Use Redis",
        status=Status.ACCEPTED,
        created=datetime.date(2026, 3, 15),
        confidence=Confidence.HIGH,
        scope=Scope(tags=["cache"], paths=["src/cache/**"]),
        supersedes=["ADR-0000"],
        constraints_depended_on=["latency-sla"],
    )
    store.save(d)
    loaded = store.get("ADR-0001")
    assert loaded.created == datetime.date(2026, 3, 15)
    assert loaded.supersedes == ["ADR-0000"]
    assert loaded.constraints_depended_on == ["latency-sla"]
    assert loaded.scope.paths == ["src/cache/**"]
