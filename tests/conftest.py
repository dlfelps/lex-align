"""Shared fixtures for lex-align tests."""
from __future__ import annotations

import datetime
from pathlib import Path

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
from lex_align.store import DecisionStore


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """A temporary project root with .lex-align initialized."""
    (tmp_path / ".lex-align" / "decisions").mkdir(parents=True)
    (tmp_path / ".lex-align" / "sessions").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def store(tmp_project: Path) -> DecisionStore:
    return DecisionStore(tmp_project / ".lex-align" / "decisions")


@pytest.fixture
def sample_decision() -> Decision:
    return Decision(
        id="ADR-0001",
        title="Use Pytest for testing",
        status=Status.ACCEPTED,
        created=datetime.date(2026, 1, 15),
        confidence=Confidence.HIGH,
        scope=Scope(tags=["testing"], paths=["tests/**"]),
        context_text="We needed a test framework.",
        decision_text="We chose pytest.",
        consequences_text="Tests are easier to write.",
    )


@pytest.fixture
def observed_decision() -> Decision:
    return Decision(
        id="ADR-0002",
        title="Uses redis",
        status=Status.OBSERVED,
        created=datetime.date(2026, 1, 10),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        observed_via=ObservedVia.SEED,
    )


@pytest.fixture
def mock_llm(mocker):
    """Mock the LLM client used by propose and promote commands."""
    client = mocker.MagicMock()
    client.generate_adr_body.return_value = (
        "Context: a background situation.",
        "Decision: we chose this approach.",
        "Consequences: positive and negative effects.",
    )
    client.generate_promotion_body.return_value = (
        "Context: why this dependency exists.",
        "Decision: adopted this library.",
        "Consequences: enables certain features.",
    )
    mocker.patch("lex_align.llm.get_client", return_value=client)
    return client


@pytest.fixture
def pyproject_toml(tmp_project: Path) -> Path:
    """A pyproject.toml with sample runtime dependencies."""
    content = """\
[project]
name = "my-app"
version = "0.1.0"
dependencies = [
    "fastapi>=0.100",
    "redis>=4.0",
    "sqlalchemy>=2.0",
]
"""
    path = tmp_project / "pyproject.toml"
    path.write_text(content)
    return path
