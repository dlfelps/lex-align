"""`lex-align-client status` — one-screen project + server snapshot.

Pulls together the things a developer normally checks across multiple
URLs (server health, pending approval queue, recent CVE-driven
denials) plus what's only visible on the client (hook install state,
local dep count) so you can see the whole picture without leaving
the terminal.

The collector tolerates a missing or unreachable server: each section
gets a status flag and the rest of the report still renders. This is
intentional — if the server's down you still want to know which hooks
are wired and how many runtime deps the project carries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .api import LexAlignClient, ServerError, ServerUnreachable
from .config import ClientConfig
from .pyproject_utils import get_runtime_deps
from .settings import claude_hooks_status, precommit_installed


@dataclass
class StatusReport:
    project: str
    server_url: str
    mode: str
    server_reachable: bool
    server_detail: dict = field(default_factory=dict)
    server_error: Optional[str] = None
    deps_total: int = 0
    pending_approvals: int = 0
    pending_packages: list[str] = field(default_factory=list)
    cve_severity: dict = field(default_factory=dict)
    hot_registry_packages: list[dict] = field(default_factory=list)
    claude_hooks: dict = field(default_factory=dict)
    precommit_installed: bool = False
    auto_request_approval: bool = True

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "server_url": self.server_url,
            "mode": self.mode,
            "auto_request_approval": self.auto_request_approval,
            "server": {
                "reachable": self.server_reachable,
                "detail": self.server_detail,
                "error": self.server_error,
            },
            "deps_total": self.deps_total,
            "pending_approvals": {
                "count": self.pending_approvals,
                "packages": self.pending_packages,
            },
            "security": {
                "cve_severity": self.cve_severity,
                "hot_registry_packages": self.hot_registry_packages,
            },
            "hooks": {
                "claude": self.claude_hooks,
                "precommit_installed": self.precommit_installed,
            },
        }


def collect(project_root: Path, config: ClientConfig) -> StatusReport:
    deps = get_runtime_deps(project_root / "pyproject.toml")
    hooks = claude_hooks_status(project_root)

    report = StatusReport(
        project=config.project,
        server_url=config.server_url,
        mode=config.mode,
        server_reachable=False,
        deps_total=len(deps),
        claude_hooks=hooks,
        precommit_installed=precommit_installed(project_root),
        auto_request_approval=config.auto_request_approval,
    )

    try:
        with LexAlignClient(config) as client:
            health = client.health()
            report.server_reachable = True
            report.server_detail = health
            try:
                pending = client.pending_approvals()
                report.pending_approvals = len(pending)
                report.pending_packages = sorted(
                    {row.get("package") for row in pending if row.get("package")}
                )
            except (ServerError, ServerUnreachable) as exc:
                report.server_error = f"approvals: {exc}"
            try:
                sec = client.security_report()
                report.cve_severity = dict(sec.get("severity_distribution") or {})
                report.hot_registry_packages = list(
                    sec.get("hot_registry_packages") or []
                )
            except (ServerError, ServerUnreachable) as exc:
                # Don't override an earlier approvals error — keep the
                # first surface so the user sees the most useful one.
                if report.server_error is None:
                    report.server_error = f"security: {exc}"
    except (ServerError, ServerUnreachable, Exception) as exc:
        report.server_error = str(exc)
    return report


def format_report(report: StatusReport) -> str:
    lines: list[str] = [
        f"# lex-align status — project: {report.project}",
        f"  server_url           : {report.server_url}",
        f"  mode                 : {report.mode}",
        f"  auto_request_approval: {'on' if report.auto_request_approval else 'off'}",
        "",
    ]

    if report.server_reachable:
        d = report.server_detail
        lines.append(
            f"Server: reachable — registry={'loaded' if d.get('registry_loaded') else 'absent'} "
            f"redis={d.get('redis')} db={d.get('db')}"
        )
    else:
        lines.append(f"Server: UNREACHABLE ({report.server_error or 'no detail'})")
    lines.append("")

    lines.append(f"Runtime dependencies in pyproject.toml: {report.deps_total}")
    lines.append("")

    if report.pending_approvals:
        lines.append(f"Pending approvals for this project: {report.pending_approvals}")
        for pkg in report.pending_packages[:10]:
            lines.append(f"  · {pkg}")
        remaining = len(report.pending_packages) - 10
        if remaining > 0:
            lines.append(f"  · ... and {remaining} more")
    else:
        lines.append("Pending approvals for this project: 0")
    lines.append("")

    sev = report.cve_severity or {}
    crit = sev.get("critical", 0)
    high = sev.get("high", 0)
    if crit or high:
        lines.append(
            f"Recent CVE-driven denials: critical={crit} high={high} "
            f"medium={sev.get('medium', 0)} low={sev.get('low', 0)}"
        )
    else:
        lines.append("Recent CVE-driven denials: none.")
    if report.hot_registry_packages:
        lines.append("Approved packages with new CVE pressure:")
        for row in report.hot_registry_packages[:5]:
            pkg = row.get("package", "?")
            cvss = row.get("max_cvss")
            cvss_label = f"CVSS {cvss}" if cvss is not None else "CVSS ?"
            lines.append(f"  ! {pkg} — {cvss_label}")
    lines.append("")

    lines.append("Hooks:")
    for event, installed in report.claude_hooks.items():
        lines.append(f"  Claude {event:<14} : {'installed' if installed else 'missing'}")
    lines.append(
        f"  git pre-commit       : "
        f"{'installed' if report.precommit_installed else 'missing'}"
    )
    return "\n".join(lines).rstrip() + "\n"
