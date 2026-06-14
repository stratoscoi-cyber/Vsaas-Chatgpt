"""Operational resilience middleware: idempotency, audit log and tenant context.

* Idempotency: a mutating request carrying ``Idempotency-Key`` is executed once;
  repeats return the stored response (no duplicate side effects).
* Audit: admin actions are recorded with actor, tenant, path and status.
* Tenant: ``X-Tenant-ID`` is captured on ``g`` for scoping/audit.
"""

from __future__ import annotations

from flask import Flask, Response, g, request

from .models import AuditLog, IdempotencyRecord

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def install_resilience(app: Flask) -> None:
    def db():
        return app.config["SESSION_FACTORY"]()

    @app.before_request
    def _tenant_and_idempotency():
        g.tenant_id = request.headers.get("X-Tenant-ID")
        g.idempotency_key = None
        if request.method in _MUTATING:
            key = request.headers.get("Idempotency-Key")
            if key:
                g.idempotency_key = key
                rec = db().query(IdempotencyRecord).filter_by(key=key).first()
                if rec is not None:
                    payload = rec.response_json or {}
                    resp = Response(payload.get("body", ""), status=rec.status_code,
                                    content_type=payload.get("content_type", "application/json"))
                    resp.headers["Idempotent-Replay"] = "true"
                    return resp
        return None

    @app.after_request
    def _persist(response):
        # Store idempotent responses (only successful-ish, to allow retries on error).
        key = getattr(g, "idempotency_key", None)
        if key and request.method in _MUTATING and response.status_code < 500:
            session = db()
            if session.query(IdempotencyRecord).filter_by(key=key).first() is None:
                try:
                    body = response.get_data(as_text=True)
                    session.add(IdempotencyRecord(
                        key=key, method=request.method, path=request.path,
                        status_code=response.status_code,
                        response_json={"body": body, "content_type": response.content_type}))
                    session.commit()
                except Exception:  # pragma: no cover - never break the response
                    session.rollback()
        # Audit admin actions.
        if request.path.startswith("/api/admin"):
            session = db()
            try:
                actor = getattr(g, "admin_key", None) or getattr(g, "api_key", None) or "anonymous"
                session.add(AuditLog(actor=str(actor)[:80], tenant_id=getattr(g, "tenant_id", None),
                                     method=request.method, path=request.path[:300],
                                     status_code=response.status_code))
                session.commit()
            except Exception:  # pragma: no cover
                session.rollback()
        return response
