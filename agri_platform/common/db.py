"""Database plumbing shared by the services.

Each service owns its own SQLAlchemy declarative ``Base`` and table set, but the
engine/session wiring is identical, so it lives here. The default URL is a local
SQLite file, which keeps the services runnable (and testable) with zero external
infrastructure; set ``DATABASE_URL`` to a PostgreSQL/PostGIS DSN in production.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import scoped_session, sessionmaker


def make_engine(url: str) -> Engine:
    """Create an engine, applying SQLite-friendly options when needed."""
    connect_args = {}
    if url.startswith("sqlite"):
        # Allow use across the Flask request threads.
        connect_args["check_same_thread"] = False
    return create_engine(url, future=True, connect_args=connect_args)


def make_session_factory(url: str) -> tuple[Engine, scoped_session]:
    """Return ``(engine, Session)`` where ``Session`` is a thread-local factory."""
    engine = make_engine(url)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    return engine, scoped_session(factory)


def init_models(engine: Engine, base, *, drop: bool = False) -> None:
    """Create (optionally recreating) all tables for a declarative ``base``."""
    if drop:
        base.metadata.drop_all(engine)
    base.metadata.create_all(engine)


def default_url(service: str, override: Optional[str] = None) -> str:
    return override or f"sqlite:///{service}.db"
