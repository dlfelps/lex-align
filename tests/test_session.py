"""Tests for session state tracking and event logging."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lex_align.session import (
    EventLogger,
    SessionState,
    clear_current_session,
    get_current_session_id,
    set_current_session_id,
)


@pytest.fixture
def sessions_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sessions"
    d.mkdir()
    return d


def test_session_state_initial(sessions_dir: Path):
    state = SessionState(sessions_dir, "sess-001")
    assert state.unresolved_dep_changes() == []
    assert not state.has_observed_prompt_fired("ADR-0001")


def test_record_dep_change(sessions_dir: Path):
    state = SessionState(sessions_dir, "sess-001")
    state.record_dep_change(["redis", "fastapi"])
    assert "redis" in state.unresolved_dep_changes()
    assert "fastapi" in state.unresolved_dep_changes()


def test_unresolved_after_propose(sessions_dir: Path):
    state = SessionState(sessions_dir, "sess-001")
    state.record_dep_change(["redis", "fastapi"])
    state.record_propose_called(["redis"])
    unresolved = state.unresolved_dep_changes()
    assert "redis" not in unresolved
    assert "fastapi" in unresolved


def test_record_observed_prompt(sessions_dir: Path):
    state = SessionState(sessions_dir, "sess-001")
    state.record_observed_prompt("ADR-0001")
    assert state.has_observed_prompt_fired("ADR-0001")
    assert not state.has_observed_prompt_fired("ADR-0002")


def test_state_persists_across_instances(sessions_dir: Path):
    state1 = SessionState(sessions_dir, "sess-001")
    state1.record_dep_change(["redis"])
    state1.record_observed_prompt("ADR-0001")

    state2 = SessionState(sessions_dir, "sess-001")
    assert "redis" in state2.unresolved_dep_changes()
    assert state2.has_observed_prompt_fired("ADR-0001")


def test_state_does_not_share_between_sessions(sessions_dir: Path):
    state1 = SessionState(sessions_dir, "sess-001")
    state1.record_dep_change(["redis"])

    state2 = SessionState(sessions_dir, "sess-002")
    assert state2.unresolved_dep_changes() == []


def test_state_cleanup(sessions_dir: Path):
    state = SessionState(sessions_dir, "sess-001")
    state.record_dep_change(["redis"])
    state.cleanup()
    assert not state._state_file.exists()


def test_event_logger_writes_jsonl(sessions_dir: Path):
    logger = EventLogger(sessions_dir, "sess-001")
    logger.log_voluntary("show", ["ADR-0001"])
    logger.log_voluntary("considered", ["redis"])
    logger.log_automated("reconciliation", ["fastapi"])

    log_file = sessions_dir / "sess-001.jsonl"
    assert log_file.exists()
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 3

    entry = json.loads(lines[0])
    assert entry["event_type"] == "voluntary"
    assert entry["command"] == "show"
    assert entry["targets"] == ["ADR-0001"]
    assert entry["session_id"] == "sess-001"
    assert "timestamp" in entry
    assert "event_id" in entry


def test_event_logger_automated(sessions_dir: Path):
    logger = EventLogger(sessions_dir, "sess-001")
    logger.log_automated("reconciliation", ["redis"])

    log_file = sessions_dir / "sess-001.jsonl"
    lines = log_file.read_text().strip().splitlines()
    entry = json.loads(lines[0])
    assert entry["event_type"] == "automated"
    assert entry["command"] == "reconciliation"


def test_current_session_roundtrip(sessions_dir: Path):
    set_current_session_id(sessions_dir, "test-session-123")
    assert get_current_session_id(sessions_dir) == "test-session-123"
    clear_current_session(sessions_dir)
    assert get_current_session_id(sessions_dir) is None


def test_get_current_session_missing(sessions_dir: Path):
    assert get_current_session_id(sessions_dir) is None
