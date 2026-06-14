"""Tests for KYC verification, tenant isolation and the Redis-backed event bus."""

import threading
import time
from queue import Empty, Queue

import pytest

from agri_platform.common import db as db_helpers
from agri_platform.common.config import Settings
from agri_platform.common.eventbus import RedisEventBus, make_event_bus
from agri_platform.marketplace import kyc
from agri_platform.marketplace.app import create_app
from agri_platform.marketplace.models import Base, Load


# --- KYC engine ----------------------------------------------------------------

def test_manual_provider_never_auto_verifies():
    r = kyc.ManualVerificationProvider().verify_document({"doc_type": "drivers_license"})
    assert r.verified is False and r.status == "manual_review_required"


class _StubKyc:
    name = "stub"

    def __init__(self, verified=True):
        self._verified = verified

    def verify_document(self, doc):
        return kyc.VerificationResult(self._verified, "verified" if self._verified else "rejected",
                                      {"checked": doc.get("doc_type")})

    def screen(self, identity):
        hits = [] if identity.get("name") != "BAD ACTOR" else [{"list": "sanctions"}]
        return kyc.ScreeningResult(len(hits) == 0, "screened", hits)


def _client(**over):
    engine, factory = db_helpers.make_session_factory("sqlite:///:memory:")
    db_helpers.init_models(engine, Base)
    s = Settings(service="lgaas", database_url="sqlite:///:memory:", log_json=False,
                 admin_api_keys=frozenset({"k"}), **over)
    app = create_app(settings=s, session_factory=factory)
    app.testing = True
    return app, factory


def test_document_manual_provider_leaves_unverified():
    app, _ = _client()
    c = app.test_client()
    body = c.post("/api/compliance/documents", json={"entity_type": "driver", "entity_id": "d1",
                  "doc_type": "drivers_license", "reference": "DL1"}).get_json()
    # manual provider does not flip verified; submitted default False stays False
    assert body["verified"] is False
    assert body["kyc"]["status"] == "manual_review_required"


def test_document_provider_auto_verifies():
    app, _ = _client()
    app.config["KYC_PROVIDER"] = _StubKyc(verified=True)
    c = app.test_client()
    body = c.post("/api/compliance/documents", json={"entity_type": "driver", "entity_id": "d1",
                  "doc_type": "drivers_license", "reference": "DL1"}).get_json()
    assert body["verified"] is True and body["kyc"]["status"] == "verified"


def test_screening_endpoint_flags_hit():
    app, _ = _client()
    app.config["KYC_PROVIDER"] = _StubKyc()
    c = app.test_client()
    clean = c.post("/api/compliance/screen", json={"name": "Ada Lovelace"}).get_json()
    assert clean["clear"] is True
    flagged = c.post("/api/compliance/screen", json={"name": "BAD ACTOR"}).get_json()
    assert flagged["clear"] is False and flagged["hits"]


# --- tenant isolation ----------------------------------------------------------

def _load_payload():
    return {"shipper_id": "s", "weight_kg": 1000,
            "origin": {"lat": 10.5, "lon": 7.4}, "destination": {"lat": 11.8, "lon": 13.1}}


def test_stamp_labels_tenant_even_when_isolation_off():
    app, _ = _client()
    c = app.test_client()
    load = c.post("/api/loads", headers={"X-Tenant-ID": "acme"}, json=_load_payload()).get_json()
    assert load["tenant_id"] == "acme"


def test_isolation_scopes_reads():
    app, _ = _client(tenant_isolation=True)
    c = app.test_client()
    a = c.post("/api/loads", headers={"X-Tenant-ID": "tA"}, json=_load_payload()).get_json()
    c.post("/api/loads", headers={"X-Tenant-ID": "tB"}, json=_load_payload())
    # tenant A sees only its own load
    listed = c.get("/api/loads", headers={"X-Tenant-ID": "tA"}).get_json()["loads"]
    assert len(listed) == 1 and listed[0]["ref"] == a["ref"]
    # tenant B cannot fetch tenant A's load
    assert c.get(f"/api/loads/{a['ref']}", headers={"X-Tenant-ID": "tB"}).status_code == 404
    # tenant A can
    assert c.get(f"/api/loads/{a['ref']}", headers={"X-Tenant-ID": "tA"}).status_code == 200


def test_isolation_off_sees_all():
    app, _ = _client(tenant_isolation=False)
    c = app.test_client()
    c.post("/api/loads", headers={"X-Tenant-ID": "tA"}, json=_load_payload())
    c.post("/api/loads", headers={"X-Tenant-ID": "tB"}, json=_load_payload())
    assert len(c.get("/api/loads").get_json()["loads"]) == 2


# --- Redis-backed event bus (with an in-memory fake) ---------------------------

class _FakePubSub:
    def __init__(self, registry, lock):
        self._registry = registry
        self._lock = lock
        self._q: Queue = Queue()
        self._closed = False

    def subscribe(self, channel):
        with self._lock:
            self._registry.setdefault(channel, []).append(self._q)

    def listen(self):
        while not self._closed:
            try:
                yield self._q.get(timeout=0.1)
            except Empty:
                continue

    def close(self):
        self._closed = True


class _FakeRedis:
    def __init__(self):
        self._registry = {}
        self._lock = threading.Lock()

    def pubsub(self):
        return _FakePubSub(self._registry, self._lock)

    def publish(self, channel, data):
        with self._lock:
            qs = list(self._registry.get(channel, []))
        for q in qs:
            q.put({"type": "message", "data": data})
        return len(qs)


def test_make_event_bus_without_url_is_in_process():
    from agri_platform.common.eventbus import EventBus
    assert isinstance(make_event_bus(None), EventBus)


def test_redis_event_bus_delivers_across_subscribe():
    bus = RedisEventBus(client=_FakeRedis())
    q = bus.subscribe("shipment:x")
    time.sleep(0.05)  # let the listener thread subscribe
    assert bus.publish("shipment:x", {"event_type": "location", "lat": 1}) == 1
    msg = q.get(timeout=2)
    assert msg["event_type"] == "location"
    bus.unsubscribe("shipment:x", q)
