"""Dashboard pages.

Four pages render server-side and fetch their data from the JSON API:

- ``/dashboard/security`` is a vulnerability-posture view: severity buckets,
  packages with the worst CVE history, and the "hot" cell — registry-allowed
  packages that have started attracting CVE denials.
- ``/dashboard/legal`` is a license-compliance view: license breakdown by
  verdict, unknown-license policy performance, projects pulling the most
  non-compliant packages.
- ``/dashboard/agents`` shows a generic agent-activity report.
- ``/dashboard/registry`` is an interactive workshop: it loads the live
  registry, lets the operator triage pending approval requests, and
  exports the result as YAML. Classifying a pending request also
  updates the in-memory registry so live ``/evaluate`` calls see the
  rule immediately, but persistence still requires exporting the YAML
  and rebuilding the server image.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


router = APIRouter()
_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


@router.get("/dashboard/security", response_class=HTMLResponse)
async def security_dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "security.html",
        {"title": "Security posture", "endpoint": "/api/v1/reports/security"},
    )


@router.get("/dashboard/legal", response_class=HTMLResponse)
async def legal_dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "legal.html",
        {"title": "Legal compliance", "endpoint": "/api/v1/reports/legal"},
    )


@router.get("/dashboard/agents", response_class=HTMLResponse)
async def agents_dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "report.html",
        {"title": "Agent activity", "endpoint": "/api/v1/reports/agents"},
    )


@router.get("/dashboard/registry", response_class=HTMLResponse)
async def registry_dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "registry.html",
        {"title": "Registry workshop"},
    )
