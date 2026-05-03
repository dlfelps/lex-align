"""``lex-align-server check-config`` — pre-flight checks for a single-team
local-file deployment.

Each check is independent: it returns a :class:`CheckResult` so the CLI
can render it as a coloured row and exit non-zero when any of them
fails. The checks deliberately stop at what a one-person /
single-team install needs:

* ``REGISTRY_PATH`` is set, exists or can be created, and round-trips
  through the registry validator.
* The audit SQLite directory is writable.
* The cache backend (Redis if configured, in-memory otherwise) is
  reachable.
* The proposer auto-detects to ``local_file`` (the recommended single-
  team mode) — anything else surfaces as a warning so operators don't
  silently end up on a heavier backend.
* Authentication is configured deliberately, not by accident.

The validator never connects to GitHub. The PR-based proposer is the
escape hatch for large orgs; if you're running ``check-config`` you're
almost certainly on the local-file path and the GitHub probe would just
add noise.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .cache import JsonCache
from .config import Settings
from .registry_schema import ValidationError, validate_registry


logger = logging.getLogger(__name__)


# Status sentinels — kept as plain strings so CLI rendering is trivial.
OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class CheckResult:
    status: str           # OK | WARN | FAIL
    label: str            # short human label, e.g. "REGISTRY_PATH"
    detail: str           # one-line explanation

    @property
    def is_failure(self) -> bool:
        return self.status == FAIL


def _ok(label: str, detail: str) -> CheckResult:
    return CheckResult(OK, label, detail)


def _warn(label: str, detail: str) -> CheckResult:
    return CheckResult(WARN, label, detail)


def _fail(label: str, detail: str) -> CheckResult:
    return CheckResult(FAIL, label, detail)


# ── individual checks ────────────────────────────────────────────────────


def check_registry_path(settings: Settings) -> CheckResult:
    """REGISTRY_PATH must be set to a YAML file path the server can use."""
    path = settings.registry_path
    if path is None:
        return _fail(
            "REGISTRY_PATH",
            "unset — the server has nowhere to load or write the registry. "
            "Export REGISTRY_PATH=/path/to/registry.yml.",
        )
    if path.exists():
        if not path.is_file():
            return _fail(
                "REGISTRY_PATH",
                f"{path} exists but is not a regular file.",
            )
        return _ok("REGISTRY_PATH", f"{path} exists ({path.stat().st_size} bytes).")
    parent = path.parent
    if not parent.exists():
        return _fail(
            "REGISTRY_PATH",
            f"{path} does not exist and parent {parent} is missing — "
            "create the directory before starting the server.",
        )
    if not os.access(parent, os.W_OK):
        return _fail(
            "REGISTRY_PATH",
            f"{path} does not exist and parent {parent} is not writable; "
            "the server cannot create the registry on first proposal.",
        )
    return _warn(
        "REGISTRY_PATH",
        f"{path} does not exist yet; will be created on first proposal.",
    )


def check_registry_yaml(settings: Settings) -> CheckResult:
    """The registry YAML, if present, must round-trip through the validator."""
    path = settings.registry_path
    if path is None or not path.exists():
        # Either unset (caught above) or empty-on-first-boot (caught above).
        return _ok("registry YAML", "no file to validate yet.")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return _fail("registry YAML", f"{path} did not parse as YAML: {exc}")
    if not isinstance(doc, dict):
        return _fail(
            "registry YAML",
            f"{path} top level is {type(doc).__name__}; expected a mapping.",
        )
    try:
        compiled = validate_registry(doc)
    except ValidationError as exc:
        return _fail("registry YAML", f"{path} failed validation: {exc}")
    n = len(compiled.get("packages") or {})
    return _ok("registry YAML", f"{path} validates ({n} package rule{'s' if n != 1 else ''}).")


def check_audit_path(settings: Settings) -> CheckResult:
    """The audit SQLite directory must be writable."""
    db_path = settings.database_path
    parent = db_path.parent
    if db_path.exists():
        if not os.access(db_path, os.W_OK):
            return _fail(
                "audit DB",
                f"{db_path} exists but is not writable by uid {os.getuid()}.",
            )
        return _ok("audit DB", f"{db_path} exists and is writable.")
    if not parent.exists():
        return _fail(
            "audit DB",
            f"parent {parent} does not exist; create it before starting "
            "the server (audit log is required, not optional).",
        )
    if not os.access(parent, os.W_OK):
        return _fail(
            "audit DB",
            f"parent {parent} is not writable by uid {os.getuid()}.",
        )
    return _warn(
        "audit DB",
        f"{db_path} does not exist yet; will be created on first request.",
    )


async def check_cache(settings: Settings) -> CheckResult:
    """Ping the configured cache backend.

    Cache failure is **not** fatal — the server degrades to "no cache"
    and keeps serving — so this is a warning, never a hard failure.
    """
    cache = JsonCache(settings.redis_url)
    try:
        ok = await cache.ping()
    finally:
        await cache.close()
    if ok:
        return _ok("cache (redis)", f"reachable at {settings.redis_url}.")
    return _warn(
        "cache (redis)",
        f"unreachable at {settings.redis_url}; server will run without cache "
        "(license/CVE lookups hit upstream every time).",
    )


def check_proposer(settings: Settings) -> CheckResult:
    """Recommend ``local_file`` for the single-team path.

    Anything other than ``local_file`` / ``log_only`` is surfaced as a
    warning so the operator notices if auto-detection picked something
    heavier than they expected.
    """
    explicit = (settings.registry_proposer or "").strip()
    if explicit:
        backend = explicit
        source = "explicit (REGISTRY_PROPOSER)"
    else:
        # We replicate the loader's own decision tree here without booting
        # the loader itself, so that we don't construct a real proposer
        # (which would, e.g., require git for the local_git path).
        backend = _predict_autodetect(settings)
        source = "auto-detected"

    if backend == "local_file":
        return _ok("proposer", f"{backend} ({source}) — recommended for single-team installs.")
    if backend == "log_only":
        return _warn(
            "proposer",
            "log_only — proposals are logged but not written. "
            "Set REGISTRY_PATH to enable local_file (recommended).",
        )
    if backend == "local_git":
        return _ok(
            "proposer",
            f"{backend} ({source}) — proposals are committed to a local git tree.",
        )
    if backend == "github":
        return _warn(
            "proposer",
            "github — PR-based flow. This is the multi-team / org-wide path; "
            "make sure REGISTRY_REPO_TOKEN is provisioned and the webhook is wired. "
            "Single-team installs usually want local_file instead.",
        )
    return _warn("proposer", f"{backend} ({source}) — custom backend.")


def check_auth(settings: Settings) -> CheckResult:
    """Surface accidental anonymous-mode deployments.

    Anonymous is a fine *default* (it makes evaluation friction-free),
    but a server bound to anything other than localhost without auth is
    almost always a misconfiguration.
    """
    if settings.auth_enabled:
        return _ok("auth", f"enabled (backend={settings.auth_backend}).")
    if settings.bind_host in ("127.0.0.1", "localhost", "::1"):
        return _ok(
            "auth",
            f"disabled (anonymous), bound to {settings.bind_host} — "
            "fine for single-user / local evaluation.",
        )
    return _warn(
        "auth",
        f"AUTH_ENABLED=false but BIND_HOST={settings.bind_host} — "
        "the server accepts anonymous requests on a non-loopback interface. "
        "Set AUTH_ENABLED=true and choose an AUTH_BACKEND.",
    )


# ── orchestrator ────────────────────────────────────────────────────────


async def run_checks(settings: Settings) -> list[CheckResult]:
    """Run every check in the order the CLI prints them."""
    results: list[CheckResult] = [
        check_registry_path(settings),
        check_registry_yaml(settings),
        check_audit_path(settings),
        await check_cache(settings),
        check_proposer(settings),
        check_auth(settings),
    ]
    return results


def run_checks_sync(settings: Settings) -> list[CheckResult]:
    """Sync wrapper for the CLI entry point."""
    return asyncio.run(run_checks(settings))


# ── helpers ─────────────────────────────────────────────────────────────


def _predict_autodetect(settings: Settings) -> str:
    """Mirror :func:`proposer.loader._autodetect` *without* the GitHub
    auto-escalation that the loader has now dropped.

    Inlined deliberately: importing the loader would pull in ``git`` and
    other side-effecting dependencies. ``check-config`` is meant to run
    even on a freshly-unboxed laptop without git installed.
    """
    if settings.registry_repo_url:
        return "github"
    path = settings.registry_path
    if path is None:
        return "log_only"
    candidate = path if path.exists() else path.parent
    if not candidate.exists():
        return "log_only"
    # Crude is-this-a-git-tree probe. The loader uses ``git rev-parse``
    # which we can avoid by looking for a ``.git`` directory walking up.
    if _has_git_marker(candidate):
        return "local_git"
    if os.access(candidate if candidate.is_dir() else candidate.parent, os.W_OK):
        return "local_file"
    return "log_only"


def _has_git_marker(start: Path) -> bool:
    cur: Optional[Path] = start.resolve()
    while cur is not None:
        if (cur / ".git").exists():
            return True
        if cur.parent == cur:
            return False
        cur = cur.parent
    return False
