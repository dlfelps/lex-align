"""Tests for the cold-start compliance check."""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lex_align import compliance as compliance_mod
from lex_align.cli import main
from lex_align.licenses import LicenseCache, LicenseInfo
from lex_align.models import Provenance, Status
from lex_align.registry import load_registry, save_config
from lex_align.store import DecisionStore


def _write_pyproject(project_root: Path, deps: list[str]) -> Path:
    body = "[project]\nname = 'demo'\nversion = '0.1.0'\ndependencies = [\n"
    body += "".join(f'    "{d}",\n' for d in deps)
    body += "]\n"
    path = project_root / "pyproject.toml"
    path.write_text(body)
    return path


def _seed_license_cache(project_root: Path, packages: dict[str, str]) -> LicenseCache:
    cache = LicenseCache(project_root / ".lex-align" / "license-cache.json")
    for package, normalized in packages.items():
        cache.put(
            package, None,
            LicenseInfo(
                license_raw=normalized,
                license_normalized=normalized,
                fetched_at=datetime.date.today(),
                source="pypi",
            ),
        )
    return cache


@pytest.fixture
def configured_project(tmp_project: Path, sample_registry_file: Path) -> Path:
    save_config(tmp_project, {"registry_file": str(sample_registry_file)})
    return tmp_project


# ── analyze ─────────────────────────────────────────────────────────────────


def test_analyze_buckets_preferred_as_auto_accepted(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["httpx>=0.28.0"])
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    cache = LicenseCache(configured_project / ".lex-align" / "license-cache.json")
    registry = load_registry(configured_project)

    report = compliance_mod.analyze(pyproject, store, registry, cache)
    assert len(report.auto_accepted) == 1
    assert report.auto_accepted[0].name == "httpx"
    assert not report.blocked
    assert not report.needs_adr
    # Analyze writes nothing.
    assert store.load_all() == []


def test_analyze_buckets_banned_as_blocker(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["pyqt5>=5.15"])
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    cache = LicenseCache(configured_project / ".lex-align" / "license-cache.json")
    registry = load_registry(configured_project)

    report = compliance_mod.analyze(pyproject, store, registry, cache)
    assert len(report.blocked) == 1
    assert report.blocked[0].name == "pyqt5"
    assert report.blocked[0].status == "banned"


def test_analyze_buckets_deprecated_as_blocker_with_replacement(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["requests>=2.30"])
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    cache = LicenseCache(configured_project / ".lex-align" / "license-cache.json")
    registry = load_registry(configured_project)

    report = compliance_mod.analyze(pyproject, store, registry, cache)
    assert len(report.blocked) == 1
    assert report.blocked[0].replacement == "httpx"


def test_analyze_buckets_approved_as_needs_adr(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["flask>=3.0"])
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    cache = LicenseCache(configured_project / ".lex-align" / "license-cache.json")
    registry = load_registry(configured_project)

    report = compliance_mod.analyze(pyproject, store, registry, cache)
    assert len(report.needs_adr) == 1
    assert report.needs_adr[0].name == "flask"
    assert report.needs_adr[0].status == "approved"


def test_analyze_unknown_with_permissive_license_auto_accepts(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["someobscurelib>=1.0"])
    cache = _seed_license_cache(configured_project, {"someobscurelib": "MIT"})
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    registry = load_registry(configured_project)

    report = compliance_mod.analyze(pyproject, store, registry, cache)
    assert len(report.auto_accepted) == 1
    assert report.auto_accepted[0].license == "MIT"


def test_analyze_unknown_with_gpl_blocks(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["somegpllib>=1.0"])
    cache = _seed_license_cache(configured_project, {"somegpllib": "GPL-3.0"})
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    registry = load_registry(configured_project)

    report = compliance_mod.analyze(pyproject, store, registry, cache)
    assert len(report.blocked) == 1
    assert report.blocked[0].license == "GPL-3.0"


def test_analyze_already_accepted_is_skipped(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["httpx>=0.28.0"])
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    cache = LicenseCache(configured_project / ".lex-align" / "license-cache.json")
    registry = load_registry(configured_project)

    # Pre-seed an accepted ADR so compliance treats it as already_covered.
    compliance_mod.seed(pyproject, store, registry, cache)

    report = compliance_mod.analyze(pyproject, store, registry, cache)
    assert len(report.already_covered) == 1
    assert not report.auto_accepted
    assert not report.needs_adr


def test_analyze_existing_observed_routed_to_needs_adr(configured_project: Path):
    from lex_align.store import create_observed
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    create_observed("httpx", store, Provenance.RECONCILIATION)
    pyproject = _write_pyproject(configured_project, ["httpx>=0.28.0"])
    cache = LicenseCache(configured_project / ".lex-align" / "license-cache.json")
    registry = load_registry(configured_project)

    report = compliance_mod.analyze(pyproject, store, registry, cache)
    assert len(report.needs_adr) == 1
    assert report.needs_adr[0].adr_id == "ADR-0001"
    # Even though httpx is preferred, the prior observed entry takes precedence.
    assert not report.auto_accepted


# ── seed ────────────────────────────────────────────────────────────────────


def test_seed_writes_accepted_adrs_for_preferred(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["httpx>=0.28.0"])
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    cache = LicenseCache(configured_project / ".lex-align" / "license-cache.json")
    registry = load_registry(configured_project)

    report = compliance_mod.seed(pyproject, store, registry, cache)
    assert report.seeded is True
    decisions = store.load_all()
    assert len(decisions) == 1
    assert decisions[0].status is Status.ACCEPTED
    assert decisions[0].provenance is Provenance.REGISTRY_PREFERRED
    assert report.auto_accepted[0].adr_id == decisions[0].id


def test_seed_writes_observed_for_approved(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["flask>=3.0"])
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    cache = LicenseCache(configured_project / ".lex-align" / "license-cache.json")
    registry = load_registry(configured_project)

    report = compliance_mod.seed(pyproject, store, registry, cache)
    decisions = store.load_all()
    assert len(decisions) == 1
    assert decisions[0].status is Status.OBSERVED
    assert decisions[0].provenance is Provenance.REGISTRY_APPROVED
    assert report.needs_adr[0].adr_id == decisions[0].id


def test_seed_with_blockers_writes_nothing(configured_project: Path):
    pyproject = _write_pyproject(
        configured_project, ["httpx>=0.28.0", "pyqt5>=5.15", "flask>=3.0"]
    )
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    cache = LicenseCache(configured_project / ".lex-align" / "license-cache.json")
    registry = load_registry(configured_project)

    report = compliance_mod.seed(pyproject, store, registry, cache)
    assert report.seeded is False
    assert report.blocked
    # Even though httpx and flask are eligible, NOTHING is written when a
    # blocker exists.
    assert store.load_all() == []


def test_seed_writes_license_adr_for_unknown_permissive(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["someobscurelib>=1.0"])
    cache = _seed_license_cache(configured_project, {"someobscurelib": "MIT"})
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    registry = load_registry(configured_project)

    report = compliance_mod.seed(pyproject, store, registry, cache)
    decisions = store.load_all()
    assert len(decisions) == 1
    assert decisions[0].provenance is Provenance.LICENSE_AUTO_APPROVE
    assert decisions[0].license == "MIT"


def test_seed_is_idempotent(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["httpx>=0.28.0"])
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    cache = LicenseCache(configured_project / ".lex-align" / "license-cache.json")
    registry = load_registry(configured_project)

    compliance_mod.seed(pyproject, store, registry, cache)
    second = compliance_mod.seed(pyproject, store, registry, cache)
    # Second run should find the existing accepted ADR and skip.
    assert len(store.load_all()) == 1
    assert second.passing
    assert len(second.already_covered) == 1


def test_passing_when_all_preferred(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["httpx>=0.28.0"])
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    cache = LicenseCache(configured_project / ".lex-align" / "license-cache.json")
    registry = load_registry(configured_project)

    report = compliance_mod.seed(pyproject, store, registry, cache)
    assert report.passing is True


def test_not_passing_when_approved_remains(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["httpx>=0.28.0", "flask>=3.0"])
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    cache = LicenseCache(configured_project / ".lex-align" / "license-cache.json")
    registry = load_registry(configured_project)

    report = compliance_mod.seed(pyproject, store, registry, cache)
    assert report.passing is False
    assert len(report.needs_adr) == 1


# ── format_report ───────────────────────────────────────────────────────────


def test_format_report_blockers_message(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["pyqt5>=5.15"])
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    cache = LicenseCache(configured_project / ".lex-align" / "license-cache.json")
    registry = load_registry(configured_project)

    report = compliance_mod.seed(pyproject, store, registry, cache)
    out = compliance_mod.format_report(report)
    assert "BLOCKERS" in out
    assert "Cannot seed" in out


def test_format_report_includes_agent_prompt_when_observed(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["flask>=3.0"])
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    cache = LicenseCache(configured_project / ".lex-align" / "license-cache.json")
    registry = load_registry(configured_project)

    report = compliance_mod.seed(pyproject, store, registry, cache)
    out = compliance_mod.format_report(report)
    assert "AGENT PROMPT" in out
    assert "flask" in out
    assert "lex-align promote" in out


def test_format_report_passing(configured_project: Path):
    pyproject = _write_pyproject(configured_project, ["httpx>=0.28.0"])
    store = DecisionStore(configured_project / ".lex-align" / "decisions")
    cache = LicenseCache(configured_project / ".lex-align" / "license-cache.json")
    registry = load_registry(configured_project)

    report = compliance_mod.seed(pyproject, store, registry, cache)
    out = compliance_mod.format_report(report)
    assert "PASSING" in out


# ── CLI integration ────────────────────────────────────────────────────────


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cli_project(tmp_path: Path, mocker, sample_registry_file_content: dict) -> Path:
    """Initialized project rooted at tmp_path with a registry on disk."""
    mocker.patch("lex_align.cli._FIRST_RUN_MARKER", tmp_path / ".marker")
    (tmp_path / ".marker").touch()
    (tmp_path / ".lex-align" / "decisions").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".lex-align" / "sessions").mkdir(parents=True, exist_ok=True)

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(sample_registry_file_content))
    save_config(tmp_path, {"registry_file": str(registry_path)})
    return tmp_path


@pytest.fixture
def sample_registry_file_content() -> dict:
    return {
        "version": "1.2",
        "global_policies": {
            "auto_approve_licenses": ["MIT", "Apache-2.0", "BSD-3-Clause"],
            "hard_ban_licenses": ["AGPL-3.0", "GPL-3.0", "LGPL-3.0"],
            "unknown_license_policy": "block",
        },
        "packages": {
            "httpx": {"status": "preferred", "reason": "Standard async HTTP client."},
            "requests": {"status": "deprecated", "replacement": "httpx", "reason": "Migrating to async."},
            "pyqt5": {"status": "banned", "reason": "GPL."},
            "flask": {"status": "approved", "reason": "Internal tools only."},
        },
    }


def test_cli_compliance_passing_exits_zero(runner: CliRunner, cli_project: Path, monkeypatch):
    monkeypatch.chdir(cli_project)
    _write_pyproject(cli_project, ["httpx>=0.28.0"])
    result = runner.invoke(main, ["compliance"])
    assert result.exit_code == 0, result.output
    assert "PASSING" in result.output


def test_cli_compliance_needs_adr_exits_one(runner: CliRunner, cli_project: Path, monkeypatch):
    monkeypatch.chdir(cli_project)
    _write_pyproject(cli_project, ["flask>=3.0"])
    result = runner.invoke(main, ["compliance"])
    assert result.exit_code == 1, result.output
    assert "NOT YET PASSING" in result.output
    assert "AGENT PROMPT" in result.output


def test_cli_compliance_blockers_exits_two(runner: CliRunner, cli_project: Path, monkeypatch):
    monkeypatch.chdir(cli_project)
    _write_pyproject(cli_project, ["pyqt5>=5.15"])
    result = runner.invoke(main, ["compliance"])
    assert result.exit_code == 2, result.output
    assert "BLOCKERS" in result.output
    # Nothing should have been written.
    store = DecisionStore(cli_project / ".lex-align" / "decisions")
    assert store.load_all() == []


def test_cli_compliance_dry_run_writes_nothing(runner: CliRunner, cli_project: Path, monkeypatch):
    monkeypatch.chdir(cli_project)
    _write_pyproject(cli_project, ["httpx>=0.28.0"])
    result = runner.invoke(main, ["compliance", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "WOULD AUTO-ACCEPT" in result.output
    store = DecisionStore(cli_project / ".lex-align" / "decisions")
    assert store.load_all() == []
