"""Application state container.

Carried on `app.state` so the per-request dependencies don't have to know how
the cache, audit log, or registry got built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

from .audit import AuditStore
from .authn import Authenticator
from .cache import JsonCache
from .config import Settings
from .registry import Registry


@dataclass
class AppState:
    settings: Settings
    cache: JsonCache
    audit: AuditStore
    http: httpx.AsyncClient
    registry: Optional[Registry]
    authenticator: Authenticator
