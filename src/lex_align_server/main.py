"""FastAPI application factory.

Wires up the routers, builds the per-app state (cache, audit store, HTTP
client, registry), and exposes a `lifespan` so resources are torn down
cleanly.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI

from .api.v1 import approval_requests as approval_router
from .api.v1 import evaluate as evaluate_router
from .api.v1 import health as health_router
from .api.v1 import registry as registry_router
from .api.v1 import reports as reports_router
from .audit import AuditStore
from .authn import load_authenticator
from .cache import JsonCache
from .config import Settings, get_settings
from .dashboards import router as dashboards_router
from .proposer import load_proposer
from .registry import load_registry
from .reloader import RegistryPoller
from .state import AppState


logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cache = JsonCache(settings.redis_url)
        audit = AuditStore(settings.database_path)
        await audit.init()
        http_client = httpx.AsyncClient(timeout=settings.outbound_timeout)
        registry = load_registry(settings.registry_path)
        if registry is not None:
            logger.info(
                "registry loaded: version=%s packages=%d",
                registry.version, len(registry.packages),
            )
        else:
            logger.warning("no registry loaded (REGISTRY_PATH unset or missing)")
        authenticator = load_authenticator(settings, http_client)
        logger.info(
            "auth: enabled=%s backend=%s",
            settings.auth_enabled, type(authenticator).__name__,
        )
        proposer = load_proposer(settings, http_client)
        logger.info("proposer: backend=%s", type(proposer).__name__)
        app.state.lex = AppState(
            settings=settings,
            cache=cache,
            audit=audit,
            http=http_client,
            registry=registry,
            authenticator=authenticator,
            proposer=proposer,
        )
        poller = RegistryPoller(app.state.lex)
        poller.start()
        try:
            yield
        finally:
            await poller.stop()
            await proposer.close()
            await http_client.aclose()
            await cache.close()

    app = FastAPI(
        title="lex-align server",
        version="2.2.0",
        description="Enterprise governance platform for AI-generated code.",
        lifespan=lifespan,
    )
    app.include_router(evaluate_router.router, prefix="/api/v1")
    app.include_router(approval_router.router, prefix="/api/v1")
    app.include_router(reports_router.router, prefix="/api/v1")
    app.include_router(registry_router.router, prefix="/api/v1")
    app.include_router(health_router.router, prefix="/api/v1")
    app.include_router(dashboards_router.router)
    return app


app = create_app()
