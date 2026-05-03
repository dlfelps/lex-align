"""Bulk-audit a project's runtime dependencies without committing.

`lex-align-client audit` is the read-only sibling of the pre-commit hook:
walk `[project].dependencies`, evaluate each one against the server, and
print a summary. Useful when adopting `lex-align` on an existing project
or before sending a PR for review — you don't have to `git commit` just
to find out which packages are out of policy.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .api import LexAlignClient, ServerError, ServerUnreachable, Verdict
from .config import ClientConfig
from .pyproject_utils import extract_pinned_version, get_runtime_deps


@dataclass
class AuditReport:
    project: str
    deps_total: int
    verdicts: list[Verdict]

    @property
    def denied(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.verdict == "DENIED"]

    @property
    def provisional(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.verdict == "PROVISIONALLY_ALLOWED"]

    @property
    def allowed(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.verdict == "ALLOWED"]

    @property
    def transport_errors(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.transport_error]

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "deps_total": self.deps_total,
            "summary": {
                "allowed": len(self.allowed) - len(self.transport_errors),
                "provisionally_allowed": len(self.provisional),
                "denied": len(self.denied),
                "transport_errors": len(self.transport_errors),
            },
            "verdicts": [v.to_dict() for v in self.verdicts],
        }


def evaluate(
    project_root: Path,
    config: ClientConfig,
    *,
    agent_model: str | None = None,
    agent_version: str | None = None,
) -> AuditReport:
    """Evaluate every runtime dep in ``project_root/pyproject.toml``."""
    deps = get_runtime_deps(project_root / "pyproject.toml")
    verdicts: list[Verdict] = []
    if not deps:
        return AuditReport(project=config.project, deps_total=0, verdicts=[])
    with LexAlignClient(
        config, agent_model=agent_model, agent_version=agent_version
    ) as client:
        for name, spec in sorted(deps.items()):
            version = extract_pinned_version(spec)
            verdicts.append(client.check(name, version))
    return AuditReport(
        project=config.project,
        deps_total=len(deps),
        verdicts=verdicts,
    )


def format_report(report: AuditReport) -> str:
    """Render an :class:`AuditReport` as a human-readable summary."""
    lines: list[str] = [
        f"Audited {report.deps_total} runtime dep"
        f"{'s' if report.deps_total != 1 else ''} for project '{report.project}'.",
        "",
    ]
    if report.deps_total == 0:
        lines.append("No `[project].dependencies` found.")
        return "\n".join(lines)

    lines.append(
        f"  ALLOWED              : {len(report.allowed) - len(report.transport_errors)}"
    )
    lines.append(f"  PROVISIONALLY_ALLOWED: {len(report.provisional)}")
    lines.append(f"  DENIED               : {len(report.denied)}")
    if report.transport_errors:
        lines.append(f"  (server unreachable for {len(report.transport_errors)})")
    lines.append("")

    if report.denied:
        lines.append("DENIED:")
        for v in report.denied:
            lines.extend(_render_verdict(v, marker="✗"))
        lines.append("")
    if report.provisional:
        lines.append("PROVISIONALLY_ALLOWED:")
        for v in report.provisional:
            lines.extend(_render_verdict(v, marker="◎"))
        lines.append(
            "  → run `lex-align-client request-approval --package <name> "
            "--rationale \"<why>\"` to enqueue formal review."
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_verdict(v: Verdict, *, marker: str) -> Iterable[str]:
    spec = v.package + (f" {v.version}" if v.version else "")
    out = [f"  {marker} {spec} — {v.reason}"]
    if v.replacement:
        out.append(f"      use instead: {v.replacement}")
    if v.cve_ids:
        out.append(f"      CVEs: {', '.join(v.cve_ids[:5])}")
    if v.license:
        out.append(f"      license: {v.license}")
    return out


def run(
    project_root: Path,
    config: ClientConfig,
    *,
    as_json: bool,
    agent_model: str | None = None,
    agent_version: str | None = None,
) -> int:
    """CLI entry point. Returns the desired exit code (0/1/2)."""
    try:
        report = evaluate(
            project_root, config,
            agent_model=agent_model, agent_version=agent_version,
        )
    except ServerUnreachable as exc:
        print(f"[lex-align] server unreachable: {exc}", file=sys.stderr)
        return 1
    except ServerError as exc:
        print(f"[lex-align] server error: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(format_report(report), end="")
    return 2 if report.denied else 0
