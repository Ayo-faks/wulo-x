"""add durable rights, retention, and provider purge workflow

Revision ID: 0019_rights_retention_purge
Revises: 0018_recording_consent_ledger
Create Date: 2026-07-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from src.clinic_recall.enums import (
    RightsRequestKind,
    RightsRequestState,
    RightsResidualCategory,
    RightsTargetAction,
    RightsTargetOwnerType,
    RightsTargetResource,
    RightsTargetState,
    RightsTargetSystem,
)
from src.clinic_recall.models import RLS_GUC

revision: str = "0019_rights_retention_purge"
down_revision: str | None = "0018_recording_consent_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUMS = (
    (RightsRequestKind, "rights_request_kind"),
    (RightsRequestState, "rights_request_state"),
    (RightsTargetSystem, "rights_target_system"),
    (RightsTargetResource, "rights_target_resource"),
    (RightsTargetAction, "rights_target_action"),
    (RightsTargetOwnerType, "rights_target_owner_type"),
    (RightsTargetState, "rights_target_state"),
    (RightsResidualCategory, "rights_residual_category"),
)

_RIGHTS_REQUEST_COLUMNS = {
    "id",
    "clinic_id",
    "kind",
    "subject_key_hash",
    "subject_key_version",
    "patient_reference_hash",
    "patient_id",
    "request_identity_hash",
    "actor_role",
    "actor_reference_hash",
    "policy_version",
    "approval_evidence_hash",
    "scope_hash",
    "state",
    "requested_at",
    "frozen_at",
    "inventory_finalized_at",
    "deleting_at",
    "verifying_at",
    "due_at",
    "completed_at",
    "completion_evidence_hash",
    "target_count",
    "verified_target_count",
    "residual_target_count",
    "created_at",
    "updated_at",
}
_RIGHTS_TARGET_COLUMNS = {
    "id",
    "clinic_id",
    "request_id",
    "system",
    "resource",
    "action",
    "owner_type",
    "owner_id",
    "target_key_hash",
    "prerequisite_target_id",
    "mandatory",
    "state",
    "current_effect_id",
    "attempt_ordinal",
    "available_at",
    "due_at",
    "disposition_code",
    "reason_code",
    "reconciliation_count",
    "last_reconciled_at",
    "verified_at",
    "residual_category",
    "residual_policy_version",
    "residual_approval_evidence_hash",
    "residual_completion_eligible",
    "residual_due_at",
    "locator_cleared_at",
    "created_at",
    "updated_at",
}


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
    """Create the permanent freeze aggregate and minimized target ledger."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for py_enum, name in _ENUMS:
            postgresql.ENUM(
                *[member.value for member in py_enum],
                name=name,
            ).create(bind, checkfirst=True)
        with op.get_context().autocommit_block():
            op.execute(
                sa.text(
                    "ALTER TYPE external_effect_type "
                    "ADD VALUE IF NOT EXISTS 'rights'"
                )
            )

    existing_tables = set(sa.inspect(bind).get_table_names())
    existing_rights_tables = existing_tables & {"rights_request", "rights_target"}
    if existing_rights_tables:
        if existing_rights_tables != {"rights_request", "rights_target"}:
            raise RuntimeError("0019 found a partial rights schema; repair before replay")
        _validate_adopted_schema(bind)
        if bind.dialect.name == "postgresql":
            _apply_policies()
            _apply_postgres_guards()
        else:
            _apply_sqlite_guards()
        return

    op.create_table(
        "rights_request",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "clinic_id",
            sa.String(),
            sa.ForeignKey("clinic.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            _enum(RightsRequestKind, "rights_request_kind"),
            nullable=False,
        ),
        sa.Column("subject_key_hash", sa.String(64), nullable=False),
        sa.Column("subject_key_version", sa.String(64), nullable=False),
        sa.Column("patient_reference_hash", sa.String(64), nullable=False),
        sa.Column("patient_id", sa.String(), nullable=True),
        sa.Column("request_identity_hash", sa.String(64), nullable=False),
        sa.Column("actor_role", sa.String(64), nullable=False),
        sa.Column("actor_reference_hash", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("approval_evidence_hash", sa.String(64), nullable=False),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column(
            "state",
            _enum(RightsRequestState, "rights_request_state"),
            nullable=False,
            server_default=RightsRequestState.REQUESTED.value,
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("inventory_finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleting_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verifying_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completion_evidence_hash", sa.String(64), nullable=True),
        sa.Column("target_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "verified_target_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "residual_target_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patient.clinic_id", "patient.id"],
            ondelete="RESTRICT",
            name="fk_rights_request_patient_tenant",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "id",
            name="uq_rights_request_clinic_id_id",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "kind",
            "scope_hash",
            name="uq_rights_request_convergence",
        ),
        sa.CheckConstraint(
            "length(subject_key_hash) = 64",
            name="ck_rights_request_subject_hash_length",
        ),
        sa.CheckConstraint(
            "length(patient_reference_hash) = 64",
            name="ck_rights_request_patient_reference_hash_length",
        ),
        sa.CheckConstraint(
            "length(request_identity_hash) = 64",
            name="ck_rights_request_identity_hash_length",
        ),
        sa.CheckConstraint(
            "length(actor_reference_hash) = 64",
            name="ck_rights_request_actor_hash_length",
        ),
        sa.CheckConstraint(
            "length(approval_evidence_hash) = 64",
            name="ck_rights_request_approval_hash_length",
        ),
        sa.CheckConstraint(
            "length(scope_hash) = 64",
            name="ck_rights_request_scope_hash_length",
        ),
        sa.CheckConstraint(
            "target_count >= 0 AND verified_target_count >= 0 "
            "AND residual_target_count >= 0",
            name="ck_rights_request_counts_nonnegative",
        ),
    )
    op.create_index("ix_rights_request_clinic_id", "rights_request", ["clinic_id"])
    op.create_index("ix_rights_request_patient_id", "rights_request", ["patient_id"])
    op.create_index(
        "ix_rights_request_clinic_state",
        "rights_request",
        ["clinic_id", "state"],
    )
    op.create_index(
        "ix_rights_request_clinic_subject",
        "rights_request",
        ["clinic_id", "kind", "subject_key_hash"],
    )
    op.create_index(
        "ix_rights_request_clinic_due",
        "rights_request",
        ["clinic_id", "due_at"],
    )

    op.create_table(
        "rights_target",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "clinic_id",
            sa.String(),
            sa.ForeignKey("clinic.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column(
            "system",
            _enum(RightsTargetSystem, "rights_target_system"),
            nullable=False,
        ),
        sa.Column(
            "resource",
            _enum(RightsTargetResource, "rights_target_resource"),
            nullable=False,
        ),
        sa.Column(
            "action",
            _enum(RightsTargetAction, "rights_target_action"),
            nullable=False,
        ),
        sa.Column(
            "owner_type",
            _enum(RightsTargetOwnerType, "rights_target_owner_type"),
            nullable=False,
        ),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("target_key_hash", sa.String(64), nullable=False),
        sa.Column("prerequisite_target_id", sa.String(), nullable=True),
        sa.Column("mandatory", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "state",
            _enum(RightsTargetState, "rights_target_state"),
            nullable=False,
            server_default=RightsTargetState.REQUESTED.value,
        ),
        sa.Column("current_effect_id", sa.String(), nullable=True),
        sa.Column("attempt_ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disposition_code", sa.String(64), nullable=True),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column(
            "reconciliation_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "residual_category",
            _enum(RightsResidualCategory, "rights_residual_category"),
            nullable=True,
        ),
        sa.Column("residual_policy_version", sa.String(128), nullable=True),
        sa.Column("residual_approval_evidence_hash", sa.String(64), nullable=True),
        sa.Column(
            "residual_completion_eligible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("residual_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locator_cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "request_id"],
            ["rights_request.clinic_id", "rights_request.id"],
            ondelete="RESTRICT",
            name="fk_rights_target_request_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "prerequisite_target_id"],
            ["rights_target.clinic_id", "rights_target.id"],
            ondelete="RESTRICT",
            name="fk_rights_target_prerequisite_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "current_effect_id"],
            ["external_effect.clinic_id", "external_effect.id"],
            ondelete="RESTRICT",
            name="fk_rights_target_effect_tenant",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "id",
            name="uq_rights_target_clinic_id_id",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "request_id",
            "target_key_hash",
            name="uq_rights_target_request_key",
        ),
        sa.CheckConstraint(
            "length(target_key_hash) = 64",
            name="ck_rights_target_key_hash_length",
        ),
        sa.CheckConstraint(
            "attempt_ordinal >= 0 AND reconciliation_count >= 0",
            name="ck_rights_target_counts_nonnegative",
        ),
    )
    op.create_index("ix_rights_target_clinic_id", "rights_target", ["clinic_id"])
    op.create_index("ix_rights_target_request_id", "rights_target", ["request_id"])
    op.create_index(
        "ix_rights_target_request_state",
        "rights_target",
        ["clinic_id", "request_id", "state"],
    )
    op.create_index(
        "ix_rights_target_clinic_due",
        "rights_target",
        ["clinic_id", "due_at"],
    )
    op.create_index(
        "ix_rights_target_clinic_owner",
        "rights_target",
        ["clinic_id", "owner_type", "owner_id"],
    )

    if bind.dialect.name == "postgresql":
        _apply_policies()
        _apply_postgres_guards()
    else:
        _apply_sqlite_guards()


def downgrade() -> None:
    """Remove PR-10 schema only when it contains no rights evidence."""
    bind = op.get_bind()
    if _rights_row_count(bind):
        raise RuntimeError(
            "0019 contains rights evidence; rollback by disabling rights dispatch, "
            "not by downgrading the schema"
        )
    _drop_guards(bind)
    op.drop_table("rights_target")
    op.drop_table("rights_request")
    if bind.dialect.name == "postgresql":
        for _, name in reversed(_ENUMS):
            postgresql.ENUM(name=name).drop(bind, checkfirst=True)


def _apply_policies() -> None:
    predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
    for table in ("rights_request", "rights_target"):
        policy = f"{table}_tenant_isolation"
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
        op.execute(
            sa.text(
                f"CREATE POLICY {policy} ON {table} "
                f"USING ({predicate}) WITH CHECK ({predicate})"
            )
        )
    clinic_policy = "clinic_tenant_isolation"
    clinic_predicate = f"id = current_setting('{RLS_GUC}', true)"
    op.execute(sa.text("ALTER TABLE clinic ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE clinic FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS {clinic_policy} ON clinic"))
    op.execute(
        sa.text(
            f"CREATE POLICY {clinic_policy} ON clinic "
            f"USING ({clinic_predicate}) WITH CHECK ({clinic_predicate})"
        )
    )


def _validate_adopted_schema(bind) -> None:
    inspector = sa.inspect(bind)
    actual_request = {
        column["name"] for column in inspector.get_columns("rights_request")
    }
    actual_target = {
        column["name"] for column in inspector.get_columns("rights_target")
    }
    if actual_request != _RIGHTS_REQUEST_COLUMNS:
        raise RuntimeError("0019 cannot adopt an incompatible rights_request table")
    if actual_target != _RIGHTS_TARGET_COLUMNS:
        raise RuntimeError("0019 cannot adopt an incompatible rights_target table")


def _apply_postgres_guards() -> None:
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION rights_request_completion_guard_fn()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.state::text <> 'completed' THEN
                RETURN NEW;
            END IF;
            IF NEW.inventory_finalized_at IS NULL OR NEW.completed_at IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = 'rights_request_completion_guard',
                    MESSAGE = 'rights request completion evidence is incomplete';
            END IF;
            IF EXISTS (
                SELECT 1 FROM rights_target AS target
                WHERE target.clinic_id = NEW.clinic_id
                  AND target.request_id = NEW.id
                  AND target.mandatory
                  AND NOT (
                    target.state::text = 'verified'
                    OR (
                        target.state::text = 'residual'
                        AND target.residual_completion_eligible
                        AND target.residual_category IS NOT NULL
                        AND target.residual_policy_version IS NOT NULL
                        AND target.residual_approval_evidence_hash IS NOT NULL
                        AND target.residual_due_at IS NOT NULL
                        AND target.residual_due_at >= clock_timestamp()
                    )
                  )
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = 'rights_request_completion_guard',
                    MESSAGE = 'mandatory rights targets are incomplete';
            END IF;
            RETURN NEW;
        END;
        $$
    """))
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS rights_request_completion_guard ON rights_request"
    ))
    op.execute(sa.text(
        "CREATE TRIGGER rights_request_completion_guard "
        "BEFORE INSERT OR UPDATE OF state, inventory_finalized_at, completed_at "
        "ON rights_request FOR EACH ROW "
        "EXECUTE FUNCTION rights_request_completion_guard_fn()"
    ))

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION rights_target_prerequisite_guard_fn()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.state::text <> 'dispatching' OR NEW.prerequisite_target_id IS NULL THEN
                RETURN NEW;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM rights_target AS prerequisite
                WHERE prerequisite.clinic_id = NEW.clinic_id
                  AND prerequisite.id = NEW.prerequisite_target_id
                  AND (
                    prerequisite.state::text = 'verified'
                    OR (
                        prerequisite.state::text = 'residual'
                        AND prerequisite.residual_category IS NOT NULL
                        AND prerequisite.residual_completion_eligible
                        AND prerequisite.residual_policy_version IS NOT NULL
                        AND prerequisite.residual_approval_evidence_hash IS NOT NULL
                        AND prerequisite.residual_due_at >= clock_timestamp()
                    )
                  )
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = 'rights_target_prerequisite_guard',
                    MESSAGE = 'rights target prerequisite is incomplete';
            END IF;
            RETURN NEW;
        END;
        $$
    """))
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS rights_target_prerequisite_guard ON rights_target"
    ))
    op.execute(sa.text(
        "CREATE TRIGGER rights_target_prerequisite_guard "
        "BEFORE INSERT OR UPDATE OF state, prerequisite_target_id "
        "ON rights_target FOR EACH ROW "
        "EXECUTE FUNCTION rights_target_prerequisite_guard_fn()"
    ))

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION rights_locator_owner_guard_fn()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            owner_kind text;
            locator_removed boolean;
        BEGIN
            owner_kind := CASE TG_TABLE_NAME
                WHEN 'external_effect' THEN 'external_effect'
                WHEN 'call_record' THEN 'call_record'
                WHEN 'inbound_call' THEN 'inbound_call'
                WHEN 'inbound_message' THEN 'inbound_message'
            END;
            IF TG_OP = 'DELETE' THEN
                locator_removed := true;
            ELSIF TG_TABLE_NAME = 'external_effect' THEN
                locator_removed := OLD.provider_resource_id IS NOT NULL
                    AND NEW.provider_resource_id IS DISTINCT FROM OLD.provider_resource_id;
            ELSIF TG_TABLE_NAME = 'inbound_call' THEN
                locator_removed := OLD.provider_call_id IS NOT NULL
                    AND NEW.provider_call_id IS DISTINCT FROM OLD.provider_call_id;
            ELSIF TG_TABLE_NAME = 'inbound_message' THEN
                locator_removed := OLD.provider_message_id IS NOT NULL
                    AND NEW.provider_message_id IS DISTINCT FROM OLD.provider_message_id;
            ELSE
                locator_removed := (
                    OLD.provider_call_id IS NOT NULL
                    AND NEW.provider_call_id IS DISTINCT FROM OLD.provider_call_id
                ) OR (
                    OLD.recording_sid IS NOT NULL
                    AND NEW.recording_sid IS DISTINCT FROM OLD.recording_sid
                ) OR (
                    OLD.recording_blob_path IS NOT NULL
                    AND NEW.recording_blob_path IS DISTINCT FROM OLD.recording_blob_path
                );
            END IF;
            IF locator_removed AND EXISTS (
                SELECT 1 FROM rights_target AS target
                WHERE target.clinic_id = OLD.clinic_id
                  AND target.owner_type::text = owner_kind
                  AND target.owner_id = OLD.id
                                    AND NOT (
                                        target.state::text = 'verified'
                                        OR (
                                                target.state::text = 'residual'
                                                AND target.residual_category IS NOT NULL
                                                AND target.residual_completion_eligible
                                                AND target.residual_policy_version IS NOT NULL
                                                AND target.residual_approval_evidence_hash IS NOT NULL
                                                AND target.residual_due_at >= clock_timestamp()
                                        )
                                    )
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = 'rights_locator_owner_guard',
                    MESSAGE = 'unresolved rights target still requires owner locator';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$
    """))
    for table, columns in (
        ("external_effect", "provider_resource_id"),
        ("call_record", "provider_call_id, recording_sid, recording_blob_path"),
        ("inbound_call", "provider_call_id"),
        ("inbound_message", "provider_message_id"),
    ):
        trigger = f"rights_locator_owner_guard_{table}"
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON {table}"))
        op.execute(sa.text(
            f"CREATE TRIGGER {trigger} BEFORE DELETE OR UPDATE OF {columns} "
            f"ON {table} FOR EACH ROW EXECUTE FUNCTION rights_locator_owner_guard_fn()"
        ))


def _apply_sqlite_guards() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS rights_request_completion_guard"))
    op.execute(sa.text("""
        CREATE TRIGGER rights_request_completion_guard
        BEFORE UPDATE OF state, inventory_finalized_at, completed_at ON rights_request
        FOR EACH ROW
        WHEN NEW.state = 'completed' AND (
            NEW.inventory_finalized_at IS NULL OR NEW.completed_at IS NULL OR EXISTS (
                SELECT 1 FROM rights_target AS target
                WHERE target.clinic_id = NEW.clinic_id
                  AND target.request_id = NEW.id
                  AND target.mandatory = 1
                  AND NOT (
                    target.state = 'verified'
                    OR (
                        target.state = 'residual'
                        AND target.residual_completion_eligible = 1
                        AND target.residual_category IS NOT NULL
                        AND target.residual_policy_version IS NOT NULL
                        AND target.residual_approval_evidence_hash IS NOT NULL
                        AND target.residual_due_at IS NOT NULL
                        AND target.residual_due_at >= CURRENT_TIMESTAMP
                    )
                  )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'rights_request_completion_guard');
        END
    """))
    op.execute(sa.text("DROP TRIGGER IF EXISTS rights_target_prerequisite_guard"))
    op.execute(sa.text("""
        CREATE TRIGGER rights_target_prerequisite_guard
        BEFORE UPDATE OF state, prerequisite_target_id ON rights_target
        FOR EACH ROW
        WHEN NEW.state = 'dispatching' AND NEW.prerequisite_target_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM rights_target AS prerequisite
            WHERE prerequisite.clinic_id = NEW.clinic_id
              AND prerequisite.id = NEW.prerequisite_target_id
                            AND (
                                prerequisite.state = 'verified'
                                OR (
                                    prerequisite.state = 'residual'
                                    AND prerequisite.residual_category IS NOT NULL
                                    AND prerequisite.residual_completion_eligible = 1
                                    AND prerequisite.residual_policy_version IS NOT NULL
                                    AND prerequisite.residual_approval_evidence_hash IS NOT NULL
                                    AND prerequisite.residual_due_at IS NOT NULL
                                    AND prerequisite.residual_due_at >= CURRENT_TIMESTAMP
                                )
                            )
          )
        BEGIN
            SELECT RAISE(ABORT, 'rights_target_prerequisite_guard');
        END
    """))
    _apply_sqlite_locator_guard(
        "external_effect",
        "external_effect",
        "OLD.provider_resource_id IS NOT NULL "
        "AND NEW.provider_resource_id IS NOT OLD.provider_resource_id",
    )
    _apply_sqlite_locator_guard(
        "call_record",
        "call_record",
        "(OLD.provider_call_id IS NOT NULL "
        "AND NEW.provider_call_id IS NOT OLD.provider_call_id) OR "
        "(OLD.recording_sid IS NOT NULL AND NEW.recording_sid IS NOT OLD.recording_sid) OR "
        "(OLD.recording_blob_path IS NOT NULL "
        "AND NEW.recording_blob_path IS NOT OLD.recording_blob_path)",
    )
    _apply_sqlite_locator_guard(
        "inbound_call",
        "inbound_call",
        "OLD.provider_call_id IS NOT NULL "
        "AND NEW.provider_call_id IS NOT OLD.provider_call_id",
    )
    _apply_sqlite_locator_guard(
        "inbound_message",
        "inbound_message",
        "OLD.provider_message_id IS NOT NULL "
        "AND NEW.provider_message_id IS NOT OLD.provider_message_id",
    )


def _apply_sqlite_locator_guard(table: str, owner_type: str, changed: str) -> None:
    update_trigger = f"rights_locator_owner_guard_{table}_update"
    delete_trigger = f"rights_locator_owner_guard_{table}_delete"
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {update_trigger}"))
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {delete_trigger}"))
    unresolved = (
        "EXISTS (SELECT 1 FROM rights_target AS target "
        "WHERE target.clinic_id = OLD.clinic_id "
        f"AND target.owner_type = '{owner_type}' "
        "AND target.owner_id = OLD.id "
        "AND NOT (target.state = 'verified' OR ("
        "target.state = 'residual' "
        "AND target.residual_category IS NOT NULL "
        "AND target.residual_completion_eligible = 1 "
        "AND target.residual_policy_version IS NOT NULL "
        "AND target.residual_approval_evidence_hash IS NOT NULL "
        "AND target.residual_due_at IS NOT NULL "
        "AND target.residual_due_at >= CURRENT_TIMESTAMP)))"
    )
    op.execute(sa.text(
        f"CREATE TRIGGER {update_trigger} BEFORE UPDATE ON {table} FOR EACH ROW "
        f"WHEN ({changed}) AND {unresolved} "
        "BEGIN SELECT RAISE(ABORT, 'rights_locator_owner_guard'); END"
    ))
    op.execute(sa.text(
        f"CREATE TRIGGER {delete_trigger} BEFORE DELETE ON {table} FOR EACH ROW "
        f"WHEN {unresolved} "
        "BEGIN SELECT RAISE(ABORT, 'rights_locator_owner_guard'); END"
    ))


def _drop_guards(bind) -> None:
    if bind.dialect.name == "postgresql":
        for table in (
            "external_effect",
            "call_record",
            "inbound_call",
            "inbound_message",
        ):
            op.execute(sa.text(
                f"DROP TRIGGER IF EXISTS rights_locator_owner_guard_{table} ON {table}"
            ))
        op.execute(sa.text(
            "DROP TRIGGER IF EXISTS rights_target_prerequisite_guard ON rights_target"
        ))
        op.execute(sa.text(
            "DROP TRIGGER IF EXISTS rights_request_completion_guard ON rights_request"
        ))
        op.execute(sa.text("DROP FUNCTION IF EXISTS rights_locator_owner_guard_fn()"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS rights_target_prerequisite_guard_fn()"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS rights_request_completion_guard_fn()"))
        op.execute(sa.text("DROP POLICY IF EXISTS clinic_tenant_isolation ON clinic"))
        op.execute(sa.text("ALTER TABLE clinic DISABLE ROW LEVEL SECURITY"))
        return
    for trigger in (
        "rights_locator_owner_guard_external_effect_update",
        "rights_locator_owner_guard_external_effect_delete",
        "rights_locator_owner_guard_call_record_update",
        "rights_locator_owner_guard_call_record_delete",
        "rights_locator_owner_guard_inbound_call_update",
        "rights_locator_owner_guard_inbound_call_delete",
        "rights_locator_owner_guard_inbound_message_update",
        "rights_locator_owner_guard_inbound_message_delete",
        "rights_target_prerequisite_guard",
        "rights_request_completion_guard",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger}"))


def _rights_row_count(bind) -> int:
    if bind.dialect.name != "postgresql":
        requests = int(bind.scalar(sa.text("SELECT count(*) FROM rights_request")) or 0)
        targets = int(bind.scalar(sa.text("SELECT count(*) FROM rights_target")) or 0)
        return requests + targets

    bind.execute(sa.text("ALTER TABLE clinic NO FORCE ROW LEVEL SECURITY"))
    total = 0
    clinic_ids = list(bind.execute(sa.text("SELECT id FROM clinic ORDER BY id")).scalars())
    for clinic_id in clinic_ids:
        bind.execute(
            sa.text("SELECT set_config(:setting, :clinic_id, true)"),
            {"setting": RLS_GUC, "clinic_id": clinic_id},
        )
        total += int(
            bind.scalar(
                sa.text(
                    "SELECT (SELECT count(*) FROM rights_request "
                    "WHERE clinic_id = :clinic_id) + "
                    "(SELECT count(*) FROM rights_target WHERE clinic_id = :clinic_id)"
                ),
                {"clinic_id": clinic_id},
            )
            or 0
        )
    bind.execute(
        sa.text("SELECT set_config(:setting, '', true)"),
        {"setting": RLS_GUC},
    )
    return total