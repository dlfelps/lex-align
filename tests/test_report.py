"""Tests for report generation."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lex_align.models import Confidence, Decision, ObservedVia, Scope, Status
from lex_align.report import generate_report, load_events, parse_since
from lex_align.session import EventLogger
from lex_align.store import DecisionStore


@pytest.fixture
def sessions_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sessions"
    d.mkdir()
    return d


def _write_event(sessions_dir: Path, session_id: str, event_type: str, command: str, targets=None):
    logger = EventLogger(sessions_dir, session_id)
    logger.log(event_type, command, targets)


def test_parse_since_days_ago():
    result = parse_since("3 days ago")
    expected = datetime.now(tz=timezone.utc) - timedelta(days=3)
    assert abs((result - expected).total_seconds()) < 5


def test_parse_since_weeks_ago():
    result = parse_since("2 weeks ago")
    expected = datetime.now(tz=timezone.utc) - timedelta(weeks=2)
    assert abs((result - expected).total_seconds()) < 5


def test_parse_since_iso_date():
    result = parse_since("2026-01-01")
    assert result is not None
    assert result.year == 2026


def test_parse_since_invalid():
    result = parse_since("not a date")
    assert result is None


def test_load_events_empty(sessions_dir: Path):
    events = load_events(sessions_dir)
    assert events == []


def test_load_events_basic(sessions_dir: Path):
    _write_event(sessions_dir, "sess-001", "voluntary", "show", ["ADR-0001"])
    _write_event(sessions_dir, "sess-001", "voluntary", "plan", ["add a redis cache"])
    events = load_events(sessions_dir)
    assert len(events) == 2


def test_load_events_filters_by_since(sessions_dir: Path):
    # Write events (they'll have current timestamps)
    _write_event(sessions_dir, "sess-001", "voluntary", "show", ["ADR-0001"])
    # Filter to future - should get 0
    since = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    events = load_events(sessions_dir, since=since)
    assert len(events) == 0


def test_generate_report_empty(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    store = DecisionStore(tmp_path / "decisions")
    (tmp_path / "decisions").mkdir()
    report = generate_report(sessions_dir, store)
    assert "Retrieval" in report
    assert "Writes" in report
    assert "Integrity" in report


def test_generate_report_counts_voluntary(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    store = DecisionStore(tmp_path / "decisions")
    (tmp_path / "decisions").mkdir()

    _write_event(sessions_dir, "s1", "voluntary", "show", ["ADR-0001"])
    _write_event(sessions_dir, "s1", "voluntary", "show", ["ADR-0002"])
    _write_event(sessions_dir, "s1", "voluntary", "plan", ["add a redis cache"])
    _write_event(sessions_dir, "s1", "voluntary", "propose", ["ADR-0003"])

    report = generate_report(sessions_dir, store)
    assert "3" in report  # total retrieval
    assert "1" in report  # propose count


def test_generate_report_observed_breakdown(tmp_path: Path):
    import datetime as dt
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()
    store = DecisionStore(decisions_dir)

    seed_d = Decision(
        id="ADR-0001",
        title="Uses fastapi",
        status=Status.OBSERVED,
        created=dt.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["fastapi"]),
        observed_via=ObservedVia.SEED,
    )
    recon_d = Decision(
        id="ADR-0002",
        title="Uses redis",
        status=Status.OBSERVED,
        created=dt.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        observed_via=ObservedVia.RECONCILIATION,
    )
    store.save(seed_d)
    store.save(recon_d)

    report = generate_report(sessions_dir, store)
    assert "via seed" in report
    assert "via reconciliation" in report
    assert "Observed entries: 2" in report


def test_generate_report_with_since(sessions_dir: Path, tmp_path: Path):
    store = DecisionStore(tmp_path / "decisions")
    (tmp_path / "decisions").mkdir()
    _write_event(sessions_dir, "s1", "voluntary", "show", ["ADR-0001"])
    report = generate_report(sessions_dir, store, since_str="1 week ago")
    assert "1 week ago" in report or "since" in report.lower()
