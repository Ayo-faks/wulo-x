"""add receipted human handoffs and immutable SLA ageing

Revision ID: 0024_receipted_handoffs
Revises: 0023_identity_evidence_tiers
Create Date: 2026-07-27
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from src.clinic_recall.enums import (
    HandoffAlternateState,
    HandoffDeliveryState,
    HandoffSeverity,
)
from src.clinic_recall.models import RLS_GUC

revision: str = "0024_receipted_handoffs"
down_revision: str | None = "0023_identity_evidence_tiers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "handoff_receipt"
_POLICY_VERSION = "pilot-handoff-sla-v1"
_POLICY_SHA256 = "eb2347df16d42bd33cd5c339a28697dbb2b92a12043f5e8f7f77d8715d378d86"
_DEFAULT_TIMEZONE = "Europe/London"
_DEFAULT_START_HOUR = 8
_DEFAULT_END_HOUR = 20
_OWNER_UNIQUES = {
    "escalation": "uq_escalation_clinic_id_id",
    "inbound_staff_task": "uq_inbound_staff_task_clinic_id_id",
    "booking_action": "uq_booking_action_clinic_id_id",
    "external_effect_handoff": "uq_external_effect_handoff_clinic_id_id",
}
_BACKFILL_SOURCE_TABLES = (
    "clinic",
    "escalation",
    "inbound_staff_task",
    "booking_action",
    "external_effect_handoff",
    "handoff_receipt",
)
_NEW_ENUMS = (
    (HandoffSeverity, "handoff_severity"),
    (HandoffDeliveryState, "handoff_delivery_state"),
    (HandoffAlternateState, "handoff_alternate_state"),
)


def _enum(py_enum, name: str) -> sa.Enum:
    values = [member.value for member in py_enum]
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(
        py_enum,
        name=name,
        native_enum=True,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


def upgrade() -> None:
    """Add one normalized receipt and backfill current human work without I/O."""
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if bind.dialect.name == "postgresql":
        _extend_postgres_enums(bind)
    _ensure_owner_contracts()
    if _TABLE not in tables:
        _create_handoff_receipt()
    else:
        actual = {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}
        expected = {
            "id",
            "clinic_id",
            "escalation_id",
            "inbound_staff_task_id",
            "booking_action_id",
            "external_effect_handoff_id",
            "severity",
            "delivery_state",
            "queued_at",
            "due_at",
            "sent_at",
            "delivered_at",
            "acknowledged_at",
            "acknowledged_by",
            "resolved_at",
            "resolved_by",
            "policy_version",
            "policy_sha256",
            "policy_critical_minutes",
            "policy_high_minutes",
            "policy_normal_business_hours",
            "severity_generation",
            "notification_count",
            "escalation_level",
            "alternate_state",
            "alternate_requested_at",
            "created_at",
            "updated_at",
        }
        if actual != expected:
            raise RuntimeError("0024 found a partial handoff receipt schema")
    _ensure_indexes()
    _backfill_receipts()
    if bind.dialect.name == "postgresql":
        _apply_policy()


def downgrade() -> None:
    """Remove only an entirely empty PR-12 schema and owner extension."""
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if _TABLE in tables:
        _assert_downgrade_safe()
        op.drop_table(_TABLE)
    _drop_owner_contracts()
    if bind.dialect.name == "postgresql":
        for _py_enum, name in reversed(_NEW_ENUMS):
            postgresql.ENUM(name=name).drop(bind, checkfirst=True)
    # Values added to existing PostgreSQL enum types are intentionally irreversible.


def _extend_postgres_enums(bind) -> None:
    for py_enum, name in _NEW_ENUMS:
        postgresql.ENUM(
            *[member.value for member in py_enum],
            name=name,
        ).create(bind, checkfirst=True)
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                "ALTER TYPE external_effect_type ADD VALUE IF NOT EXISTS "
                "'handoff_notification'"
            )
        )
        op.execute(
            sa.text("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'acknowledge'")
        )
        op.execute(sa.text("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'resolve'"))
        op.execute(
            sa.text(
                "ALTER TYPE provider_callback_kind ADD VALUE IF NOT EXISTS 'email'"
            )
        )


def _ensure_owner_contracts() -> None:
    bind = op.get_bind()
    for table, unique_name in _OWNER_UNIQUES.items():
        inspector = sa.inspect(bind)
        uniques = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(table)
            if constraint.get("name")
        }
        checks = {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table)
            if constraint.get("name")
        }
        recreate = "always" if bind.dialect.name == "sqlite" else "auto"
        with op.batch_alter_table(table, recreate=recreate) as batch:
            if table == "external_effect_handoff":
                if "ck_external_effect_handoff_status_queued" in checks:
                    batch.drop_constraint(
                        "ck_external_effect_handoff_status_queued",
                        type_="check",
                    )
                if "ck_external_effect_handoff_status" not in checks:
                    batch.create_check_constraint(
                        "ck_external_effect_handoff_status",
                        "status IN ('queued', 'acknowledged', 'resolved')",
                    )
            if unique_name not in uniques:
                batch.create_unique_constraint(unique_name, ["clinic_id", "id"])


def _drop_owner_contracts() -> None:
    bind = op.get_bind()
    for table, unique_name in reversed(tuple(_OWNER_UNIQUES.items())):
        inspector = sa.inspect(bind)
        uniques = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(table)
            if constraint.get("name")
        }
        checks = {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table)
            if constraint.get("name")
        }
        recreate = "always" if bind.dialect.name == "sqlite" else "auto"
        with op.batch_alter_table(table, recreate=recreate) as batch:
            if unique_name in uniques:
                batch.drop_constraint(unique_name, type_="unique")
            if table == "external_effect_handoff":
                if "ck_external_effect_handoff_status" in checks:
                    batch.drop_constraint(
                        "ck_external_effect_handoff_status",
                        type_="check",
                    )
                if "ck_external_effect_handoff_status_queued" not in checks:
                    batch.create_check_constraint(
                        "ck_external_effect_handoff_status_queued",
                        "status = 'queued'",
                    )


def _create_handoff_receipt() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("clinic_id", sa.String(), nullable=False),
        sa.Column("escalation_id", sa.String(), nullable=True),
        sa.Column("inbound_staff_task_id", sa.String(), nullable=True),
        sa.Column("booking_action_id", sa.String(), nullable=True),
        sa.Column("external_effect_handoff_id", sa.String(), nullable=True),
        sa.Column("severity", _enum(HandoffSeverity, "handoff_severity"), nullable=False),
        sa.Column(
            "delivery_state",
            _enum(HandoffDeliveryState, "handoff_delivery_state"),
            server_default=HandoffDeliveryState.QUEUED.value,
            nullable=False,
        ),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=200), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(length=200), nullable=True),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("policy_critical_minutes", sa.Integer(), nullable=False),
        sa.Column("policy_high_minutes", sa.Integer(), nullable=False),
        sa.Column("policy_normal_business_hours", sa.Integer(), nullable=False),
        sa.Column("severity_generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("notification_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("escalation_level", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "alternate_state",
            _enum(HandoffAlternateState, "handoff_alternate_state"),
            server_default=HandoffAlternateState.NOT_REQUESTED.value,
            nullable=False,
        ),
        sa.Column("alternate_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(CASE WHEN escalation_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN inbound_staff_task_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN booking_action_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN external_effect_handoff_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_handoff_receipt_exactly_one_owner",
        ),
        sa.CheckConstraint(
            "due_at >= queued_at AND "
            "(sent_at IS NULL OR sent_at >= queued_at) AND "
            "(delivered_at IS NULL OR "
            "(sent_at IS NOT NULL AND delivered_at >= sent_at)) AND "
            "(acknowledged_at IS NULL OR acknowledged_at >= queued_at) AND "
            "(resolved_at IS NULL OR "
            "(acknowledged_at IS NOT NULL AND resolved_at >= acknowledged_at))",
            name="ck_handoff_receipt_timestamp_order",
        ),
        sa.CheckConstraint(
            "(acknowledged_at IS NULL AND acknowledged_by IS NULL) OR "
            "(acknowledged_at IS NOT NULL AND acknowledged_by IS NOT NULL)",
            name="ck_handoff_receipt_ack_complete",
        ),
        sa.CheckConstraint(
            "(resolved_at IS NULL AND resolved_by IS NULL) OR "
            "(resolved_at IS NOT NULL AND resolved_by IS NOT NULL "
            "AND acknowledged_at IS NOT NULL)",
            name="ck_handoff_receipt_resolution_complete",
        ),
        sa.CheckConstraint(
            "(alternate_state = 'not_requested' AND alternate_requested_at IS NULL) OR "
            "(alternate_state = 'requested' AND alternate_requested_at IS NOT NULL)",
            name="ck_handoff_receipt_alternate_complete",
        ),
        sa.CheckConstraint(
            "(delivery_state = 'queued' AND sent_at IS NULL AND delivered_at IS NULL) OR "
            "(delivery_state = 'sent' AND sent_at IS NOT NULL AND delivered_at IS NULL) OR "
            "(delivery_state = 'delivered' AND sent_at IS NOT NULL "
            "AND delivered_at IS NOT NULL) OR "
            "(delivery_state IN ('definitive_failure', 'reconcile_required') "
            "AND delivered_at IS NULL)",
            name="ck_handoff_receipt_delivery_evidence",
        ),
        sa.CheckConstraint(
            "length(policy_version) BETWEEN 1 AND 128 AND length(policy_sha256) = 64",
            name="ck_handoff_receipt_policy_identity",
        ),
        sa.CheckConstraint(
            "policy_critical_minutes BETWEEN 1 AND 5 AND "
            "policy_high_minutes BETWEEN 1 AND 15 AND "
            "policy_normal_business_hours BETWEEN 1 AND 4",
            name="ck_handoff_receipt_policy_bounds",
        ),
        sa.CheckConstraint(
            "(acknowledged_by IS NULL OR length(acknowledged_by) BETWEEN 1 AND 200) "
            "AND (resolved_by IS NULL OR length(resolved_by) BETWEEN 1 AND 200)",
            name="ck_handoff_receipt_actor_bounds",
        ),
        sa.CheckConstraint(
            "severity_generation BETWEEN 0 AND 16 AND "
            "notification_count BETWEEN 0 AND 32 AND "
            "escalation_level BETWEEN 0 AND 1",
            name="ck_handoff_receipt_counter_bounds",
        ),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["clinic_id", "escalation_id"],
            ["escalation.clinic_id", "escalation.id"],
            ondelete="RESTRICT",
            name="fk_handoff_receipt_escalation_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "inbound_staff_task_id"],
            ["inbound_staff_task.clinic_id", "inbound_staff_task.id"],
            ondelete="RESTRICT",
            name="fk_handoff_receipt_inbound_task_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "booking_action_id"],
            ["booking_action.clinic_id", "booking_action.id"],
            ondelete="RESTRICT",
            name="fk_handoff_receipt_booking_action_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "external_effect_handoff_id"],
            ["external_effect_handoff.clinic_id", "external_effect_handoff.id"],
            ondelete="RESTRICT",
            name="fk_handoff_receipt_external_handoff_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "clinic_id", "id", name="uq_handoff_receipt_clinic_id_id"
        ),
        sa.UniqueConstraint(
            "clinic_id", "escalation_id", name="uq_handoff_receipt_escalation"
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "inbound_staff_task_id",
            name="uq_handoff_receipt_inbound_task",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "booking_action_id",
            name="uq_handoff_receipt_booking_action",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "external_effect_handoff_id",
            name="uq_handoff_receipt_external_handoff",
        ),
    )


def _ensure_indexes() -> None:
    required = {
        "ix_handoff_receipt_clinic_id": ["clinic_id"],
        "ix_handoff_receipt_clinic_state_due": ["clinic_id", "delivery_state", "due_at"],
        "ix_handoff_receipt_clinic_severity_due": ["clinic_id", "severity", "due_at"],
        "ix_handoff_receipt_clinic_open_due": [
            "clinic_id",
            "acknowledged_at",
            "resolved_at",
            "due_at",
        ],
    }
    existing = {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
    }
    for name, columns in required.items():
        if name not in existing:
            op.create_index(name, _TABLE, columns)


def _backfill_receipts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in _BACKFILL_SOURCE_TABLES:
            op.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
    _backfill_receipts_unforced(bind)
    if bind.dialect.name == "postgresql":
        for table in _BACKFILL_SOURCE_TABLES:
            op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))


def _backfill_receipts_unforced(bind) -> None:
    metadata = sa.MetaData()
    receipt = sa.Table(_TABLE, metadata, autoload_with=bind)
    clinic = sa.Table("clinic", metadata, autoload_with=bind)
    clinic_rows = {
        row.id: row
        for row in bind.execute(
            sa.select(clinic.c.id, clinic.c.timezone, clinic.c.contact_hours)
        )
    }
    now = datetime.now(UTC)
    specs = (
        (
            "escalation",
            "escalation_id",
            ("open", "acknowledged"),
        ),
        (
            "inbound_staff_task",
            "inbound_staff_task_id",
            ("open", "acknowledged"),
        ),
        ("booking_action", "booking_action_id", ("pending",)),
        (
            "external_effect_handoff",
            "external_effect_handoff_id",
            ("queued", "acknowledged"),
        ),
    )
    for table_name, owner_column, active_statuses in specs:
        owner = sa.Table(table_name, metadata, autoload_with=bind)
        rows = bind.execute(
            sa.select(owner).where(owner.c.status.in_(active_statuses))
        ).mappings()
        for row in rows:
            exists = bind.scalar(
                sa.select(sa.func.count())
                .select_from(receipt)
                .where(
                    receipt.c.clinic_id == row["clinic_id"],
                    receipt.c[owner_column] == row["id"],
                )
            )
            if exists:
                continue
            queued_at = _as_aware(row["created_at"])
            severity = _backfill_severity(table_name, row)
            clinic_row = clinic_rows[row["clinic_id"]]
            due_at = _backfill_due_at(
                queued_at,
                severity,
                str(clinic_row.timezone or _DEFAULT_TIMEZONE),
                clinic_row.contact_hours,
            )
            acknowledged_at = None
            acknowledged_by = None
            if row["status"] == "acknowledged" and row.get("assigned_to"):
                acknowledged_at = _as_aware(row["updated_at"])
                acknowledged_by = str(row["assigned_to"])[:200]
            values = {
                "id": _backfill_id(row["clinic_id"], owner_column, row["id"]),
                "clinic_id": row["clinic_id"],
                "escalation_id": None,
                "inbound_staff_task_id": None,
                "booking_action_id": None,
                "external_effect_handoff_id": None,
                owner_column: row["id"],
                "severity": severity,
                "delivery_state": "queued",
                "queued_at": queued_at,
                "due_at": due_at,
                "sent_at": None,
                "delivered_at": None,
                "acknowledged_at": acknowledged_at,
                "acknowledged_by": acknowledged_by,
                "resolved_at": None,
                "resolved_by": None,
                "policy_version": _POLICY_VERSION,
                "policy_sha256": _POLICY_SHA256,
                "policy_critical_minutes": 5,
                "policy_high_minutes": 15,
                "policy_normal_business_hours": 4,
                "severity_generation": 0,
                "notification_count": 0,
                "escalation_level": 0,
                "alternate_state": "not_requested",
                "alternate_requested_at": None,
                "created_at": now,
                "updated_at": now,
            }
            bind.execute(receipt.insert().values(**values))


def _backfill_severity(table_name: str, row: Mapping[str, object]) -> str:
    if table_name == "escalation":
        reason = str(row.get("reason") or "")
        if reason == "urgent":
            return "critical"
        if reason in {"clinical", "complaint"}:
            return "high"
    if table_name == "inbound_staff_task" and str(row.get("kind") or "") == "escalation":
        reason = str(row.get("reason") or "").strip().lower()
        if reason in {"urgent", "safeguarding"}:
            return "critical"
        if reason in {"distress", "clinical", "complaint"}:
            return "high"
    return "normal"


def _backfill_due_at(
    queued_at: datetime,
    severity: str,
    timezone_name: str,
    contact_hours: object,
) -> datetime:
    if severity == "critical":
        return queued_at + timedelta(minutes=5)
    if severity == "high":
        return queued_at + timedelta(minutes=15)
    zone = _safe_zone(timezone_name)
    start_hour, end_hour = _safe_window(contact_hours)
    cursor = queued_at.astimezone(zone)
    remaining = timedelta(hours=4)
    while remaining > timedelta(0):
        start, end = _window(cursor.date(), zone, start_hour, end_hour)
        if cursor < start:
            cursor = start
        elif cursor >= end:
            cursor = _window(
                cursor.date() + timedelta(days=1), zone, start_hour, end_hour
            )[0]
            continue
        consumed = min(end - cursor, remaining)
        cursor += consumed
        remaining -= consumed
    return cursor.astimezone(UTC)


def _safe_zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value or _DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo(_DEFAULT_TIMEZONE)


def _safe_window(value: object) -> tuple[int, int]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    mapping = value if isinstance(value, Mapping) else {}
    start = mapping.get("start_hour", _DEFAULT_START_HOUR)
    end = mapping.get("end_hour", _DEFAULT_END_HOUR)
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= start < end <= 24
    ):
        return _DEFAULT_START_HOUR, _DEFAULT_END_HOUR
    return start, end


def _window(
    day: date,
    zone: ZoneInfo,
    start_hour: int,
    end_hour: int,
) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(hour=start_hour), tzinfo=zone)
    if end_hour == 24:
        end = datetime.combine(day + timedelta(days=1), time(), tzinfo=zone)
    else:
        end = datetime.combine(day, time(hour=end_hour), tzinfo=zone)
    return start, end


def _as_aware(value: object) -> datetime:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise RuntimeError("0024 found an invalid owner timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _backfill_id(clinic_id: str, owner_column: str, owner_id: str) -> str:
    digest = hashlib.sha256(
        f"{clinic_id}:{owner_column}:{owner_id}".encode()
    ).hexdigest()
    return f"handoff-receipt-{digest[:32]}"


def _assert_downgrade_safe() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        savepoint = bind.begin_nested()
        try:
            op.create_check_constraint(
                "ck_handoff_receipt_pr12_downgrade_empty",
                _TABLE,
                "id IS NULL",
            )
            op.create_check_constraint(
                "ck_external_effect_pr12_downgrade_safe",
                "external_effect",
                "effect_type <> 'handoff_notification'",
            )
            op.create_check_constraint(
                "ck_audit_log_pr12_downgrade_safe",
                "audit_log",
                "action NOT IN ('acknowledge', 'resolve')",
            )
            op.create_check_constraint(
                "ck_external_handoff_pr12_downgrade_safe",
                "external_effect_handoff",
                "status = 'queued'",
            )
        except sa.exc.DBAPIError:
            savepoint.rollback()
            raise RuntimeError(
                "refuse PR-12 downgrade with retained receipt evidence"
            ) from None
        savepoint.commit()
        for table, name in (
            (_TABLE, "ck_handoff_receipt_pr12_downgrade_empty"),
            ("external_effect", "ck_external_effect_pr12_downgrade_safe"),
            ("audit_log", "ck_audit_log_pr12_downgrade_safe"),
            (
                "external_effect_handoff",
                "ck_external_handoff_pr12_downgrade_safe",
            ),
        ):
            op.drop_constraint(name, table, type_="check")
        return
    retained = bind.scalar(sa.text("SELECT count(*) FROM handoff_receipt"))
    effects = bind.scalar(
        sa.text(
            "SELECT count(*) FROM external_effect "
            "WHERE effect_type = 'handoff_notification'"
        )
    )
    audits = bind.scalar(
        sa.text(
            "SELECT count(*) FROM audit_log "
            "WHERE action IN ('acknowledge', 'resolve')"
        )
    )
    owner_states = bind.scalar(
        sa.text(
            "SELECT count(*) FROM external_effect_handoff WHERE status <> 'queued'"
        )
    )
    if retained or effects or audits or owner_states:
        raise RuntimeError("refuse PR-12 downgrade with retained receipt evidence")


def _apply_policy() -> None:
    policy = "handoff_receipt_tenant_isolation"
    predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
    op.execute(sa.text(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {_TABLE}"))
    op.execute(
        sa.text(
            f"CREATE POLICY {policy} ON {_TABLE} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )