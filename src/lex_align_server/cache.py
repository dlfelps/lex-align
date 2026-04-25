"""Redis-backed JSON cache.

Used by the license and CVE adapters to avoid hammering PyPI/OSV. We
intentionally swallow connection errors and degrade to "no cache" so the
server keeps serving when Redis is unreachable — the audit log still records
every decision.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)


class JsonCache:
    def __init__(self, url: str):
        self._url = url
        self._client: Optional[redis.Redis] = None

    async def _conn(self) -> Optional[redis.Redis]:
        if self._client is None:
            try:
                self._client = redis.from_url(self._url, decode_responses=True)
            except Exception:
                logger.warning("redis: could not initialize client for %s", self._url)
                self._client = None
        return self._client

    async def get(self, key: str) -> Optional[Any]:
        client = await self._conn()
        if client is None:
            return None
        try:
            raw = await client.get(key)
        except Exception as exc:
            logger.warning("redis: get(%s) failed: %s", key, exc)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        client = await self._conn()
        if client is None:
            return
        try:
            await client.set(key, json.dumps(value), ex=ttl_seconds)
        except Exception as exc:
            logger.warning("redis: set(%s) failed: %s", key, exc)

    async def ping(self) -> bool:
        client = await self._conn()
        if client is None:
            return False
        try:
            return bool(await client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
