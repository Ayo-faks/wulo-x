"""add pilot programme and cumulative cohort controls

Revision ID: 0017_pilot_programme_controls
Revises: 0016_scheduled_cadence
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from src.clinic_recall.enums import PilotProgrammeState
from src.clinic_recall.models import RLS_GUC

revision: str = "0017_pilot_programme_controls"
down_revision: str | None = "0016_scheduled_cadence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _programme_state_enum() -> sa.Enum:
    values = [member.value for member in PilotProgrammeState]
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.ENUM(
            *values,
            name="pilot_programme_state",
            create_type=False,
        )
    return sa.Enum(
        PilotProgrammeState,
        name="pilot_programme_state",
        native_enum=True,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


def upgrade() -> None:
    """Add the bounded tenant programme and erasure-safe participant ledger."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(
            *[member.value for member in PilotProgrammeState],
            name="pilot_programme_state",
        ).create(bind, checkfirst=True)

    tables = set(sa.inspect(bind).get_table_names())
    _ensure_patient_tenant_key()
    if "pilot_programme" not in tables:
        _create_pilot_programme()
    if "pilot_participant" not in tables:
        _create_pilot_participant()
    _ensure_indexes()

    if bind.dialect.name == "postgresql":
        _apply_policy("pilot_programme", "pilot_programme_tenant_isolation")
        _apply_policy("pilot_participant", "pilot_participant_tenant_isolation")
        _install_invariant_triggers()


def downgrade() -> None:
    """Remove local PR-13 controls while retaining scheduled cadence."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text("DROP TRIGGER IF EXISTS pilot_programme_transition_guard ON pilot_programme")
        )
        op.execute(
            sa.text("DROP TRIGGER IF EXISTS pilot_participant_patient_erasure " "ON patient")
        )
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS pilot_participant_identity_guard " "ON pilot_participant"
            )
        )
        op.execute(
            sa.text("DROP TRIGGER IF EXISTS pilot_participant_insert_guard " "ON pilot_participant")
        )
        op.execute(
            sa.text("DROP FUNCTION IF EXISTS " "anonymize_pilot_participant_on_patient_delete()")
        )
        op.execute(sa.text("DROP FUNCTION IF EXISTS protect_pilot_programme()"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS protect_pilot_participant_identity()"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS validate_pilot_participant_insert()"))
    tables = set(sa.inspect(bind).get_table_names())
    if "pilot_participant" in tables:
        op.drop_table("pilot_participant")
    if "pilot_programme" in tables:
        op.drop_table("pilot_programme")
    _drop_patient_tenant_key()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(name="pilot_programme_state").drop(bind, checkfirst=True)


def _create_pilot_programme() -> None:
    op.create_table(
        "pilot_programme",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("clinic_id", sa.String(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("release_identity", sa.String(length=200), nullable=False),
        sa.Column(
            "state",
            _programme_state_enum(),
            server_default=PilotProgrammeState.DRAFT.value,
            nullable=False,
        ),
        sa.Column(
            "maximum_unique_patients",
            sa.Integer(),
            server_default="50",
            nullable=False,
        ),
        sa.Column(
            "active_cumulative_limit",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_by", sa.String(length=200), nullable=True),
        sa.Column("release_evidence_hash", sa.String(length=64), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_by", sa.String(length=200), nullable=True),
        sa.Column("pause_reason", sa.String(length=64), nullable=True),
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
            "length(environment) BETWEEN 1 AND 32",
            name="ck_pilot_programme_environment_length",
        ),
        sa.CheckConstraint(
            "length(release_identity) BETWEEN 1 AND 200",
            name="ck_pilot_programme_release_identity_length",
        ),
        sa.CheckConstraint(
            "maximum_unique_patients = 50",
            name="ck_pilot_programme_maximum_unique_patients",
        ),
        sa.CheckConstraint(
            "active_cumulative_limit IN (0, 5, 15, 30, 50)",
            name="ck_pilot_programme_cumulative_limit",
        ),
        sa.CheckConstraint(
            "release_evidence_hash IS NULL OR length(release_evidence_hash) = 64",
            name="ck_pilot_programme_release_evidence_hash",
        ),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "clinic_id",
            "environment",
            "release_identity",
            name="uq_pilot_programme_release",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "id",
            name="uq_pilot_programme_clinic_id_id",
        ),
    )


def _create_pilot_participant() -> None:
    op.create_table(
        "pilot_participant",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("clinic_id", sa.String(), nullable=False),
        sa.Column("pilot_programme_id", sa.String(), nullable=False),
        sa.Column("patient_id", sa.String(), nullable=True),
        sa.Column("patient_key_hash", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("wave", sa.Integer(), nullable=False),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_contact_at", sa.DateTime(timezone=True), nullable=True),
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
            "ordinal BETWEEN 1 AND 50",
            name="ck_pilot_participant_ordinal",
        ),
        sa.CheckConstraint(
            "wave BETWEEN 1 AND 4",
            name="ck_pilot_participant_wave",
        ),
        sa.CheckConstraint(
            "(wave = 1 AND ordinal BETWEEN 1 AND 5) OR "
            "(wave = 2 AND ordinal BETWEEN 6 AND 15) OR "
            "(wave = 3 AND ordinal BETWEEN 16 AND 30) OR "
            "(wave = 4 AND ordinal BETWEEN 31 AND 50)",
            name="ck_pilot_participant_wave_ordinal",
        ),
        sa.CheckConstraint(
            "length(patient_key_hash) = 64",
            name="ck_pilot_participant_patient_key_hash",
        ),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["clinic_id", "pilot_programme_id"],
            ["pilot_programme.clinic_id", "pilot_programme.id"],
            ondelete="RESTRICT",
            name="fk_pilot_participant_programme_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patient.clinic_id", "patient.id"],
            ondelete="RESTRICT",
            name="fk_pilot_participant_patient_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "clinic_id",
            "pilot_programme_id",
            "patient_key_hash",
            name="uq_pilot_participant_patient",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "pilot_programme_id",
            "patient_id",
            name="uq_pilot_participant_patient_reference",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "pilot_programme_id",
            "ordinal",
            name="uq_pilot_participant_ordinal",
        ),
    )


def _ensure_patient_tenant_key() -> None:
    bind = op.get_bind()
    constraints = sa.inspect(bind).get_unique_constraints("patient")
    if any(constraint.get("column_names") == ["clinic_id", "id"] for constraint in constraints):
        return
    with op.batch_alter_table("patient") as batch:
        batch.create_unique_constraint(
            "uq_patient_clinic_id_id",
            ["clinic_id", "id"],
        )


def _drop_patient_tenant_key() -> None:
    bind = op.get_bind()
    constraints = sa.inspect(bind).get_unique_constraints("patient")
    if not any(constraint.get("name") == "uq_patient_clinic_id_id" for constraint in constraints):
        return
    with op.batch_alter_table("patient") as batch:
        batch.drop_constraint("uq_patient_clinic_id_id", type_="unique")


def _ensure_indexes() -> None:
    required = {
        "pilot_programme": {
            "ix_pilot_programme_clinic_id": ["clinic_id"],
            "ix_pilot_programme_clinic_state": ["clinic_id", "state"],
        },
        "pilot_participant": {
            "ix_pilot_participant_clinic_id": ["clinic_id"],
            "ix_pilot_participant_patient_id": ["patient_id"],
            "ix_pilot_participant_clinic_programme_ordinal": [
                "clinic_id",
                "pilot_programme_id",
                "ordinal",
            ],
        },
    }
    bind = op.get_bind()
    for table_name, indexes in required.items():
        existing = {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}
        for name, columns in indexes.items():
            if name not in existing:
                op.create_index(name, table_name, columns)


def _apply_policy(table_name: str, policy: str) -> None:
    predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
    op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {table_name}"))
    op.execute(
        sa.text(
            f"CREATE POLICY {policy} ON {table_name} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )


def _install_invariant_triggers() -> None:
    op.execute(
        sa.text("DROP TRIGGER IF EXISTS pilot_programme_transition_guard ON pilot_programme")
    )
    op.execute(
        sa.text("DROP TRIGGER IF EXISTS pilot_participant_insert_guard ON pilot_participant")
    )
    op.execute(
        sa.text("DROP TRIGGER IF EXISTS pilot_participant_identity_guard ON pilot_participant")
    )
    op.execute(sa.text("DROP TRIGGER IF EXISTS pilot_participant_patient_erasure ON patient"))
    op.execute(sa.text("""
            CREATE OR REPLACE FUNCTION protect_pilot_programme()
            RETURNS trigger AS $$
            DECLARE
                expected_limit integer;
                released_count integer;
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'pilot programme rows are retained';
                END IF;
                IF NEW.clinic_id IS DISTINCT FROM OLD.clinic_id
                   OR NEW.environment IS DISTINCT FROM OLD.environment
                   OR NEW.release_identity IS DISTINCT FROM OLD.release_identity
                   OR NEW.maximum_unique_patients IS DISTINCT FROM OLD.maximum_unique_patients THEN
                    RAISE EXCEPTION 'pilot programme identity is immutable';
                END IF;
                IF NEW.active_cumulative_limit < OLD.active_cumulative_limit THEN
                    RAISE EXCEPTION 'pilot cumulative limit cannot decrease';
                END IF;
                IF NEW.active_cumulative_limit = OLD.active_cumulative_limit
                   AND (NEW.released_at IS DISTINCT FROM OLD.released_at
                        OR NEW.released_by IS DISTINCT FROM OLD.released_by
                        OR NEW.release_evidence_hash IS DISTINCT FROM OLD.release_evidence_hash) THEN
                    RAISE EXCEPTION 'pilot release evidence changes only with a wave';
                END IF;
                IF (NEW.paused_at IS DISTINCT FROM OLD.paused_at
                    OR NEW.paused_by IS DISTINCT FROM OLD.paused_by
                    OR NEW.pause_reason IS DISTINCT FROM OLD.pause_reason)
                   AND NOT (OLD.state <> 'paused' AND NEW.state = 'paused') THEN
                    RAISE EXCEPTION 'pilot pause evidence changes only on pause';
                END IF;
                IF OLD.state = 'closed' AND NEW.state <> 'closed' THEN
                    RAISE EXCEPTION 'closed pilot programme is terminal';
                END IF;
                IF OLD.state = 'paused' AND NEW.state NOT IN ('paused', 'closed') THEN
                    RAISE EXCEPTION 'paused pilot programme cannot reactivate';
                END IF;
                IF OLD.state = 'active'
                   AND NEW.state NOT IN ('active', 'paused', 'closed') THEN
                    RAISE EXCEPTION 'active pilot programme cannot move backwards';
                END IF;
                IF OLD.state = 'dark'
                   AND NEW.state NOT IN ('dark', 'active', 'paused', 'closed') THEN
                    RAISE EXCEPTION 'dark pilot programme cannot return to draft';
                END IF;
                IF NEW.active_cumulative_limit <> OLD.active_cumulative_limit THEN
                    IF OLD.active_cumulative_limit = 0 AND OLD.state <> 'dark' THEN
                        RAISE EXCEPTION 'Wave 1 requires dark qualification';
                    END IF;
                    IF OLD.active_cumulative_limit > 0 AND OLD.state <> 'active' THEN
                        RAISE EXCEPTION 'later waves require an active programme';
                    END IF;
                    expected_limit := CASE OLD.active_cumulative_limit
                        WHEN 0 THEN 5
                        WHEN 5 THEN 15
                        WHEN 15 THEN 30
                        WHEN 30 THEN 50
                        ELSE NULL
                    END;
                    IF expected_limit IS NULL
                       OR NEW.active_cumulative_limit <> expected_limit THEN
                        RAISE EXCEPTION 'pilot cumulative limit must advance sequentially';
                    END IF;
                                        SELECT COUNT(*)
                                            INTO released_count
                                            FROM pilot_participant
                                         WHERE clinic_id = NEW.clinic_id
                                             AND pilot_programme_id = NEW.id
                                             AND ordinal <= NEW.active_cumulative_limit
                                             AND released_at IS NOT NULL;
                                        IF released_count <> NEW.active_cumulative_limit THEN
                                                RAISE EXCEPTION 'pilot cumulative release requires every released ordinal';
                                        END IF;
                END IF;
                IF NEW.state = 'active'
                   AND (NEW.active_cumulative_limit = 0
                        OR NEW.released_at IS NULL
                        OR NEW.released_by IS NULL
                        OR NEW.release_evidence_hash IS NULL) THEN
                    RAISE EXCEPTION 'active pilot programme requires release evidence';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """))
    op.execute(sa.text("""
            CREATE OR REPLACE FUNCTION anonymize_pilot_participant_on_patient_delete()
            RETURNS trigger AS $$
            BEGIN
                UPDATE pilot_participant
                   SET patient_id = NULL
                 WHERE clinic_id = OLD.clinic_id
                   AND patient_id = OLD.id;
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql;
            """))
    op.execute(sa.text("""
            CREATE OR REPLACE FUNCTION validate_pilot_participant_insert()
            RETURNS trigger AS $$
            DECLARE
                programme_state pilot_programme_state;
                programme_maximum integer;
                expected_ordinal integer;
            BEGIN
                SELECT state, maximum_unique_patients
                  INTO programme_state, programme_maximum
                  FROM pilot_programme
                 WHERE clinic_id = NEW.clinic_id AND id = NEW.pilot_programme_id
                   FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'pilot programme not found';
                END IF;
                IF programme_state IN ('paused', 'closed') THEN
                    RAISE EXCEPTION 'pilot programme does not accept enrollment';
                END IF;
                SELECT COALESCE(MAX(ordinal), 0) + 1
                  INTO expected_ordinal
                  FROM pilot_participant
                 WHERE clinic_id = NEW.clinic_id
                   AND pilot_programme_id = NEW.pilot_programme_id;
                IF NEW.ordinal <> expected_ordinal OR NEW.ordinal > programme_maximum THEN
                    RAISE EXCEPTION 'pilot participant ordinal is not the next bounded ordinal';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """))
    op.execute(sa.text("""
            CREATE OR REPLACE FUNCTION protect_pilot_participant_identity()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'pilot participant rows are append-only';
                END IF;
                IF NEW.clinic_id IS DISTINCT FROM OLD.clinic_id
                   OR NEW.pilot_programme_id IS DISTINCT FROM OLD.pilot_programme_id
                   OR NEW.patient_key_hash IS DISTINCT FROM OLD.patient_key_hash
                   OR NEW.ordinal IS DISTINCT FROM OLD.ordinal
                   OR NEW.wave IS DISTINCT FROM OLD.wave
                   OR NEW.enrolled_at IS DISTINCT FROM OLD.enrolled_at THEN
                    RAISE EXCEPTION 'pilot participant identity is immutable';
                END IF;
                IF NEW.patient_id IS DISTINCT FROM OLD.patient_id
                   AND NOT (OLD.patient_id IS NOT NULL AND NEW.patient_id IS NULL) THEN
                    RAISE EXCEPTION 'pilot participant patient reference is immutable';
                END IF;
                IF OLD.released_at IS NOT NULL
                   AND NEW.released_at IS DISTINCT FROM OLD.released_at THEN
                    RAISE EXCEPTION 'pilot participant release is immutable';
                END IF;
                IF OLD.first_contact_at IS NOT NULL
                   AND NEW.first_contact_at IS DISTINCT FROM OLD.first_contact_at THEN
                    RAISE EXCEPTION 'pilot participant first contact is immutable';
                END IF;
                IF NEW.first_contact_at IS NOT NULL AND NEW.released_at IS NULL THEN
                    RAISE EXCEPTION 'unreleased participant cannot have first contact';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """))
    op.execute(
        sa.text(
            "CREATE TRIGGER pilot_programme_transition_guard "
            "BEFORE UPDATE OR DELETE ON pilot_programme FOR EACH ROW "
            "EXECUTE FUNCTION protect_pilot_programme()"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER pilot_participant_insert_guard "
            "BEFORE INSERT ON pilot_participant FOR EACH ROW "
            "EXECUTE FUNCTION validate_pilot_participant_insert()"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER pilot_participant_identity_guard "
            "BEFORE UPDATE OR DELETE ON pilot_participant FOR EACH ROW "
            "EXECUTE FUNCTION protect_pilot_participant_identity()"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER pilot_participant_patient_erasure "
            "BEFORE DELETE ON patient FOR EACH ROW "
            "EXECUTE FUNCTION anonymize_pilot_participant_on_patient_delete()"
        )
    )
