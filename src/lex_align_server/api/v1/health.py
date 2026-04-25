"""GET /api/v1/health — basic liveness/readiness."""

from __future__ import annotations

from fastapi import APIRouter, Request


router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict:
    state = request.app.state.lex
    return {
        "redis": "ok" if await state.cache.ping() else "down",
        "db": "ok" if await state.audit.health() else "down",
        "registry_loaded": state.registry is not None,
        "registry_version": state.registry.version if state.registry is not None else None,
        "auth_enabled": state.settings.auth_enabled,
    }
