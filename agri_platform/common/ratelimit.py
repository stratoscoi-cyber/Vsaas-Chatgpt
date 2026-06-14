"""Fixed-window rate limiting."""

from __future__ import annotations

import threading
import time
from typing import Callable

from flask import Flask, g, jsonify, request


class RateLimiter:
    """In-process fixed-window limiter: ``limit`` requests per ``window`` seconds."""

    def __init__(self, limit_per_minute: int, window_seconds: int = 60):
        self.limit = limit_per_minute
        self.window = window_seconds
        self._hits: dict[str, list] = {}  # key -> [window_start, count]
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        """Return ``(allowed, remaining)`` for a client key."""
        if self.limit <= 0:
            return True, -1
        now = time.time()
        with self._lock:
            window_start, count = self._hits.get(key, [now, 0])
            if now - window_start >= self.window:
                window_start, count = now, 0
            count += 1
            self._hits[key] = [window_start, count]
            remaining = max(0, self.limit - count)
            return count <= self.limit, remaining


def install_rate_limiting(
    app: Flask,
    limiter: RateLimiter,
    key_func: Callable[[], str] | None = None,
    exempt_paths: tuple[str, ...] = ("/health",),
) -> None:
    if limiter.limit <= 0:
        return

    def default_key() -> str:
        return getattr(g, "api_key", None) or (request.remote_addr or "anonymous")

    resolve = key_func or default_key

    @app.before_request
    def _enforce():
        if request.path in exempt_paths:
            return None
        allowed, remaining = limiter.allow(resolve())
        g.rate_remaining = remaining
        if not allowed:
            resp = jsonify({"error": {"code": "rate_limited", "message": "rate limit exceeded"}})
            resp.status_code = 429
            resp.headers["Retry-After"] = str(limiter.window)
            return resp
        return None

    @app.after_request
    def _headers(response):
        if hasattr(g, "rate_remaining") and g.rate_remaining >= 0:
            response.headers["X-RateLimit-Limit"] = str(limiter.limit)
            response.headers["X-RateLimit-Remaining"] = str(g.rate_remaining)
        return response
