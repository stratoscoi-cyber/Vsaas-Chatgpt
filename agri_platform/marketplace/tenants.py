"""Row-level multi-tenant scoping.

The tenant comes from the ``X-Tenant-ID`` header (captured on ``g`` by the
resilience middleware). ``stamp`` labels new rows with the current tenant;
``scope`` filters a query to the current tenant when isolation is enabled. With
isolation off (default) nothing changes, so single-tenant deployments and the
existing tests are unaffected.
"""

from __future__ import annotations

from flask import current_app, g


def current_tenant():
    return getattr(g, "tenant_id", None)


def isolation_enabled() -> bool:
    return bool(current_app.config.get("TENANT_ISOLATION"))


def stamp(obj):
    """Label a new row with the current tenant (if it has the column)."""
    tenant = current_tenant()
    if tenant and hasattr(obj, "tenant_id") and getattr(obj, "tenant_id", None) is None:
        obj.tenant_id = tenant
    return obj


def scope(query, model):
    """Restrict a query to the current tenant when isolation is enabled."""
    if not isolation_enabled():
        return query
    tenant = current_tenant()
    if tenant and hasattr(model, "tenant_id"):
        return query.filter(model.tenant_id == tenant)
    return query
