"""Dashboard pages.

Three pages render server-side and fetch their data from the JSON API:
- /dashboard/security and /dashboard/legal show read-only reports.
- /dashboard/registry is an interactive workshop: it loads the live
  registry, lets the operator add/edit/delete entries in-browser, and
  exports the result as YAML. Edits never round-trip to the server, so
  they cannot affect live evaluation.
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
        request, "report.html",
        {"title": "Security report", "endpoint": "/api/v1/reports/security"},
    )


@router.get("/dashboard/legal", response_class=HTMLResponse)
async def legal_dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "report.html",
        {"title": "Legal report", "endpoint": "/api/v1/reports/legal"},
    )


@router.get("/dashboard/registry", response_class=HTMLResponse)
async def registry_dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "registry.html",
        {"title": "Registry workshop"},
    )
