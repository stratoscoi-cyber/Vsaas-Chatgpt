"""Tiny in-process publish/subscribe bus for server-sent events.

Each subscriber gets its own bounded queue; ``publish`` fans a message out to
every current subscriber of a channel. This is intentionally process-local: it
backs SSE for a single (threaded) process or one gunicorn worker. To fan events
across multiple workers/replicas, back the same interface with Redis pub/sub.
"""

from __future__ import annotations

import threading
from queue import Empty, Full, Queue
from typing import Any, Dict, List, Set


class EventBus:
    def __init__(self, max_queue: int = 100):
        self._subscribers: Dict[str, Set[Queue]] = {}
        self._lock = threading.Lock()
        self._max_queue = max_queue

    def subscribe(self, channel: str) -> Queue:
        q: Queue = Queue(maxsize=self._max_queue)
        with self._lock:
            self._subscribers.setdefault(channel, set()).add(q)
        return q

    def unsubscribe(self, channel: str, q: Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(channel)
            if subs:
                subs.discard(q)
                if not subs:
                    self._subscribers.pop(channel, None)

    def publish(self, channel: str, data: Any) -> int:
        with self._lock:
            subs = list(self._subscribers.get(channel, ()))
        delivered = 0
        for q in subs:
            try:
                q.put_nowait(data)
                delivered += 1
            except Full:
                pass  # slow consumer; drop rather than block producers
        return delivered

    def subscriber_count(self, channel: str) -> int:
        with self._lock:
            return len(self._subscribers.get(channel, ()))


class RedisEventBus:
    """Cross-process event bus backed by Redis pub/sub (same interface as EventBus).

    ``publish`` goes to Redis; each ``subscribe`` starts a listener thread that
    feeds a local queue, so SSE works across multiple workers/replicas.
    """

    def __init__(self, redis_url: str = None, client=None, prefix: str = "evbus:", max_queue: int = 100):
        if client is not None:
            self._redis = client
        else:
            import redis  # optional dependency

            self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
            self._redis.ping()
        self._prefix = prefix
        self._max_queue = max_queue
        self._threads: Dict[Queue, threading.Thread] = {}
        self._stops: Dict[Queue, threading.Event] = {}

    def subscribe(self, channel: str) -> Queue:
        import json as _json

        q: Queue = Queue(maxsize=self._max_queue)
        stop = threading.Event()
        pubsub = self._redis.pubsub()
        pubsub.subscribe(self._prefix + channel)

        def _listen():
            try:
                for msg in pubsub.listen():
                    if stop.is_set():
                        break
                    if msg.get("type") != "message":
                        continue
                    try:
                        q.put_nowait(_json.loads(msg["data"]))
                    except Full:
                        pass
            finally:
                pubsub.close()

        t = threading.Thread(target=_listen, daemon=True)
        t.start()
        self._threads[q] = t
        self._stops[q] = stop
        return q

    def unsubscribe(self, channel: str, q: Queue) -> None:
        stop = self._stops.pop(q, None)
        if stop:
            stop.set()
        self._threads.pop(q, None)

    def publish(self, channel: str, data) -> int:
        import json as _json

        return int(self._redis.publish(self._prefix + channel, _json.dumps(data)))

    def subscriber_count(self, channel: str) -> int:  # best-effort
        try:
            return int(self._redis.pubsub_numsub(self._prefix + channel)[0][1])
        except Exception:  # pragma: no cover
            return 0


def make_event_bus(redis_url: Optional[str] = None) -> "EventBus":
    """Redis-backed bus when a URL is given and reachable, else in-process."""
    if redis_url:
        try:
            return RedisEventBus(redis_url)
        except Exception:  # pragma: no cover - depends on infra
            pass
    return EventBus()


from typing import Optional  # noqa: E402  (kept local to avoid reordering above)

__all__ = ["EventBus", "RedisEventBus", "make_event_bus", "Empty"]
