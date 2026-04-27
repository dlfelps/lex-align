"""Server configuration.

All settings are sourced from environment variables. Defaults bias towards the
single-user/local-evaluation experience: no auth, bind to localhost, point at a
co-located Redis.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    # Auth. ``auth_enabled`` is the master switch — when false, every request
    # is anonymous regardless of ``auth_backend``. When true, ``auth_backend``
    # selects how the requester is identified. See docs/org-mode-auth.md.
    auth_enabled: bool = False
    auth_backend: str = "header"  # header | webhook | apikey | anonymous | module:path:Class

    # Header backend (recommended): trust these from an upstream auth gateway.
    auth_user_header: str = "X-Forwarded-User"
    auth_email_header: str = "X-Forwarded-Email"
    auth_groups_header: str = "X-Forwarded-Groups"
    auth_groups_separator: str = ","
    # Comma-separated CIDRs of proxies whose forwarded-* headers we honour.
    # Requests from outside these CIDRs are rejected even if the headers are
    # set, since otherwise a direct caller could spoof identity.
    auth_trusted_proxies: str = "127.0.0.1/32,::1/128"

    # Webhook backend: POST {"token": "..."} to this URL; expect identity JSON.
    auth_verify_url: Optional[str] = None
    auth_verify_timeout: float = 3.0

    # Network bind. Single-user mode binds 127.0.0.1; org-mode operators are
    # expected to override to 0.0.0.0 (and turn on auth_enabled).
    bind_host: str = "127.0.0.1"
    bind_port: int = 8765

    # External services.
    redis_url: str = "redis://localhost:6379/0"
    osv_api_url: str = "https://api.osv.dev/v1/query"
    pypi_api_url: str = "https://pypi.org/pypi"

    # Storage.
    database_path: Path = Path("/var/lib/lexalign/lexalign.sqlite")
    registry_path: Optional[Path] = None

    # Cache TTLs (seconds).
    license_cache_ttl: int = 60 * 60 * 24 * 7   # 7 days
    cve_cache_ttl: int = 60 * 60 * 6            # 6 hours
    pypi_latest_version_ttl: int = 60 * 60      # 1 hour

    # HTTP timeouts (seconds).
    outbound_timeout: float = 5.0


def get_settings() -> Settings:
    """Build a fresh Settings object. Avoids module-level caching so tests can
    override via env vars on a per-test basis.
    """
    return Settings()
