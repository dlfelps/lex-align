"""Tests for report generation."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lex_align.models import Confidence, Decision, Provenance, Scope, Status
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

    for i, (name, prov) in enumerate(
        [("fastapi", Provenance.RECONCILIATION), ("redis", Provenance.MANUAL)],
        start=1,
    ):
        store.save(Decision(
            id=f"ADR-{i:04d}",
            title=f"Uses {name}",
            status=Status.OBSERVED,
            created=dt.date.today(),
            confidence=Confidence.MEDIUM,
            scope=Scope(tags=[name]),
            provenance=prov,
        ))

    report = generate_report(sessions_dir, store)
    assert "via reconciliation" in report
    assert "via manual" in report
    assert "Observed entries: 2" in report


def test_generate_report_enforcement_section(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()
    store = DecisionStore(decisions_dir)

    _write_event(sessions_dir, "s1", "automated", "enforcement-block", ["pyqt5"])
    _write_event(sessions_dir, "s1", "automated", "enforcement-allow", ["httpx"])
    _write_event(sessions_dir, "s1", "automated", "enforcement-license-block", ["gpllib"])

    report = generate_report(sessions_dir, store)
    assert "Enforcement" in report
    assert "preferred auto-approvals:   1" in report
    assert "registry blocks:            1" in report
    assert "license blocks:             1" in report


def test_generate_report_writes_breakdown_by_provenance(tmp_path: Path):
    import datetime as dt
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()
    store = DecisionStore(decisions_dir)

    store.save(Decision(
        id="ADR-0001", title="Use httpx", status=Status.ACCEPTED,
        created=dt.date.today(), confidence=Confidence.HIGH, scope=Scope(tags=["httpx"]),
        provenance=Provenance.REGISTRY_PREFERRED,
    ))
    store.save(Decision(
        id="ADR-0002", title="Blocked: add pyqt5", status=Status.REJECTED,
        created=dt.date.today(), confidence=Confidence.HIGH, scope=Scope(tags=["pyqt5"]),
        provenance=Provenance.REGISTRY_BLOCKED,
    ))

    report = generate_report(sessions_dir, store)
    assert "auto (registry preferred):  1" in report
    assert "blocked attempts:           1" in report


def test_generate_report_with_since(sessions_dir: Path, tmp_path: Path):
    store = DecisionStore(tmp_path / "decisions")
    (tmp_path / "decisions").mkdir()
    _write_event(sessions_dir, "s1", "voluntary", "show", ["ADR-0001"])
    report = generate_report(sessions_dir, store, since_str="1 week ago")
    assert "1 week ago" in report or "since" in report.lower()
