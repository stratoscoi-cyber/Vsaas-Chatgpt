"""Tests for the in-process event bus that backs SSE."""

from queue import Empty

from agri_platform.common.eventbus import EventBus


def test_publish_delivers_to_subscriber():
    bus = EventBus()
    q = bus.subscribe("c1")
    assert bus.publish("c1", {"x": 1}) == 1
    assert q.get_nowait() == {"x": 1}


def test_multiple_subscribers_each_receive():
    bus = EventBus()
    a, b = bus.subscribe("c1"), bus.subscribe("c1")
    bus.publish("c1", "hi")
    assert a.get_nowait() == "hi" and b.get_nowait() == "hi"


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    q = bus.subscribe("c1")
    bus.unsubscribe("c1", q)
    assert bus.publish("c1", "x") == 0
    assert bus.subscriber_count("c1") == 0


def test_other_channel_isolated():
    bus = EventBus()
    q = bus.subscribe("c1")
    bus.publish("c2", "x")
    try:
        q.get_nowait()
        assert False, "should not receive other channel's message"
    except Empty:
        pass


def test_full_queue_drops_without_blocking():
    bus = EventBus(max_queue=1)
    bus.subscribe("c1")
    assert bus.publish("c1", 1) == 1
    assert bus.publish("c1", 2) == 0  # queue full -> dropped, no block
