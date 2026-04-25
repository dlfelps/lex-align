"""Dashboard skeleton (Phase 4).

Placeholder pages that fetch the corresponding /api/v1/reports/* endpoint
client-side and render the JSON. The full UI is intentionally out of scope
for this milestone; these pages exist so an operator can verify the data
path end-to-end.

Auth gate: dashboards are only mounted when AUTH_ENABLED=true.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


router = APIRouter()
_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


def _require_auth_enabled(request: Request) -> None:
    if not request.app.state.lex.settings.auth_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dashboards are only enabled in organization mode.",
        )


@router.get("/dashboard/security", response_class=HTMLResponse)
async def security_dashboard(request: Request) -> HTMLResponse:
    _require_auth_enabled(request)
    return templates.TemplateResponse(
        request, "report.html",
        {"title": "Security report", "endpoint": "/api/v1/reports/security"},
    )


@router.get("/dashboard/legal", response_class=HTMLResponse)
async def legal_dashboard(request: Request) -> HTMLResponse:
    _require_auth_enabled(request)
    return templates.TemplateResponse(
        request, "report.html",
        {"title": "Legal report", "endpoint": "/api/v1/reports/legal"},
    )


@router.get("/dashboard/registry", response_class=HTMLResponse)
async def registry_dashboard(request: Request) -> HTMLResponse:
    _require_auth_enabled(request)
    reg = request.app.state.lex.registry
    return templates.TemplateResponse(
        request, "registry.html",
        {
            "title": "Registry workshop",
            "registry": reg,
        },
    )
