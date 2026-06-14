"""Offset/limit pagination helpers for list endpoints."""

from __future__ import annotations

from typing import Any, Dict, Tuple

from flask import request

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def page_params(default_size: int = DEFAULT_PAGE_SIZE, max_size: int = MAX_PAGE_SIZE) -> Tuple[int, int]:
    """Read ``page`` (1-based) and ``page_size`` from the query string, clamped."""
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        size = int(request.args.get("page_size", default_size))
    except ValueError:
        size = default_size
    size = max(1, min(size, max_size))
    return page, size


def paginate(query, page: int, page_size: int) -> Tuple[list, Dict[str, Any]]:
    """Apply limit/offset to a SQLAlchemy query and return ``(items, meta)``."""
    total = query.order_by(None).count()
    items = query.limit(page_size).offset((page - 1) * page_size).all()
    pages = (total + page_size - 1) // page_size if page_size else 0
    return items, {
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
        "has_next": page < pages,
        "has_prev": page > 1,
    }
