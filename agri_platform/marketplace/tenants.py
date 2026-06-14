"""Row-level multi-tenant scoping.

The tenant comes from the ``X-Tenant-ID`` header (captured on ``g`` by the
resilience middleware). ``stamp`` labels new rows with the current tenant;
``scope`` filters a query to the current tenant when isolation is enabled. With
isolation off (default) nothing changes, so single-tenant deployments and the
existing tests are unaffected.
"""

from __future__ import annotations

from flask import current_app, g
from sqlalchemy import event
from sqlalchemy.orm import with_loader_criteria


def _safe_tenant():
    try:
        return getattr(g, "tenant_id", None)
    except Exception:  # outside an application/request context
        return None


def current_tenant():
    return _safe_tenant()


def isolation_enabled() -> bool:
    return bool(current_app.config.get("TENANT_ISOLATION"))


def stamp(obj):
    """Label a new row with the current tenant (if it has the column)."""
    tenant = current_tenant()
    if tenant and hasattr(obj, "tenant_id") and getattr(obj, "tenant_id", None) is None:
        obj.tenant_id = tenant
    return obj


def scope(query, model):
    """Restrict a query to the current tenant when isolation is enabled.

    Mostly redundant once :func:`install_tenant_guard` is active, but kept for
    explicit, readable scoping at call sites.
    """
    if not isolation_enabled():
        return query
    tenant = current_tenant()
    if tenant and hasattr(model, "tenant_id"):
        return query.filter(model.tenant_id == tenant)
    return query


def install_tenant_guard(session_factory, base, is_enabled) -> None:
    """Auto-stamp new rows and auto-filter reads for *every* tenant model.

    A ``before_flush`` hook stamps the current tenant onto any new object that has
    a ``tenant_id`` column; a ``do_orm_execute`` hook applies a tenant predicate to
    all ORM SELECTs (covering joins, counts and lazy loads), so no endpoint can
    leak cross-tenant data. Filtering only engages when isolation is enabled and a
    tenant is present; stamping always labels rows when a tenant is present.
    """
    sm = getattr(session_factory, "session_factory", session_factory)
    # Every mapped model that carries a tenant column.
    tenant_models = [m.class_ for m in base.registry.mappers if hasattr(m.class_, "tenant_id")]

    @event.listens_for(sm, "before_flush")
    def _stamp(session, flush_context, instances):  # noqa: ANN001
        tenant = _safe_tenant()
        if not tenant:
            return
        for obj in session.new:
            if hasattr(obj, "tenant_id") and getattr(obj, "tenant_id", None) is None:
                obj.tenant_id = tenant

    @event.listens_for(sm, "do_orm_execute")
    def _filter(state):  # noqa: ANN001
        if not state.is_select or state.execution_options.get("skip_tenant"):
            return
        if not is_enabled():
            return
        tenant = _safe_tenant()
        if not tenant:
            return
        # Direct bound expressions (not lambdas) so statement caching stays valid;
        # criteria for models not present in the query are ignored cheaply.
        for model in tenant_models:
            state.statement = state.statement.options(
                with_loader_criteria(model, model.tenant_id == tenant, include_aliases=True)
            )
