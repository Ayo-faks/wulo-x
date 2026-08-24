"""expand call records into the per-call recording-consent ledger

Revision ID: 0018_recording_consent_ledger
Revises: 0017_pilot_programme_controls
Create Date: 2026-07-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from src.clinic_recall.enums import (
    CallRecordingStatus,
    RecordingConsentSource,
    RecordingConsentState,
    RecordingDeletionState,
)
from src.clinic_recall.models import RLS_GUC

revision: str = "0018_recording_consent_ledger"
down_revision: str | None = "0017_pilot_programme_controls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
    """Add per-call consent/provider evidence without claiming legacy consent."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(
            *[member.value for member in RecordingConsentState],
            name="recording_consent_state",
        ).create(bind, checkfirst=True)
        postgresql.ENUM(
            *[member.value for member in RecordingConsentSource],
            name="recording_consent_source",
        ).create(bind, checkfirst=True)
        postgresql.ENUM(
            *[member.value for member in RecordingDeletionState],
            name="recording_deletion_state",
        ).create(bind, checkfirst=True)
        with op.get_context().autocommit_block():
            for status in CallRecordingStatus:
                op.execute(
                    sa.text(
                        "ALTER TYPE call_recording_status "
                        f"ADD VALUE IF NOT EXISTS '{status.value}'"
                    )
                )

    inbound_uniques = {
        constraint["name"] for constraint in sa.inspect(bind).get_unique_constraints("inbound_call")
    }
    if "uq_inbound_call_clinic_id_id" not in inbound_uniques:
        with op.batch_alter_table("inbound_call") as batch_op:
            batch_op.create_unique_constraint(
                "uq_inbound_call_clinic_id_id",
                ["clinic_id", "id"],
            )
    if "uq_inbound_call_clinic_id_provider" not in inbound_uniques:
        with op.batch_alter_table("inbound_call") as batch_op:
            batch_op.create_unique_constraint(
                "uq_inbound_call_clinic_id_provider",
                ["clinic_id", "id", "provider"],
            )

    call_columns = {column["name"] for column in sa.inspect(bind).get_columns("call_record")}
    inspector = sa.inspect(bind)
    foreign_key_details = inspector.get_foreign_keys("call_record")
    foreign_keys = {constraint["name"] for constraint in foreign_key_details}
    legacy_patient_foreign_keys = [
        constraint["name"]
        for constraint in foreign_key_details
        if constraint.get("constrained_columns") == ["patient_id"]
        and constraint.get("referred_table") == "patient"
        and constraint.get("name")
    ]
    unique_constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("call_record")
    }
    check_constraints = {
        constraint["name"] for constraint in inspector.get_check_constraints("call_record")
    }
    with op.batch_alter_table("call_record") as batch_op:
        batch_op.alter_column(
            "provider_call_id",
            existing_type=sa.String(),
            nullable=True,
        )
        if "external_effect_id" not in call_columns:
            batch_op.add_column(sa.Column("external_effect_id", sa.String(), nullable=True))
        if "inbound_call_id" not in call_columns:
            batch_op.add_column(sa.Column("inbound_call_id", sa.String(), nullable=True))
        if "consent_state" not in call_columns:
            batch_op.add_column(
                sa.Column(
                    "consent_state",
                    _enum(RecordingConsentState, "recording_consent_state"),
                    nullable=True,
                )
            )
        if "consent_asked_at" not in call_columns:
            batch_op.add_column(
                sa.Column("consent_asked_at", sa.DateTime(timezone=True), nullable=True)
            )
        if "consent_decided_at" not in call_columns:
            batch_op.add_column(
                sa.Column("consent_decided_at", sa.DateTime(timezone=True), nullable=True)
            )
        if "consent_decision_source" not in call_columns:
            batch_op.add_column(
                sa.Column(
                    "consent_decision_source",
                    _enum(RecordingConsentSource, "recording_consent_source"),
                    nullable=True,
                )
            )
        if "consent_version" not in call_columns:
            batch_op.add_column(sa.Column("consent_version", sa.String(64), nullable=True))
        for column_name in (
            "recording_requested_at",
            "recording_started_at",
            "recording_stop_requested_at",
            "recording_stopped_at",
        ):
            if column_name not in call_columns:
                batch_op.add_column(
                    sa.Column(column_name, sa.DateTime(timezone=True), nullable=True)
                )
        if "deletion_state" not in call_columns:
            batch_op.add_column(
                sa.Column(
                    "deletion_state",
                    _enum(RecordingDeletionState, "recording_deletion_state"),
                    nullable=True,
                )
            )

    # Every row present while Alembic still reports 0017 is historical. This
    # must run after the first batch has physically created the new columns.
    _backfill_legacy_rows(bind, tagged_only=False)

    with op.batch_alter_table("call_record") as batch_op:
        batch_op.alter_column(
            "consent_state",
            existing_type=_enum(RecordingConsentState, "recording_consent_state"),
            nullable=False,
            server_default=RecordingConsentState.NOT_ASKED.value,
        )
        batch_op.alter_column(
            "deletion_state",
            existing_type=_enum(RecordingDeletionState, "recording_deletion_state"),
            nullable=False,
            server_default=RecordingDeletionState.NOT_REQUESTED.value,
        )
        if "fk_call_record_external_effect_tenant" not in foreign_keys:
            batch_op.create_foreign_key(
                "fk_call_record_external_effect_tenant",
                "external_effect",
                ["clinic_id", "external_effect_id"],
                ["clinic_id", "id"],
                ondelete="RESTRICT",
            )
        if "fk_call_record_inbound_call_tenant" not in foreign_keys:
            batch_op.create_foreign_key(
                "fk_call_record_inbound_call_tenant",
                "inbound_call",
                ["clinic_id", "inbound_call_id"],
                ["clinic_id", "id"],
                ondelete="RESTRICT",
            )
        if "fk_call_record_inbound_provider_tenant" not in foreign_keys:
            batch_op.create_foreign_key(
                "fk_call_record_inbound_provider_tenant",
                "inbound_call",
                ["clinic_id", "inbound_call_id", "provider"],
                ["clinic_id", "id", "provider"],
                ondelete="RESTRICT",
            )
        for constraint_name in legacy_patient_foreign_keys:
            batch_op.drop_constraint(constraint_name, type_="foreignkey")
        if "fk_call_record_patient_tenant" not in foreign_keys:
            batch_op.create_foreign_key(
                "fk_call_record_patient_tenant",
                "patient",
                ["clinic_id", "patient_id"],
                ["clinic_id", "id"],
                ondelete="RESTRICT",
            )
        if "uq_call_record_external_effect" not in unique_constraints:
            batch_op.create_unique_constraint(
                "uq_call_record_external_effect",
                ["clinic_id", "external_effect_id"],
            )
        if "uq_call_record_inbound_call" not in unique_constraints:
            batch_op.create_unique_constraint(
                "uq_call_record_inbound_call",
                ["clinic_id", "inbound_call_id"],
            )
        if "ck_call_record_not_both_internal_anchors" not in check_constraints:
            batch_op.create_check_constraint(
                "ck_call_record_not_both_internal_anchors",
                "NOT (external_effect_id IS NOT NULL AND inbound_call_id IS NOT NULL)",
            )
        if "ck_call_record_has_trusted_anchor" not in check_constraints:
            batch_op.create_check_constraint(
                "ck_call_record_has_trusted_anchor",
                "provider_call_id IS NOT NULL OR external_effect_id IS NOT NULL "
                "OR inbound_call_id IS NOT NULL",
            )

    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_call_record_external_effect_id "
            "ON call_record (external_effect_id)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_call_record_inbound_call_id "
            "ON call_record (inbound_call_id)"
        )
    )
    # SQLite's final batch rebuild applies the new server default to a legacy
    # nullable enum value. Every row present during this migration is historical
    # Phase-A data, so classify it truthfully after the final rebuild as well.
    _backfill_legacy_rows(bind, tagged_only=True)
    _apply_inbound_identity_guard(bind)

    if bind.dialect.name == "postgresql":
        _apply_policy()


def downgrade() -> None:
    """Remove PR-09 fields only when every row is a historical legacy row."""
    bind = op.get_bind()
    if _unsafe_downgrade_row_count(bind):
        raise RuntimeError(
            "0018 contains PR-09 call rows; rollback by disabling recording, "
            "not by downgrading the schema"
        )
    _drop_inbound_identity_guard(bind)
    has_legacy_patient_foreign_key = any(
        constraint.get("constrained_columns") == ["patient_id"]
        and constraint.get("referred_table") == "patient"
        for constraint in sa.inspect(bind).get_foreign_keys("call_record")
    )
    op.execute(sa.text("DROP INDEX IF EXISTS ix_call_record_inbound_call_id"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_call_record_external_effect_id"))
    with op.batch_alter_table("call_record") as batch_op:
        batch_op.drop_constraint("ck_call_record_has_trusted_anchor", type_="check")
        batch_op.drop_constraint(
            "ck_call_record_not_both_internal_anchors",
            type_="check",
        )
        batch_op.drop_constraint("uq_call_record_inbound_call", type_="unique")
        batch_op.drop_constraint("uq_call_record_external_effect", type_="unique")
        batch_op.drop_constraint("fk_call_record_inbound_call_tenant", type_="foreignkey")
        batch_op.drop_constraint(
            "fk_call_record_inbound_provider_tenant",
            type_="foreignkey",
        )
        batch_op.drop_constraint("fk_call_record_external_effect_tenant", type_="foreignkey")
        batch_op.drop_constraint("fk_call_record_patient_tenant", type_="foreignkey")
        if not has_legacy_patient_foreign_key:
            batch_op.create_foreign_key(
                "fk_call_record_patient",
                "patient",
                ["patient_id"],
                ["id"],
                ondelete="SET NULL",
            )
        for column_name in (
            "deletion_state",
            "recording_stopped_at",
            "recording_stop_requested_at",
            "recording_started_at",
            "recording_requested_at",
            "consent_version",
            "consent_decision_source",
            "consent_decided_at",
            "consent_asked_at",
            "consent_state",
            "inbound_call_id",
            "external_effect_id",
        ):
            batch_op.drop_column(column_name)
        batch_op.alter_column(
            "provider_call_id",
            existing_type=sa.String(),
            nullable=False,
        )
    with op.batch_alter_table("inbound_call") as batch_op:
        batch_op.drop_constraint("uq_inbound_call_clinic_id_provider", type_="unique")
        batch_op.drop_constraint("uq_inbound_call_clinic_id_id", type_="unique")
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(name="recording_deletion_state").drop(bind, checkfirst=True)
        postgresql.ENUM(name="recording_consent_source").drop(bind, checkfirst=True)
        postgresql.ENUM(name="recording_consent_state").drop(bind, checkfirst=True)


def _apply_policy() -> None:
    predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
    op.execute(sa.text("ALTER TABLE call_record ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE call_record FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("DROP POLICY IF EXISTS call_record_tenant_isolation ON call_record"))
    op.execute(
        sa.text(
            "CREATE POLICY call_record_tenant_isolation ON call_record "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )


def _apply_inbound_identity_guard(bind) -> None:
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("""
                CREATE OR REPLACE FUNCTION call_record_inbound_identity_guard()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $$
                DECLARE
                    anchor_provider clinic_phone_provider;
                    anchor_provider_call_id text;
                BEGIN
                    IF NEW.inbound_call_id IS NULL THEN
                        RETURN NEW;
                    END IF;
                    SELECT provider, provider_call_id
                    INTO anchor_provider, anchor_provider_call_id
                    FROM inbound_call
                    WHERE clinic_id = NEW.clinic_id AND id = NEW.inbound_call_id;
                    IF NOT FOUND THEN
                        RETURN NEW;
                    END IF;
                    IF anchor_provider IS DISTINCT FROM NEW.provider THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '23514',
                            CONSTRAINT = 'ck_call_record_inbound_provider_identity',
                            MESSAGE = 'call record inbound provider identity conflict';
                    END IF;
                    IF NEW.provider_call_id IS NOT NULL
                       AND anchor_provider_call_id IS DISTINCT FROM NEW.provider_call_id THEN
                        IF TG_OP = 'UPDATE'
                           AND NEW.provider::text = 'acs'
                           AND OLD.provider_call_id IS NULL
                           AND OLD.inbound_call_id IS NOT DISTINCT FROM NEW.inbound_call_id
                           AND OLD.provider IS NOT DISTINCT FROM NEW.provider THEN
                            RETURN NEW;
                        END IF;
                        RAISE EXCEPTION USING
                            ERRCODE = '23514',
                            CONSTRAINT = 'ck_call_record_inbound_provider_call_identity',
                            MESSAGE = 'call record inbound provider call identity conflict';
                    END IF;
                    RETURN NEW;
                END;
                $$
                """))
        op.execute(sa.text("DROP TRIGGER IF EXISTS call_record_inbound_identity ON call_record"))
        op.execute(
            sa.text(
                "CREATE TRIGGER call_record_inbound_identity "
                "BEFORE INSERT OR UPDATE OF clinic_id, inbound_call_id, provider, provider_call_id "
                "ON call_record FOR EACH ROW "
                "EXECUTE FUNCTION call_record_inbound_identity_guard()"
            )
        )
        return

    op.execute(sa.text("DROP TRIGGER IF EXISTS call_record_inbound_identity_insert"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS call_record_inbound_identity_update"))
    mismatch = (
        "EXISTS (SELECT 1 FROM inbound_call AS anchor "
        "WHERE anchor.clinic_id = NEW.clinic_id AND anchor.id = NEW.inbound_call_id "
        "AND (anchor.provider <> NEW.provider OR "
        "(NEW.provider_call_id IS NOT NULL "
        "AND anchor.provider_call_id <> NEW.provider_call_id)))"
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER call_record_inbound_identity_insert "
            "BEFORE INSERT ON call_record FOR EACH ROW "
            f"WHEN NEW.inbound_call_id IS NOT NULL AND {mismatch} "
            "BEGIN SELECT RAISE(ABORT, 'call record inbound identity conflict'); END"
        )
    )
    update_mismatch = (
        "EXISTS (SELECT 1 FROM inbound_call AS anchor "
        "WHERE anchor.clinic_id = NEW.clinic_id AND anchor.id = NEW.inbound_call_id "
        "AND (anchor.provider <> NEW.provider OR "
        "(NEW.provider_call_id IS NOT NULL "
        "AND anchor.provider_call_id <> NEW.provider_call_id "
        "AND NOT (NEW.provider = 'acs' AND OLD.provider_call_id IS NULL "
        "AND OLD.inbound_call_id IS NEW.inbound_call_id "
        "AND OLD.provider IS NEW.provider))))"
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER call_record_inbound_identity_update "
            "BEFORE UPDATE OF clinic_id, inbound_call_id, provider, provider_call_id "
            "ON call_record FOR EACH ROW "
            f"WHEN NEW.inbound_call_id IS NOT NULL AND {update_mismatch} "
            "BEGIN SELECT RAISE(ABORT, 'call record inbound identity conflict'); END"
        )
    )


def _drop_inbound_identity_guard(bind) -> None:
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP TRIGGER IF EXISTS call_record_inbound_identity ON call_record"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS call_record_inbound_identity_guard()"))
        return
    op.execute(sa.text("DROP TRIGGER IF EXISTS call_record_inbound_identity_insert"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS call_record_inbound_identity_update"))


def _backfill_legacy_rows(bind, *, tagged_only: bool) -> None:
    condition = (
        " AND consent_version = 'legacy-stored-consent-v0' "
        "AND consent_decision_source = 'policy'"
        if tagged_only
        else (
            " AND external_effect_id IS NULL AND inbound_call_id IS NULL "
            "AND consent_version IS NULL AND consent_decision_source IS NULL "
            "AND consent_asked_at IS NULL AND consent_decided_at IS NULL"
        )
    )
    update = (
        "UPDATE call_record SET "
        "consent_state = 'ambiguous', "
        "consent_decision_source = 'policy', "
        "consent_version = 'legacy-stored-consent-v0', "
        "deletion_state = 'not_requested'"
    )
    if bind.dialect.name != "postgresql":
        where = condition.removeprefix(" AND ")
        op.execute(sa.text(f"{update}{' WHERE ' + where if where else ''}"))
        return

    clinic_ids = list(bind.execute(sa.text("SELECT id FROM clinic ORDER BY id")).scalars())
    for clinic_id in clinic_ids:
        bind.execute(
            sa.text("SELECT set_config(:setting, :clinic_id, true)"),
            {"setting": RLS_GUC, "clinic_id": clinic_id},
        )
        bind.execute(
            sa.text(f"{update} WHERE clinic_id = :clinic_id{condition}"),
            {"clinic_id": clinic_id},
        )
    bind.execute(
        sa.text("SELECT set_config(:setting, '', true)"),
        {"setting": RLS_GUC},
    )


def _unsafe_downgrade_row_count(bind) -> int:
    predicate = (
        "provider_call_id IS NULL OR external_effect_id IS NOT NULL "
        "OR inbound_call_id IS NOT NULL OR consent_state <> 'ambiguous' "
        "OR COALESCE(consent_decision_source::text, '') <> 'policy' "
        "OR COALESCE(consent_version, '') <> 'legacy-stored-consent-v0'"
    )
    if bind.dialect.name != "postgresql":
        sqlite_predicate = predicate.replace(
            "consent_decision_source::text", "consent_decision_source"
        )
        return int(
            bind.scalar(sa.text(f"SELECT count(*) FROM call_record WHERE {sqlite_predicate}")) or 0
        )

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
                    f"SELECT count(*) FROM call_record WHERE clinic_id = :clinic_id "
                    f"AND ({predicate})"
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
