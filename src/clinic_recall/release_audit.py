"""Provision and verify the ordinary SELECT-only release inventory role."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import SupportsInt, cast

import sqlalchemy as sa
from alembic.script import ScriptDirectory
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy.engine import Engine, make_url

from .db import get_engine
from .models import Base
from .release_migration import (
    MigrationAuthorizationError,
    MigrationExecutionError,
    _alembic_config,
    _bootstrap_runtime_configuration,
    inspect_database,
    require_expected_target,
)

_ROLE_NAME = "clinic_recall_audit"
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")


class AuditRoleError(RuntimeError):
    """Raised when the release inventory role is absent, elevated, or unauthorized."""


@dataclass(frozen=True)
class AuditRoleEvidence:
    """Secret-free privilege evidence for the release inventory role."""

    superuser: bool
    bypass_rls: bool
    create_db: bool
    create_role: bool
    replication: bool = False
    role_memberships: int = 0


def require_ordinary_audit_role(evidence: AuditRoleEvidence) -> None:
    """Fail if any elevated PostgreSQL role attribute is present."""
    if any(asdict(evidence).values()):
        raise AuditRoleError("release inventory role has elevated privileges")


def _audit_password(*, expected_host: str, expected_database: str) -> str:
    raw = os.getenv("CLINIC_RECALL_AUDIT_DATABASE_URL", "").strip()
    if not raw:
        raise AuditRoleError("release inventory database credential is unavailable")
    if "://" in raw:
        parsed = make_url(raw)
        username = parsed.username
        password = parsed.password
        host = parsed.host
        database = parsed.database
    else:
        parsed = conninfo_to_dict(raw)
        username = parsed.get("user")
        password = parsed.get("password")
        host = parsed.get("host")
        database = parsed.get("dbname")
    if username != _ROLE_NAME or not password:
        raise AuditRoleError("release inventory database credential is invalid")
    if (host or "").strip().lower().rstrip(".") != expected_host.strip().lower().rstrip(
        "."
    ) or (database or "").strip() != expected_database.strip():
        raise AuditRoleError("release inventory database credential target mismatch")
    return password


def _source_identity() -> tuple[str, str]:
    source_sha = os.getenv("GIT_SHA", "").strip().lower()
    heads = ScriptDirectory.from_config(_alembic_config()).get_heads()
    if _SOURCE_SHA.fullmatch(source_sha) is None or len(heads) != 1:
        raise AuditRoleError("release source identity is invalid")
    return source_sha, heads[0]


def _require_authorization(authorization: str) -> None:
    source_sha, source_head = _source_identity()
    expected = f"{source_sha}:{source_head}"
    if not hmac.compare_digest(authorization.strip(), expected):
        raise AuditRoleError("exact source/head audit-role authorization is required")


def require_role_evidence(row: object) -> AuditRoleEvidence:
    """Normalize one PostgreSQL role row and enforce ordinary privileges."""
    if row is None:
        raise AuditRoleError("release inventory role verification failed")
    values: tuple[object, ...] = tuple(row)  # type: ignore[arg-type]
    evidence = AuditRoleEvidence(
        superuser=bool(values[0]),
        bypass_rls=bool(values[1]),
        create_db=bool(values[2]),
        create_role=bool(values[3]),
        replication=bool(values[4]) if len(values) > 4 else False,
        role_memberships=int(cast(SupportsInt, values[5])) if len(values) > 5 else 0,
    )
    require_ordinary_audit_role(evidence)
    return evidence


def require_select_only_table_access(connection: sa.Connection) -> None:
    """Require SELECT and reject every mutation privilege on inventory tables."""
    for table_name in sorted({"alembic_version", *Base.metadata.tables}):
        qualified_name = f"public.{table_name}"
        row = connection.execute(
            sa.text(
                "SELECT has_table_privilege(current_user, :table_name, 'SELECT'), "
                "has_table_privilege(current_user, :table_name, 'INSERT'), "
                "has_table_privilege(current_user, :table_name, 'UPDATE'), "
                "has_table_privilege(current_user, :table_name, 'DELETE'), "
                "has_table_privilege(current_user, :table_name, 'TRUNCATE'), "
                "has_table_privilege(current_user, :table_name, 'REFERENCES'), "
                "has_table_privilege(current_user, :table_name, 'TRIGGER')"
            ),
            {"table_name": qualified_name},
        ).one()
        if not bool(row[0]) or any(bool(value) for value in row[1:]):
            raise AuditRoleError("release inventory table privileges are not SELECT-only")


def _require_database_target(
    engine: Engine,
    *,
    expected_host: str,
    expected_database: str,
) -> None:
    snapshot = inspect_database(engine, config=_alembic_config())
    try:
        require_expected_target(
            snapshot,
            expected_host=expected_host,
            expected_database=expected_database,
        )
    except MigrationAuthorizationError as exc:
        raise AuditRoleError("release inventory database target mismatch") from exc
    if snapshot.current_head != snapshot.source_head:
        raise AuditRoleError("release inventory database is not at the source head")


def provision_audit_role(
    engine: Engine,
    *,
    authorization: str,
    expected_host: str,
    expected_database: str,
) -> AuditRoleEvidence:
    """Create or reconcile the audit role and grant SELECT only."""
    _require_database_target(
        engine,
        expected_host=expected_host,
        expected_database=expected_database,
    )
    _require_authorization(authorization)
    password = _audit_password(
        expected_host=expected_host,
        expected_database=expected_database,
    )
    raw_connection = engine.raw_connection()
    try:
        with raw_connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            database_name = cursor.fetchone()[0]
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (_ROLE_NAME,))
            role_exists = cursor.fetchone() is not None
            role_identifier = sql.Identifier(_ROLE_NAME)
            password_literal = sql.Literal(password)
            if not role_exists:
                cursor.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS "
                        "NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION"
                    ).format(role_identifier, password_literal)
                )
            cursor.execute(
                sql.SQL(
                    "ALTER ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS "
                    "NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION"
                ).format(role_identifier, password_literal)
            )
            cursor.execute(
                "SELECT parent.rolname FROM pg_auth_members membership "
                "JOIN pg_roles parent ON parent.oid = membership.roleid "
                "JOIN pg_roles member ON member.oid = membership.member "
                "WHERE member.rolname = %s",
                (_ROLE_NAME,),
            )
            for (parent_role,) in cursor.fetchall():
                cursor.execute(
                    sql.SQL("REVOKE {} FROM {}").format(
                        sql.Identifier(parent_role), role_identifier
                    )
                )
            cursor.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(
                    sql.Identifier(database_name), role_identifier
                )
            )
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database_name), role_identifier
                )
            )
            cursor.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM {}").format(
                    role_identifier
                )
            )
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role_identifier)
            )
            cursor.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}").format(
                    role_identifier
                )
            )
            cursor.execute(
                sql.SQL(
                    "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}"
                ).format(role_identifier)
            )
            cursor.execute(
                sql.SQL(
                    "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM {}"
                ).format(role_identifier)
            )
            for object_type in ("TABLES", "SEQUENCES", "FUNCTIONS"):
                cursor.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                        "REVOKE ALL PRIVILEGES ON {} FROM {}"
                    ).format(sql.SQL(object_type), role_identifier)
                )
            for table_name in sorted({"alembic_version", *Base.metadata.tables}):
                cursor.execute(
                    sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(
                        sql.Identifier("public", table_name), role_identifier
                    )
                )
            cursor.execute(
                "SELECT role.rolsuper, role.rolbypassrls, role.rolcreatedb, "
                "role.rolcreaterole, role.rolreplication, "
                "(SELECT count(*) FROM pg_auth_members membership "
                "WHERE membership.member = role.oid) "
                "FROM pg_roles role WHERE role.rolname = %s",
                (_ROLE_NAME,),
            )
            evidence = require_role_evidence(cursor.fetchone())
        raw_connection.commit()
    except Exception:
        raw_connection.rollback()
        raise
    finally:
        raw_connection.close()
    return evidence


def inspect_audit_connection(
    *,
    expected_host: str,
    expected_database: str,
) -> tuple[AuditRoleEvidence, str | None]:
    """Verify the audit credential in a read-only transaction."""
    raw = os.getenv("CLINIC_RECALL_AUDIT_DATABASE_URL", "").strip()
    if not raw:
        raise AuditRoleError("release inventory database credential is unavailable")
    if "://" not in raw:
        parts = conninfo_to_dict(raw)
        raw = sa.URL.create(
            "postgresql+psycopg",
            username=parts.get("user"),
            password=parts.get("password"),
            host=parts.get("host"),
            port=int(parts.get("port") or 5432),
            database=parts.get("dbname"),
            query={"sslmode": parts.get("sslmode") or "require"},
        ).render_as_string(hide_password=False)
    audit_engine = sa.create_engine(raw, pool_pre_ping=True)
    try:
        snapshot = inspect_database(audit_engine, config=_alembic_config())
        try:
            require_expected_target(
                snapshot,
                expected_host=expected_host,
                expected_database=expected_database,
            )
        except MigrationAuthorizationError as exc:
            raise AuditRoleError("release inventory database target mismatch") from exc
        with audit_engine.connect() as connection:
            connection.execute(sa.text("SET TRANSACTION READ ONLY"))
            evidence = require_role_evidence(
                connection.execute(
                    sa.text(
                        "SELECT role.rolsuper, role.rolbypassrls, role.rolcreatedb, "
                        "role.rolcreaterole, role.rolreplication, "
                        "(SELECT count(*) FROM pg_auth_members membership "
                        "WHERE membership.member = role.oid) "
                        "FROM pg_roles role WHERE role.rolname = current_user"
                    )
                ).one()
            )
            require_select_only_table_access(connection)
        head = snapshot.current_head
    finally:
        audit_engine.dispose()
    return evidence, head


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure the release inventory role.")
    parser.add_argument(
        "--authorization",
        default=os.getenv("CLINIC_RECALL_AUDIT_ROLE_AUTHORIZATION", ""),
    )
    parser.add_argument(
        "--expected-host",
        default=os.getenv("CLINIC_RECALL_EXPECTED_DATABASE_HOST", ""),
    )
    parser.add_argument(
        "--expected-database",
        default=os.getenv("CLINIC_RECALL_EXPECTED_DATABASE_NAME", ""),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _bootstrap_runtime_configuration(require_audit=True)
        provision_audit_role(
            get_engine(),
            authorization=args.authorization,
            expected_host=args.expected_host,
            expected_database=args.expected_database,
        )
        evidence, head = inspect_audit_connection(
            expected_host=args.expected_host,
            expected_database=args.expected_database,
        )
    except (AuditRoleError, MigrationExecutionError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"status": "blocked", "reason": "audit role setup failed"}))
        return 2
    print(json.dumps({"status": "ready", "head": head, **asdict(evidence)}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())