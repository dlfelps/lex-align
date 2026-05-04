"""Application state container.

Carried on `app.state` so the per-request dependencies don't have to know how
the cache, audit log, or registry got built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import httpx

from .audit import AuditStore
from .authn import Authenticator
from .cache import JsonCache
from .config import Settings
from .proposer import Proposer
from .registry import Registry


if TYPE_CHECKING:  # pragma: no cover
    from .cve_scanner import CveScanner


@dataclass
class AppState:
    settings: Settings
    cache: JsonCache
    audit: AuditStore
    http: httpx.AsyncClient
    registry: Optional[Registry]
    authenticator: Authenticator
    proposer: Proposer
    cve_scanner: Optional["CveScanner"] = None
