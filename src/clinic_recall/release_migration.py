"""Source-bound, expand-only database migration gate for release deployment."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

from .db import get_engine

_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")


class MigrationAuthorizationError(RuntimeError):
    """Raised when a stale database is not authorized for this source/head."""


class MigrationExecutionError(RuntimeError):
    """Raised when migration execution cannot prove the expected terminal head."""


@dataclass(frozen=True)
class MigrationSnapshot:
    """Secret-free database and source identity used by the migration gate."""

    current_head: str | None
    source_head: str
    source_sha: str
    database_host: str
    database_name: str
    role_superuser: bool
    role_bypass_rls: bool
    role_create_db: bool
    role_create_role: bool


def upgrade_required(snapshot: MigrationSnapshot, *, authorization: str) -> bool:
    """Return whether an upgrade is authorized, or fail closed."""
    if _SOURCE_SHA.fullmatch(snapshot.source_sha) is None:
        raise MigrationAuthorizationError("deployed source identity is invalid")
    if snapshot.current_head == snapshot.source_head:
        return False
    if snapshot.current_head is None:
        raise MigrationAuthorizationError("database migration head is missing")
    expected = f"{snapshot.source_sha}:{snapshot.source_head}"
    if not hmac.compare_digest(authorization.strip(), expected):
        raise MigrationAuthorizationError("exact source/head migration authorization is required")
    return True


def require_expected_target(
    snapshot: MigrationSnapshot,
    *,
    expected_host: str,
    expected_database: str,
) -> None:
    """Require the connected database to match the selected release environment."""
    normalized_host = expected_host.strip().lower().rstrip(".")
    normalized_database = expected_database.strip()
    if not normalized_host or not normalized_database:
        raise MigrationAuthorizationError("exact database target is required")
    if not hmac.compare_digest(snapshot.database_host, normalized_host):
        raise MigrationAuthorizationError("database host does not match release target")
    if not hmac.compare_digest(snapshot.database_name, normalized_database):
        raise MigrationAuthorizationError("database name does not match release target")


def _alembic_config() -> Config:
    root = Path(__file__).resolve().parents[2]
    path = root / "infra" / "postgres" / "alembic.ini"
    if not path.is_file():
        raise MigrationExecutionError("Alembic release assets are unavailable")
    return Config(str(path))


def _source_head(config: Config) -> str:
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise MigrationExecutionError("release source must have exactly one migration head")
    return heads[0]


def _has_explicit_database_target() -> bool:
    if os.getenv("CLINIC_RECALL_DATABASE_URL", "").strip():
        return True
    return all(
        os.getenv(name, "").strip()
        for name in (
            "POSTGRES_HOST",
            "POSTGRES_DATABASE_NAME",
            "POSTGRES_ADMIN_LOGIN",
            "POSTGRES_PASSWORD",
        )
    )


def _bootstrap_runtime_configuration(*, require_audit: bool = False) -> None:
    main_explicit = _has_explicit_database_target()
    audit_explicit = bool(os.getenv("CLINIC_RECALL_AUDIT_DATABASE_URL", "").strip())
    if require_audit and main_explicit != audit_explicit:
        raise MigrationExecutionError("database credentials must use one explicit target")
    if main_explicit:
        return
    if audit_explicit:
        raise MigrationExecutionError("database credentials must use one explicit target")
    if not os.getenv("AZURE_APPCONFIG_ENDPOINT", "").strip():
        return
    from apps.artagent.backend.config.appconfig_provider import bootstrap_appconfig

    if not bootstrap_appconfig():
        raise MigrationExecutionError("App Configuration bootstrap failed")
    if not _has_explicit_database_target():
        raise MigrationExecutionError("database credential is unavailable after bootstrap")
    if require_audit and not os.getenv("CLINIC_RECALL_AUDIT_DATABASE_URL", "").strip():
        raise MigrationExecutionError("audit database credential is unavailable after bootstrap")


def inspect_database(engine: Engine, *, config: Config) -> MigrationSnapshot:
    """Read role flags and Alembic identity in one read-only transaction."""
    with engine.connect() as connection:
        connection.execute(sa.text("SET TRANSACTION READ ONLY"))
        role = connection.execute(
            sa.text(
                "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
                "FROM pg_roles WHERE rolname = current_user"
            )
        ).one()
        current_head = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
        database_name = connection.execute(sa.text("SELECT current_database()")).scalar_one()
    return MigrationSnapshot(
        current_head=current_head,
        source_head=_source_head(config),
        source_sha=os.getenv("GIT_SHA", "").strip().lower(),
        database_host=(engine.url.host or "").strip().lower().rstrip("."),
        database_name=str(database_name),
        role_superuser=bool(role[0]),
        role_bypass_rls=bool(role[1]),
        role_create_db=bool(role[2]),
        role_create_role=bool(role[3]),
    )


def _assert_linear_upgrade(config: Config, current_head: str, source_head: str) -> None:
    script = ScriptDirectory.from_config(config)
    revision = script.get_revision(source_head)
    while revision is not None and revision.revision != current_head:
        down_revision = revision.down_revision
        if isinstance(down_revision, tuple):
            raise MigrationExecutionError("release migration history is not linear")
        revision = script.get_revision(down_revision) if down_revision else None
    if revision is None:
        raise MigrationExecutionError("database head is not an ancestor of release head")


def run_release_migration(
    *,
    authorization: str,
    expected_host: str,
    expected_database: str,
) -> tuple[MigrationSnapshot, bool]:
    """Upgrade to the source head when exactly authorized, then verify it."""
    _bootstrap_runtime_configuration()
    config = _alembic_config()
    engine = get_engine()
    before = inspect_database(engine, config=config)
    require_expected_target(
        before,
        expected_host=expected_host,
        expected_database=expected_database,
    )
    if not upgrade_required(before, authorization=authorization):
        return before, False
    assert before.current_head is not None
    _assert_linear_upgrade(config, before.current_head, before.source_head)
    try:
        command.upgrade(config, before.source_head)
    except Exception as exc:
        raise MigrationExecutionError("database migration failed") from exc
    after = inspect_database(engine, config=config)
    require_expected_target(
        after,
        expected_host=expected_host,
        expected_database=expected_database,
    )
    if after.current_head != before.source_head:
        raise MigrationExecutionError("database migration did not reach release head")
    return after, True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply one source-bound release migration.")
    parser.add_argument(
        "--authorization",
        default=os.getenv("CLINIC_RECALL_MIGRATION_AUTHORIZATION", ""),
        help="Exact non-secret <GIT_SHA>:<ALEMBIC_HEAD> authorization.",
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
        snapshot, upgraded = run_release_migration(
            authorization=args.authorization,
            expected_host=args.expected_host,
            expected_database=args.expected_database,
        )
    except (MigrationAuthorizationError, MigrationExecutionError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"status": "blocked", "reason": "database migration check failed"}))
        return 2
    print(
        json.dumps(
            {"status": "upgraded" if upgraded else "current", **asdict(snapshot)},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())