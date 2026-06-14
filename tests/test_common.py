"""Tests for shared infrastructure: cache, rate limiter, pagination, config."""

import time

from agri_platform.common.cache import InMemoryCache
from agri_platform.common.config import Settings
from agri_platform.common.pagination import paginate
from agri_platform.common.ratelimit import RateLimiter


def test_inmemory_cache_set_get_delete():
    c = InMemoryCache(default_ttl=100)
    assert c.get("k") is None
    c.set("k", {"v": 1})
    assert c.get("k") == {"v": 1}
    c.delete("k")
    assert c.get("k") is None


def test_inmemory_cache_expiry():
    c = InMemoryCache()
    c.set("k", 1, ttl=1)
    assert c.get("k") == 1
    # force expiry without sleeping a full second
    c._data["k"] = (time.time() - 1, 1)
    assert c.get("k") is None


def test_rate_limiter_allows_then_blocks():
    rl = RateLimiter(limit_per_minute=2)
    assert rl.allow("a")[0] is True
    assert rl.allow("a")[0] is True
    allowed, remaining = rl.allow("a")
    assert allowed is False and remaining == 0
    # a different client has its own bucket
    assert rl.allow("b")[0] is True


def test_rate_limiter_disabled_when_zero():
    rl = RateLimiter(limit_per_minute=0)
    for _ in range(100):
        assert rl.allow("x")[0] is True


class _FakeQuery:
    def __init__(self, items):
        self.items = items
        self._limit = len(items)
        self._offset = 0

    def order_by(self, *a):
        return self

    def count(self):
        return len(self.items)

    def limit(self, n):
        self._limit = n
        return self

    def offset(self, n):
        self._offset = n
        return self

    def all(self):
        return self.items[self._offset:self._offset + self._limit]


def test_paginate_meta_and_slice():
    items, meta = paginate(_FakeQuery(list(range(25))), page=2, page_size=10)
    assert items == list(range(10, 20))
    assert meta == {
        "page": 2, "page_size": 10, "total": 25, "pages": 3,
        "has_next": True, "has_prev": True,
    }


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", "a, b ,c")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "120")
    s = Settings.from_env("wfaas", default_port=5001)
    assert s.database_url == "sqlite:///x.db"
    assert s.auth_enabled is True
    assert s.api_keys == frozenset({"a", "b", "c"})
    assert s.rate_limit_per_minute == 120
