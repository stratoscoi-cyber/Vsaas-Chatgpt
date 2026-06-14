"""Pluggable cache abstraction.

A process-local TTL cache is always available; when ``REDIS_URL`` is set and the
``redis`` package is installed, a shared Redis-backed cache is used instead so
multiple service replicas share state. Values are JSON-serialisable.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional, Protocol


class Cache(Protocol):
    def get(self, key: str) -> Optional[Any]: ...
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None: ...
    def delete(self, key: str) -> None: ...


class InMemoryCache:
    """Thread-safe TTL cache backed by a dict."""

    def __init__(self, default_ttl: int = 300):
        self.default_ttl = default_ttl
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at < time.time():
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        with self._lock:
            self._data[key] = (time.time() + (ttl or self.default_ttl), value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


class RedisCache:
    """Redis-backed cache (JSON values)."""

    def __init__(self, client, default_ttl: int = 300):
        self._client = client
        self.default_ttl = default_ttl

    def get(self, key: str) -> Optional[Any]:
        raw = self._client.get(key)
        return json.loads(raw) if raw is not None else None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        self._client.set(key, json.dumps(value), ex=ttl or self.default_ttl)

    def delete(self, key: str) -> None:
        self._client.delete(key)


def make_cache(redis_url: Optional[str] = None, default_ttl: int = 300) -> Cache:
    """Build the best available cache; falls back to in-memory."""
    if redis_url:
        try:
            import redis  # imported lazily so it's optional

            client = redis.Redis.from_url(redis_url, decode_responses=True)
            client.ping()
            return RedisCache(client, default_ttl)
        except Exception:  # pragma: no cover - depends on infra
            # Any connection/import problem degrades gracefully to local cache.
            pass
    return InMemoryCache(default_ttl)
