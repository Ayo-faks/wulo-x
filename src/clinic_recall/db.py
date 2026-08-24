"""Engine, session, and per-clinic scoping for the Clinic Recall data plane.

Per-clinic isolation is enforced in depth:

* **Database layer** - PostgreSQL row-level security. :func:`clinic_scope` sets
  the ``app.clinic_id`` session variable (via ``set_config``) that the RLS
  policies read. With no clinic set, ``current_setting('app.clinic_id', true)``
  returns ``NULL`` and every policy denies access (fail closed).
* **Application layer** - :func:`current_clinic_id` exposes the active tenant
  and :func:`tenant_select` builds queries already filtered by ``clinic_id``,
  so a missing or mismatched scope can never silently read another clinic.

Both layers must hold. The RLS half is exercised by the ``postgres``-marked
isolation tests; the app-layer half is exercised everywhere (including SQLite).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import RLS_GUC, Base

# The tenant bound to the current execution context. ``None`` means "no clinic
# scope set" — callers must establish a scope before touching tenant data.
_current_clinic_id: ContextVar[str | None] = ContextVar("clinic_recall_clinic_id", default=None)

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None
_privacy_engine: Engine | None = None
_PrivacySessionLocal: sessionmaker[Session] | None = None


def configure_engine(url: str, **engine_kwargs: object) -> Engine:
    """Create (or replace) the process-wide engine and session factory.

    Args:
        url: SQLAlchemy database URL.
        **engine_kwargs: Extra keyword arguments forwarded to ``create_engine``.

    Returns:
        The configured :class:`~sqlalchemy.engine.Engine`.
    """
    global _engine, _SessionLocal
    _engine = sa.create_engine(url, **engine_kwargs)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    """Return the configured engine, building it from config on first use."""
    if _engine is None:
        from .config import get_database_url

        configure_engine(get_database_url(), pool_pre_ping=True)
    assert _engine is not None  # nosec B101 - just configured above
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    """Return the session factory, building the engine if needed."""
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None  # nosec B101 - just configured above
    return _SessionLocal


def configure_privacy_engine(url: str, **engine_kwargs: object) -> Engine:
    """Create or replace the ordinary-role privacy engine and session factory."""
    global _privacy_engine, _PrivacySessionLocal
    _privacy_engine = sa.create_engine(url, **engine_kwargs)
    _PrivacySessionLocal = sessionmaker(
        bind=_privacy_engine,
        expire_on_commit=False,
    )
    return _privacy_engine


def get_privacy_engine() -> Engine:
    """Return the ordinary-role engine, building it from privacy config."""
    if _privacy_engine is None:
        from .config import get_privacy_database_url

        configure_privacy_engine(
            get_privacy_database_url(),
            pool_pre_ping=True,
        )
    assert _privacy_engine is not None  # nosec B101 - just configured above
    return _privacy_engine


def get_privacy_sessionmaker() -> sessionmaker[Session]:
    """Return the dedicated rights/retention session factory."""
    if _PrivacySessionLocal is None:
        get_privacy_engine()
    assert _PrivacySessionLocal is not None  # nosec B101 - just configured above
    return _PrivacySessionLocal


def current_clinic_id() -> str:
    """Return the clinic bound to the current context.

    Raises:
        LookupError: If no clinic scope is active (fail closed).
    """
    clinic_id = _current_clinic_id.get()
    if clinic_id is None:
        raise LookupError(
            "No clinic scope is active; wrap tenant access in clinic_scope(...)."
        )
    return clinic_id


@contextmanager
def clinic_scope(session: Session, clinic_id: str) -> Iterator[Session]:
    """Bind ``session`` to a single clinic for both RLS and app-layer scoping.

    On PostgreSQL this sets the transaction-local ``app.clinic_id`` variable the
    RLS policies read. On other backends (SQLite in tests) only the app-layer
    context variable is set. The scope is always reset on exit.

    Args:
        session: An open SQLAlchemy session.
        clinic_id: The tenant to scope to.

    Yields:
        The same ``session``, now scoped to ``clinic_id``.
    """
    token = _current_clinic_id.set(clinic_id)
    try:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            # set_config(..., is_local=true) scopes the GUC to this transaction.
            # Bound parameter (not string interpolation) avoids any injection.
            session.execute(
                sa.text("SELECT set_config(:guc, :cid, true)"),
                {"guc": RLS_GUC, "cid": clinic_id},
            )
        yield session
    finally:
        _current_clinic_id.reset(token)


def tenant_select(model: type[Base]) -> sa.Select[Any]:
    """Build a ``SELECT`` over ``model`` filtered to the active clinic.

    Uses ``clinic_id`` for tenant tables and ``id`` for the ``clinic`` table.

    Raises:
        LookupError: If no clinic scope is active.
    """
    clinic_id = current_clinic_id()
    scoped = cast(Any, model)
    scope_column = scoped.id if scoped.__tablename__ == "clinic" else scoped.clinic_id
    return sa.select(model).where(scope_column == clinic_id)
