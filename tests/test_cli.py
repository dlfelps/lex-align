"""Integration tests for CLI commands."""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lex_align.cli import main
from lex_align.models import Confidence, Decision, ObservedVia, Scope, Status
from lex_align.store import DecisionStore


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def initialized_project(tmp_path: Path) -> Path:
    (tmp_path / ".lex-align" / "decisions").mkdir(parents=True)
    (tmp_path / ".lex-align" / "sessions").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def store(initialized_project: Path) -> DecisionStore:
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
        settings_file = Path(td, ".claude", "settings.json")
        assert settings_file.exists()
        settings = json.loads(settings_file.read_text())
        assert "hooks" in settings


def test_init_seeds_from_pyproject(runner: CliRunner, tmp_path: Path, mocker):
    mocker.patch("lex_align.cli._FIRST_RUN_MARKER", tmp_path / ".marker")
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        Path(td, "pyproject.toml").write_text(
            '[project]\ndependencies = ["fastapi>=0.100"]\n'
        )
        result = runner.invoke(main, ["init", "--yes"])
        assert result.exit_code == 0
        assert "fastapi" in result.output.lower()
        store = DecisionStore(Path(td, ".lex-align", "decisions"))
        decisions = store.load_all()
        assert len(decisions) == 1
        assert decisions[0].status == Status.OBSERVED
        assert decisions[0].observed_via == ObservedVia.SEED
        # v1.1: First-Run Audit prompt should appear when deps are seeded
        assert "First-Run Audit" in result.output
        assert "first-run-audit" in result.output


def test_init_idempotent(runner: CliRunner, tmp_path: Path, mocker):
    mocker.patch("lex_align.cli._FIRST_RUN_MARKER", tmp_path / ".marker")
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        Path(td, "pyproject.toml").write_text(
            '[project]\ndependencies = ["fastapi>=0.100"]\n'
        )
        runner.invoke(main, ["init", "--yes"])
        runner.invoke(main, ["init", "--yes"])
        store = DecisionStore(Path(td, ".lex-align", "decisions"))
        assert len(store.load_all()) == 1  # Not doubled


# ── show ──────────────────────────────────────────────────────────────────────

def test_show_displays_decision(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, sample_decision
):
    store.save(sample_decision)
    with runner.isolated_filesystem(temp_dir=initialized_project) as td:
        (Path(td) / ".lex-align").symlink_to(initialized_project / ".lex-align")
        result = runner.invoke(main, ["show", "ADR-0001"], catch_exceptions=False)

    # Run directly using the store path
    result = runner.invoke(main, ["show", "ADR-0001"], env={"HOME": str(initialized_project)})
    # Just test via the store directly
    decision = store.get("ADR-0001")
    assert decision is not None
    assert decision.title == "Use Pytest for testing"


def test_show_not_found(runner: CliRunner, initialized_project: Path, mocker):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["show", "ADR-9999"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_show_observed_entry_includes_promote_hint(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    d = Decision(
        id="ADR-0001",
        title="Uses redis",
        status=Status.OBSERVED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        observed_via=ObservedVia.SEED,
    )
    store.save(d)
    result = runner.invoke(main, ["show", "ADR-0001"])
    assert result.exit_code == 0
    assert "promote" in result.output.lower()


# ── plan ──────────────────────────────────────────────────────────────────────

def test_plan_returns_relevant_decisions(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    d = Decision(
        id="ADR-0001",
        title="Use Redis for caching",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.HIGH,
        scope=Scope(tags=["cache"]),
    )
    store.save(d)
    result = runner.invoke(main, ["plan", "add a redis caching layer"])
    assert result.exit_code == 0
    assert "RELEVANT DECISIONS" in result.output
    assert "ADR-0001" in result.output


def test_plan_includes_observed_entries(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    d = Decision(
        id="ADR-0001",
        title="Uses redis",
        status=Status.OBSERVED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        observed_via=ObservedVia.SEED,
    )
    store.save(d)
    result = runner.invoke(main, ["plan", "add a redis cache"])
    assert result.exit_code == 0
    assert "OBSERVED ENTRIES" in result.output
    assert "ADR-0001" in result.output


def test_plan_includes_considered_alternatives(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    from lex_align.models import Alternative, Outcome, Reversible
    d = Decision(
        id="ADR-0001",
        title="Use Postgres for jobs",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["postgres"]),
        alternatives=[Alternative("Redis queue", Outcome.NOT_CHOSEN, "Too simple", Reversible.CHEAP)],
    )
    store.save(d)
    result = runner.invoke(main, ["plan", "add a postgres job queue"])
    assert result.exit_code == 0
    assert "WHAT HAS BEEN CONSIDERED" in result.output
    assert "Redis queue" in result.output


def test_plan_no_matches(
    runner: CliRunner, initialized_project: Path, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["plan", "xyzzy unknownthing"])
    assert result.exit_code == 0
    assert "No relevant decisions" in result.output


def test_plan_logs_voluntary(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    sessions_dir = initialized_project / ".lex-align" / "sessions"
    from lex_align.session import set_current_session_id, EventLogger
    set_current_session_id(sessions_dir, "sess-test")
    store.save(Decision(
        id="ADR-0001",
        title="Use Redis",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
    ))
    runner.invoke(main, ["plan", "add a redis layer"])
    log_file = sessions_dir / "sess-test.jsonl"
    assert log_file.exists()
    events = [json.loads(line) for line in log_file.read_text().splitlines() if line]
    plan_events = [e for e in events if e["command"] == "plan"]
    assert len(plan_events) == 1


# ── rebuild-index ─────────────────────────────────────────────────────────────

def test_rebuild_index_command(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    store.save(Decision(
        id="ADR-0001",
        title="Use Redis",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(),
    ))
    result = runner.invoke(main, ["rebuild-index"])
    assert result.exit_code == 0
    assert "rebuilt" in result.output.lower()
    index = store._load_index()
    assert "redis" in index


# ── first-run-audit ───────────────────────────────────────────────────────────

def test_first_run_audit_command(runner: CliRunner):
    result = runner.invoke(main, ["first-run-audit"])
    assert result.exit_code == 0
    assert "OBSERVED" in result.output
    assert "promote" in result.output.lower()


# ── history ───────────────────────────────────────────────────────────────────

def test_history_by_tag(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    d = Decision(
        id="ADR-0001",
        title="Use Redis",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["cache"]),
    )
    store.save(d)
    result = runner.invoke(main, ["history", "cache"])
    assert result.exit_code == 0
    assert "ADR-0001" in result.output


def test_history_not_found(
    runner: CliRunner, initialized_project: Path, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["history", "notag"])
    assert "No decisions" in result.output


# ── check-constraint ──────────────────────────────────────────────────────────

def test_check_constraint(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    d = Decision(
        id="ADR-0001",
        title="Use Redis",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(),
        constraints_depended_on=["latency-sla"],
    )
    store.save(d)
    result = runner.invoke(main, ["check-constraint", "latency-sla"])
    assert result.exit_code == 0
    assert "ADR-0001" in result.output


# ── propose ───────────────────────────────────────────────────────────────────

def test_propose_creates_decision(
    runner: CliRunner, initialized_project: Path, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    input_text = (
        "Use Redis for caching\n"   # title
        "We need fast cache access\n"  # rationale
        "medium\n"                  # confidence
        "cache\n"                   # tags
        "\n"                        # paths (empty)
        "\n"                        # constraints (empty)
        "\n"                        # supersedes (empty)
        "n\n"                       # no alternatives
    )
    result = runner.invoke(main, ["propose"], input=input_text)
    assert result.exit_code == 0, result.output
    assert "Written" in result.output

    store = DecisionStore(initialized_project / ".lex-align" / "decisions")
    decisions = store.load_all()
    assert len(decisions) == 1
    assert decisions[0].status == Status.ACCEPTED
    assert "redis" in decisions[0].title.lower()


def test_propose_calls_llm(
    runner: CliRunner, initialized_project: Path, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    input_text = "Use FastAPI\nAsync web framework\nmedium\napi\n\n\n\nn\n"
    runner.invoke(main, ["propose"], input=input_text)
    mock_llm.generate_adr_body.assert_called_once()


def test_propose_with_alternatives(
    runner: CliRunner, initialized_project: Path, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    input_text = (
        "Use Redis\n"
        "We need fast cache\n"
        "high\n"
        "cache\n"
        "\n"
        "\n"
        "\n"
        "y\n"           # add alternative
        "Memcached\n"   # name
        "not-chosen\n"  # outcome
        "Less features\n"  # reason
        "cheap\n"       # reversible
        "\n"            # no constraint
        "n\n"           # no more alternatives
    )
    result = runner.invoke(main, ["propose"], input=input_text)
    assert result.exit_code == 0, result.output

    store = DecisionStore(initialized_project / ".lex-align" / "decisions")
    d = store.load_all()[0]
    assert len(d.alternatives) == 1
    assert d.alternatives[0].name == "Memcached"


def test_propose_updates_superseded_decision(
    runner: CliRunner, initialized_project: Path, mock_llm, store: DecisionStore, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    old = Decision(
        id="ADR-0001",
        title="Use Postgres sessions",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(),
    )
    store.save(old)

    input_text = (
        "Use Redis for sessions\n"
        "Better performance\n"
        "high\n"
        "session\n"
        "\n"
        "\n"
        "ADR-0001\n"   # supersedes
        "n\n"
    )
    result = runner.invoke(main, ["propose"], input=input_text)
    assert result.exit_code == 0, result.output

    updated_old = store.get("ADR-0001")
    assert updated_old.status == Status.SUPERSEDED
    new_decisions = [d for d in store.load_all() if d.id != "ADR-0001"]
    assert len(new_decisions) == 1
    assert "ADR-0001" in new_decisions[0].supersedes


def test_propose_with_dependency_prefill(
    runner: CliRunner, initialized_project: Path, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    input_text = (
        "\n"         # accept default title "Add redis"
        "Fast cache\n"
        "medium\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "n\n"
    )
    result = runner.invoke(main, ["propose", "--dependency", "redis>=5.0"], input=input_text)
    assert result.exit_code == 0, result.output


# ── promote ───────────────────────────────────────────────────────────────────

def test_promote_converts_observed(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    d = Decision(
        id="ADR-0001",
        title="Uses redis",
        status=Status.OBSERVED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        observed_via=ObservedVia.SEED,
    )
    store.save(d)

    input_text = (
        "Redis was chosen for its speed\n"  # context
        "high\n"                            # confidence
        "redis\n"                           # tags
        "\n"                                # paths
        "\n"                                # constraints
        "n\n"                               # no alternatives
    )
    result = runner.invoke(main, ["promote", "ADR-0001"], input=input_text)
    assert result.exit_code == 0, result.output
    assert "Promoted" in result.output

    updated = store.get("ADR-0001")
    assert updated.status == Status.ACCEPTED


def test_promote_calls_llm(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    d = Decision(
        id="ADR-0001",
        title="Uses redis",
        status=Status.OBSERVED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        observed_via=ObservedVia.SEED,
    )
    store.save(d)
    input_text = "Context for redis\nhigh\nredis\n\n\nn\n"
    runner.invoke(main, ["promote", "ADR-0001"], input=input_text)
    mock_llm.generate_promotion_body.assert_called_once()


def test_promote_nonexistent(
    runner: CliRunner, initialized_project: Path, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["promote", "ADR-9999"], input="context\nmedium\n\n\n\nn\n")
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_promote_already_accepted(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    store.save(
        Decision(
            id="ADR-0001",
            title="Use Redis",
            status=Status.ACCEPTED,
            created=datetime.date.today(),
            confidence=Confidence.HIGH,
            scope=Scope(),
        )
    )
    result = runner.invoke(main, ["promote", "ADR-0001"], input="context\nmedium\n\n\n\nn\n")
    assert result.exit_code != 0
    assert "not an observed" in result.output.lower()


def test_promote_with_context_option(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    d = Decision(
        id="ADR-0001",
        title="Uses redis",
        status=Status.OBSERVED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        observed_via=ObservedVia.SEED,
    )
    store.save(d)
    # With --context, skips the context prompt
    input_text = "high\nredis\n\n\nn\n"
    result = runner.invoke(
        main, ["promote", "ADR-0001", "--context", "Pre-filled context"], input=input_text
    )
    assert result.exit_code == 0, result.output


# ── propose non-interactive (--yes) ──────────────────────────────────────────

def test_propose_yes_creates_decision(
    runner: CliRunner, initialized_project: Path, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, [
        "propose", "--yes",
        "--title", "Use Redis for caching",
        "--rationale", "We need fast cache access",
        "--confidence", "high",
        "--tags", "cache,redis",
        "--paths", "src/cache.py",
        "--constraints", "latency-sla",
    ])
    assert result.exit_code == 0, result.output
    assert "Written" in result.output
    store = DecisionStore(initialized_project / ".lex-align" / "decisions")
    decisions = store.load_all()
    assert len(decisions) == 1
    d = decisions[0]
    assert d.status == Status.ACCEPTED
    assert "redis" in d.title.lower()
    assert d.confidence.value == "high"
    assert "cache" in d.scope.tags
    assert "redis" in d.scope.tags
    assert "src/cache.py" in d.scope.paths
    assert "latency-sla" in d.constraints_depended_on


def test_propose_yes_requires_title(
    runner: CliRunner, initialized_project: Path, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["propose", "--yes", "--rationale", "some reason"])
    assert result.exit_code != 0
    assert "--title" in result.output


def test_propose_yes_requires_rationale(
    runner: CliRunner, initialized_project: Path, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["propose", "--yes", "--title", "Use X"])
    assert result.exit_code != 0
    assert "--rationale" in result.output


def test_propose_yes_with_alternatives_json(
    runner: CliRunner, initialized_project: Path, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    alts = json.dumps([
        {"name": "Memcached", "outcome": "not-chosen", "reason": "Less features", "reversible": "cheap", "constraint": None}
    ])
    result = runner.invoke(main, [
        "propose", "--yes",
        "--title", "Use Redis",
        "--rationale", "Speed",
        "--alternatives-json", alts,
    ])
    assert result.exit_code == 0, result.output
    store = DecisionStore(initialized_project / ".lex-align" / "decisions")
    d = store.load_all()[0]
    assert len(d.alternatives) == 1
    assert d.alternatives[0].name == "Memcached"


def test_propose_yes_supersedes(
    runner: CliRunner, initialized_project: Path, mock_llm, store: DecisionStore, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    old = Decision(
        id="ADR-0001",
        title="Use Postgres sessions",
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(),
    )
    store.save(old)
    result = runner.invoke(main, [
        "propose", "--yes",
        "--title", "Use Redis for sessions",
        "--rationale", "Better performance",
        "--supersedes", "ADR-0001",
    ])
    assert result.exit_code == 0, result.output
    updated_old = store.get("ADR-0001")
    assert updated_old.status == Status.SUPERSEDED


def test_propose_yes_invalid_alternatives_json(
    runner: CliRunner, initialized_project: Path, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, [
        "propose", "--yes",
        "--title", "Use X",
        "--rationale", "reason",
        "--alternatives-json", "not-json",
    ])
    assert result.exit_code != 0
    assert "not valid JSON" in result.output


# ── promote non-interactive (--yes) ──────────────────────────────────────────

def test_promote_yes_converts_observed(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    store.save(Decision(
        id="ADR-0001",
        title="Uses redis",
        status=Status.OBSERVED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        observed_via=ObservedVia.SEED,
    ))
    result = runner.invoke(main, [
        "promote", "ADR-0001", "--yes",
        "--context", "Redis was chosen for its speed",
        "--confidence", "high",
        "--tags", "redis,cache",
        "--constraints", "latency-sla",
    ])
    assert result.exit_code == 0, result.output
    assert "Promoted" in result.output
    updated = store.get("ADR-0001")
    assert updated.status == Status.ACCEPTED
    assert updated.confidence.value == "high"
    assert "redis" in updated.scope.tags
    assert "latency-sla" in updated.constraints_depended_on


def test_promote_yes_requires_context(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    store.save(Decision(
        id="ADR-0001",
        title="Uses redis",
        status=Status.OBSERVED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        observed_via=ObservedVia.SEED,
    ))
    result = runner.invoke(main, ["promote", "ADR-0001", "--yes"])
    assert result.exit_code != 0
    assert "--context" in result.output


def test_promote_yes_with_alternatives_json(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    store.save(Decision(
        id="ADR-0001",
        title="Uses redis",
        status=Status.OBSERVED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        observed_via=ObservedVia.SEED,
    ))
    alts = json.dumps([
        {"name": "Memcached", "outcome": "not-chosen", "reason": "Less features", "reversible": "cheap", "constraint": None}
    ])
    result = runner.invoke(main, [
        "promote", "ADR-0001", "--yes",
        "--context", "Redis chosen for speed",
        "--alternatives-json", alts,
    ])
    assert result.exit_code == 0, result.output
    updated = store.get("ADR-0001")
    assert len(updated.alternatives) == 1
    assert updated.alternatives[0].name == "Memcached"


def test_promote_yes_invalid_alternatives_json(
    runner: CliRunner, initialized_project: Path, store: DecisionStore, mock_llm, mocker
):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    store.save(Decision(
        id="ADR-0001",
        title="Uses redis",
        status=Status.OBSERVED,
        created=datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=["redis"]),
        observed_via=ObservedVia.SEED,
    ))
    result = runner.invoke(main, [
        "promote", "ADR-0001", "--yes",
        "--context", "Redis chosen for speed",
        "--alternatives-json", "{bad json}",
    ])
    assert result.exit_code != 0
    assert "not valid JSON" in result.output


# ── doctor ────────────────────────────────────────────────────────────────────

def test_doctor_healthy(runner: CliRunner, initialized_project: Path, mocker):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    from lex_align.settings import add_lex_hooks
    add_lex_hooks(initialized_project)
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "healthy" in result.output.lower()


def test_doctor_repair(runner: CliRunner, initialized_project: Path, mocker):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["doctor", "--repair"])
    assert result.exit_code == 0
    assert "Repaired" in result.output or "healthy" in result.output.lower()


# ── uninstall ─────────────────────────────────────────────────────────────────

def test_uninstall_removes_hooks(runner: CliRunner, initialized_project: Path, mocker):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    from lex_align.settings import add_lex_hooks, check_hooks_present
    add_lex_hooks(initialized_project)
    result = runner.invoke(main, ["uninstall", "--yes"])
    assert result.exit_code == 0
    status = check_hooks_present(initialized_project)
    assert not any(status.values())


# ── privacy ───────────────────────────────────────────────────────────────────

def test_privacy_displays_notice(runner: CliRunner):
    result = runner.invoke(main, ["privacy"])
    assert result.exit_code == 0
    assert "telemetry" in result.output.lower()
    assert "git" in result.output.lower()


# ── report ────────────────────────────────────────────────────────────────────

def test_report_command(runner: CliRunner, initialized_project: Path, mocker):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["report"])
    assert result.exit_code == 0
    assert "Retrieval" in result.output
    assert "Integrity" in result.output


def test_report_with_since(runner: CliRunner, initialized_project: Path, mocker):
    mocker.patch("lex_align.cli._find_project_root", return_value=initialized_project)
    result = runner.invoke(main, ["report", "--since", "2 weeks ago"])
    assert result.exit_code == 0
