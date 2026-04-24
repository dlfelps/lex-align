"""Shared fixtures for lex-align tests."""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from lex_align.models import (
    Confidence,
    Decision,
    Provenance,
    Scope,
    Status,
)
from lex_align.store import DecisionStore


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """A temporary project root with .lex-align initialized."""
    (tmp_path / ".lex-align" / "decisions").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".lex-align" / "sessions").mkdir(parents=True, exist_ok=True)
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
        provenance=Provenance.RECONCILIATION,
    )


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


@pytest.fixture
def sample_registry_file(tmp_project: Path) -> Path:
    """A registry JSON with one example of each status plus license policy."""
    import json
    registry = {
        "version": "1.2",
        "global_policies": {
            "auto_approve_licenses": ["MIT", "Apache-2.0", "BSD-3-Clause"],
            "hard_ban_licenses": ["AGPL-3.0", "GPL-3.0", "LGPL-3.0"],
            "unknown_license_policy": "block",
        },
        "packages": {
            "httpx": {"status": "preferred", "reason": "Standard async HTTP client."},
            "requests": {
                "status": "deprecated", "replacement": "httpx",
                "reason": "Migrating to async.",
            },
            "pyqt5": {"status": "banned", "reason": "GPL."},
            "cryptography": {
                "status": "version-constrained", "min_version": "42.0.0",
                "reason": "CVE-2023-50782",
            },
            "flask": {"status": "approved", "reason": "Internal tools only."},
        },
    }
    path = tmp_project / "registry.json"
    path.write_text(json.dumps(registry))
    return path
