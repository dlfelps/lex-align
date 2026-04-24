"""Integration tests for CLI commands."""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lex_align.cli import main
from lex_align.models import Confidence, Decision, Provenance, Scope, Status
from lex_align.store import DecisionStore


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def initialized_project(tmp_path: Path, mocker) -> Path:
    # Skip the first-run privacy prompt globally for CLI tests.
    mocker.patch("lex_align.cli._FIRST_RUN_MARKER", tmp_path / ".marker")
    (tmp_path / ".marker").touch()
    (tmp_path / ".lex-align" / "decisions").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".lex-align" / "sessions").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def store_in_project(initialized_project: Path) -> DecisionStore:
    return DecisionStore(initialized_project / ".lex-align" / "decisions")


# ── init ──────────────────────────────────────────────────────────────────────

def test_init_creates_directories(runner: CliRunner, tmp_path: Path, mocker):
    mocker.patch("lex_align.cli._FIRST_RUN_MARKER", tmp_path / ".marker")
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        result = runner.invoke(main, ["init", "--yes"])
        assert result.exit_code == 0, result.output
        assert Path(td, ".lex-align", "decisions").exists()
        assert Path(td, ".lex-align", "sessions").exists()


def test_init_configures_hooks(runner: CliRunner, tmp_path: Path, mocker):
    mocker.patch("lex_align.cli._FIRST_RUN_MARKER", tmp_path / ".marker")
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        result = runner.invoke(main, ["init", "--yes"])
        assert result.exit_code == 0
        settings = json.loads(Path(td, ".claude", "settings.json").read_text())
        assert "hooks" in settings


def test_init_writes_gitignore_entries(runner: CliRunner, tmp_path: Path, mocker):
    mocker.patch("lex_align.cli._FIRST_RUN_MARKER", tmp_path / ".marker")
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        runner.invoke(main, ["init", "--yes"])
        content = Path(td, ".gitignore").read_text()
        assert ".lex-align/sessions/" in content
        assert ".lex-align/license-cache.json" in content


def test_init_runs_compliance_by_default(runner: CliRunner, tmp_path: Path, mocker):
    """With no registry, init seeds observed entries for existing deps."""
    mocker.patch("lex_align.cli._FIRST_RUN_MARKER", tmp_path / ".marker")
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        Path(td, "pyproject.toml").write_text(
            '[project]\ndependencies = ["fastapi>=0.100"]\n'
        )
        result = runner.invoke(main, ["init", "--yes"])
        assert result.exit_code == 0, result.output
        store = DecisionStore(Path(td, ".lex-align", "decisions"))
        decisions = store.load_all()
        assert len(decisions) == 1
        assert decisions[0].status.value == "observed"
        assert "fastapi" in decisions[0].scope.tags


def test_init_no_compliance_skips_seeding(runner: CliRunner, tmp_path: Path, mocker):
    mocker.patch("lex_align.cli._FIRST_RUN_MARKER", tmp_path / ".marker")
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        Path(td, "pyproject.toml").write_text(
            '[project]\ndependencies = ["fastapi>=0.100"]\n'
        )
        result = runner.invoke(main, ["init", "--yes", "--no-compliance"])
        assert result.exit_code == 0, result.output
        store = DecisionStore(Path(td, ".lex-align", "decisions"))
        assert store.load_all() == []


def test_init_compliance_with_registry_seeds_accepted(
    runner: CliRunner, tmp_path: Path, mocker, sample_registry_file: Path
):
    mocker.patch("lex_align.cli._FIRST_RUN_MARKER", tmp_path / ".marker")
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        Path(td, "pyproject.toml").write_text(
            '[project]\ndependencies = ["httpx>=0.28.0"]\n'
        )
        result = runner.invoke(
            main, ["init", "--yes", "--registry", str(sample_registry_file)]
        )
        assert result.exit_code == 0, result.output
        store = DecisionStore(Path(td, ".lex-align", "decisions"))
        decisions = store.load_all()
        assert len(decisions) == 1
        assert decisions[0].status.value == "accepted"
        assert "httpx" in decisions[0].scope.tags


def test_init_with_registry_records_path(runner: CliRunner, tmp_path: Path, mocker, sample_registry_file: Path):
    mocker.patch("lex_align.cli._FIRST_RUN_MARKER", tmp_path / ".marker")
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        result = runner.invoke(
            main, ["init", "--yes", "--registry", str(sample_registry_file)]
        )
        assert result.exit_code == 0, result.output
        config = json.loads(Path(td, ".lex-align", "config.json").read_text())
        assert "registry_file" in config


def test_first_run_audit_command_does_not_exist(runner: CliRunner):
    result = runner.invoke(main, ["first-run-audit"])
    assert result.exit_code != 0


# ── show ──────────────────────────────────────────────────────────────────────

def test_show_displays_decision(
    runner: CliRunner, initialized_project: Path, store_in_project: DecisionStore,
    sample_decision, mocker,
):
    store_in_project.save(sample_decision)
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["show", "ADR-0001"])
    assert result.exit_code == 0, result.output
    assert sample_decision.title in result.output


def test_show_nonexistent_fails(runner: CliRunner, initialized_project: Path, mocker):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["show", "ADR-9999"])
    assert result.exit_code != 0


def test_show_observed_includes_promote_hint(
    runner: CliRunner, initialized_project: Path, store_in_project: DecisionStore,
    observed_decision, mocker,
):
    store_in_project.save(observed_decision)
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["show", "ADR-0002"])
    assert result.exit_code == 0
    assert "promote" in result.output


# ── plan ──────────────────────────────────────────────────────────────────────

def test_plan_surfaces_registry_guidance_for_matching_terms(
    runner: CliRunner, initialized_project: Path, sample_registry_file: Path, mocker
):
    from lex_align.registry import save_config
    save_config(initialized_project, {"registry_file": str(sample_registry_file)})
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["plan", "need async http client like httpx"])
    assert result.exit_code == 0
    assert "REGISTRY GUIDANCE" in result.output
    assert "httpx" in result.output


def test_plan_surfaces_banned_in_registry_guidance(
    runner: CliRunner, initialized_project: Path, sample_registry_file: Path, mocker
):
    from lex_align.registry import save_config
    save_config(initialized_project, {"registry_file": str(sample_registry_file)})
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["plan", "use pyqt5 for the gui"])
    assert result.exit_code == 0
    assert "[banned]" in result.output
    assert "pyqt5" in result.output


# ── propose ───────────────────────────────────────────────────────────────────

def test_propose_writes_accepted_adr(
    runner: CliRunner, initialized_project: Path, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, [
        "propose",
        "--title", "Use Redis for session storage",
        "--context", "Mobile needs sub-100ms sessions.",
        "--decision", "Store sessions in Redis.",
        "--consequences", "Redis becomes load-bearing.",
        "--yes",
    ])
    assert result.exit_code == 0, result.output
    store = DecisionStore(initialized_project / ".lex-align" / "decisions")
    decisions = store.load_all()
    assert len(decisions) == 1
    assert decisions[0].status is Status.ACCEPTED
    assert decisions[0].title == "Use Redis for session storage"


def test_propose_requires_title(runner: CliRunner, initialized_project: Path, mocker):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["propose", "--yes"])
    assert result.exit_code != 0
    assert "title" in result.output.lower()


def test_propose_lists_all_missing_flags_in_one_error(
    runner: CliRunner, initialized_project: Path, mocker,
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["propose", "--yes"])
    assert result.exit_code != 0
    # All four required flags should be named — no more whack-a-mole.
    lower = result.output.lower()
    assert "--title" in lower
    assert "--context" in lower
    assert "--decision" in lower
    assert "--consequences" in lower


def test_propose_without_yes_on_non_tty_fails_fast(
    runner: CliRunner, initialized_project: Path, mocker,
):
    """Non-TTY stdin should auto-enable --yes and produce a clear usage error,
    never hang on click.prompt."""
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["propose"])  # no --yes, no flags
    assert result.exit_code != 0
    assert "non-interactive" in result.output.lower()
    assert "stdin is not a tty" in (result.output + result.stderr).lower() or \
           "--yes auto-enabled" in (result.output + result.stderr).lower()


def test_propose_refuses_banned_dependency(
    runner: CliRunner, initialized_project: Path, sample_registry_file: Path, mocker
):
    from lex_align.registry import save_config
    save_config(initialized_project, {"registry_file": str(sample_registry_file)})
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, [
        "propose",
        "--dependency", "pyqt5",
        "--title", "Add pyqt5",
        "--context", "x", "--decision", "y", "--consequences", "z",
        "--yes",
    ])
    assert result.exit_code != 0
    assert "banned" in result.output.lower()


def test_propose_refuses_deprecated_dependency(
    runner: CliRunner, initialized_project: Path, sample_registry_file: Path, mocker
):
    from lex_align.registry import save_config
    save_config(initialized_project, {"registry_file": str(sample_registry_file)})
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, [
        "propose",
        "--dependency", "requests",
        "--title", "Add requests",
        "--context", "x", "--decision", "y", "--consequences", "z",
        "--yes",
    ])
    assert result.exit_code != 0
    assert "deprecated" in result.output.lower()
    assert "httpx" in result.output


def test_propose_allows_preferred_dependency(
    runner: CliRunner, initialized_project: Path, sample_registry_file: Path, mocker
):
    from lex_align.registry import save_config
    save_config(initialized_project, {"registry_file": str(sample_registry_file)})
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, [
        "propose",
        "--dependency", "httpx",
        "--title", "Add httpx",
        "--context", "x", "--decision", "y", "--consequences", "z",
        "--yes",
    ])
    assert result.exit_code == 0, result.output


# ── promote ──────────────────────────────────────────────────────────────────

def test_promote_converts_observed_to_accepted(
    runner: CliRunner, initialized_project: Path, store_in_project: DecisionStore,
    observed_decision, mocker,
):
    store_in_project.save(observed_decision)
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, [
        "promote", "ADR-0002",
        "--context", "Redis chosen for low-latency lookup.",
        "--decision", "Use Redis.",
        "--consequences", "Fast sessions.",
        "--yes",
    ])
    assert result.exit_code == 0, result.output
    updated = store_in_project.get("ADR-0002")
    assert updated.status is Status.ACCEPTED
    assert "low-latency" in updated.context_text


def test_promote_rejects_non_observed(
    runner: CliRunner, initialized_project: Path, store_in_project: DecisionStore,
    sample_decision, mocker,
):
    store_in_project.save(sample_decision)
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, [
        "promote", "ADR-0001",
        "--context", "x",
        "--yes",
    ])
    assert result.exit_code != 0


def test_promote_without_yes_on_non_tty_fails_fast(
    runner: CliRunner, initialized_project: Path, store_in_project: DecisionStore,
    observed_decision, mocker,
):
    """Non-TTY stdin should auto-enable --yes and surface a usage error for
    missing --context, never hang on click.prompt."""
    store_in_project.save(observed_decision)
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["promote", "ADR-0002"])  # no --yes, no --context
    assert result.exit_code != 0
    assert "--context" in result.output or "context" in result.output.lower()


# ── registry ────────────────────────────────────────────────────────────────

def test_registry_show(runner: CliRunner, initialized_project: Path, sample_registry_file: Path, mocker):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["registry", "show", "--registry", str(sample_registry_file)])
    assert result.exit_code == 0
    assert "httpx" in result.output
    assert "auto_approve_licenses" in result.output


def test_registry_check_preferred(runner: CliRunner, initialized_project: Path, sample_registry_file: Path, mocker):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(
        main, ["registry", "check", "httpx", "--registry", str(sample_registry_file)]
    )
    assert result.exit_code == 0
    assert "allow" in result.output


def test_registry_check_unknown(runner: CliRunner, initialized_project: Path, sample_registry_file: Path, mocker):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(
        main, ["registry", "check", "notinlist", "--registry", str(sample_registry_file)]
    )
    assert result.exit_code == 0
    assert "unknown" in result.output


# ── history, check-constraint, doctor ─────────────────────────────────────────

def test_history_returns_decisions_by_tag(
    runner: CliRunner, initialized_project: Path, store_in_project: DecisionStore, mocker
):
    d = Decision(
        id="ADR-0001",
        title="Use Redis",
        status=Status.ACCEPTED,
        created=datetime.date(2026, 1, 1),
        confidence=Confidence.HIGH,
        scope=Scope(tags=["redis"]),
    )
    store_in_project.save(d)
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["history", "redis"])
    assert result.exit_code == 0
    assert "ADR-0001" in result.output


def test_doctor_repair_rebuilds_stale_index(
    runner: CliRunner, initialized_project: Path, store_in_project: DecisionStore,
    sample_decision, mocker,
):
    store_in_project.save(sample_decision)
    # Corrupt the index so it no longer reflects the decision on disk.
    (initialized_project / ".lex-align" / "index.json").write_text("{}")
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)

    # Without --repair, doctor reports the problem but does not fix it.
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "decision index" in result.output
    idx = json.loads((initialized_project / ".lex-align" / "index.json").read_text())
    assert idx == {}

    result = runner.invoke(main, ["doctor", "--repair"])
    assert result.exit_code == 0
    idx = json.loads((initialized_project / ".lex-align" / "index.json").read_text())
    assert any(sample_decision.id in ids for ids in idx.values())


# ── privacy ──────────────────────────────────────────────────────────────────

def test_privacy_displays_notice(runner: CliRunner):
    result = runner.invoke(main, ["privacy"])
    assert result.exit_code == 0
    assert "records architectural decisions" in result.output
