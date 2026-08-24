"""PostgreSQL row-level security (RLS) policy management.

The policy SQL lives here so the Alembic migration and the ``postgres``-marked
isolation tests apply exactly the same rules — they can never drift.

Every tenant table is locked to the clinic named by the ``app.clinic_id``
session variable (see :func:`clinic_recall.db.clinic_scope`):

* ``ENABLE`` + ``FORCE`` row level security. ``FORCE`` is essential: without it
  the table owner (the application's admin role) bypasses RLS entirely.
* A single ``USING``/``WITH CHECK`` policy
  ``clinic_id = current_setting('app.clinic_id', true)``. When no clinic is set,
  ``current_setting(..., true)`` returns ``NULL`` and the predicate denies every
  row — fail closed.

These statements are PostgreSQL-only and are skipped on other backends.
"""

from __future__ import annotations

from sqlalchemy.engine import Connection

from .models import RLS_GUC, TENANT_TABLES

_POLICY_SUFFIX = "tenant_isolation"


def _policy_name(table: str) -> str:
    return f"{table}_{_POLICY_SUFFIX}"


def apply_rls_policies(connection: Connection) -> None:
    """Enable, force, and create the per-clinic RLS policy on every tenant table."""
    from sqlalchemy import text

    for table in TENANT_TABLES:
        policy = _policy_name(table)
        predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
        connection.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        connection.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        connection.execute(text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
        connection.execute(
            text(
                f"CREATE POLICY {policy} ON {table} "
                f"USING ({predicate}) WITH CHECK ({predicate})"
            )
        )


def drop_rls_policies(connection: Connection) -> None:
    """Drop the per-clinic RLS policy and disable RLS on every tenant table."""
    from sqlalchemy import text

    for table in TENANT_TABLES:
        policy = _policy_name(table)
        connection.execute(text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
        connection.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
