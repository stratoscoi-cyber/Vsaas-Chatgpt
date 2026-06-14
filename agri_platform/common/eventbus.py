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


__all__ = ["EventBus", "Empty"]
