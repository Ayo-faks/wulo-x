"""add controlled csv import provenance, source links, and alias tombstones

Revision ID: 0021_controlled_csv_import
Revises: 0020_availability_booking_state
Create Date: 2026-07-26

PR-08: metadata-only ``import_batch`` provenance, provider-qualified
``patient_source_link`` aliases, the ``import_match_review`` queue, and
``rights_alias_tombstone`` anti-rehydration evidence. No raw CSV content,
filenames, or row-level errors are representable in this schema.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from src.clinic_recall.enums import (
    ImportBatchState,
    ImportMatchReviewState,
    MatchStrategy,
    SourceLinkState,
    SourceSystem,
)
from src.clinic_recall.models import RLS_GUC

revision: str = "0021_controlled_csv_import"
down_revision: str | None = "0020_availability_booking_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUMS = (
    (ImportBatchState, "import_batch_state"),
    (SourceSystem, "source_system"),
    (SourceLinkState, "source_link_state"),
    (ImportMatchReviewState, "import_match_review_state"),
    (MatchStrategy, "match_strategy"),
)

_TABLES = (
    "import_match_review",
    "patient_source_link",
    "import_batch",
    "rights_alias_tombstone",
)

_MODEL_COLUMNS = {
    "import_batch": {
        "id",
        "clinic_id",
        "state",
        "file_sha256",
        "validation_summary_sha256",
        "schema_version",
        "source_system",
        "export_at",
        "preview_requested_at",
        "preview_actor",
        "preview_expires_at",
        "preview_upload_disposed_at",
        "approved_at",
        "approved_by",
        "approval_upload_disposed_at",
        "attestation_version",
        "attested_channels",
        "consent_policy_version",
        "consent_policy_hash",
        "consent_authority_granted",
        "total_rows",
        "valid_row_count",
        "invalid_row_count",
        "patient_count",
        "appointment_count",
        "error_count",
        "error_reason_counts",
        "patients_inserted",
        "patients_updated",
        "appointments_inserted",
        "appointments_updated",
        "consent_granted_count",
        "consent_unknown_count",
        "opt_out_count",
        "completed_at",
        "metadata_retention_state",
        "created_at",
        "updated_at",
    },
    "patient_source_link": {
        "id",
        "clinic_id",
        "patient_id",
        "provider",
        "source_ref",
        "import_batch_id",
        "state",
        "strategy",
        "strategy_version",
        "evidence_hash",
        "resolved_by",
        "resolved_at",
        "created_at",
        "updated_at",
    },
    "import_match_review": {
        "id",
        "clinic_id",
        "import_batch_id",
        "patient_id",
        "provider",
        "strategy",
        "strategy_version",
        "state",
        "candidate_count",
        "candidate_evidence_hash",
        "reason",
        "resolved_by",
        "resolved_at",
        "source_link_id",
        "created_at",
        "updated_at",
    },
    "rights_alias_tombstone": {
        "id",
        "clinic_id",
        "rights_request_id",
        "provider",
        "subject_key_hash",
        "subject_key_version",
        "created_at",
        "updated_at",
    },
}


def _enum_type(py_enum, name: str) -> sa.types.TypeEngine:
    values = [member.value for member in py_enum]
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(
        py_enum,
        name=name,
        native_enum=False,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


def upgrade() -> None:
    """Create or adopt PR-08 tables, enums, audit values, and forced RLS."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for py_enum, name in _ENUMS:
            postgresql.ENUM(*[member.value for member in py_enum], name=name).create(
                bind, checkfirst=True
            )
        with op.get_context().autocommit_block():
            op.execute(
                sa.text("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'csv_import_preview'")
            )
            op.execute(
                sa.text("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'csv_import_approve'")
            )
            op.execute(
                sa.text("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'csv_import_match'")
            )

    existing = set(sa.inspect(bind).get_table_names()) & set(_TABLES)
    if existing:
        if existing != set(_TABLES):
            raise RuntimeError("0021 found a partial PR-08 schema; repair before replay")
        _validate_adopted_schema(bind)
        if bind.dialect.name == "postgresql":
            _apply_policies()
        return

    op.create_table(
        "import_batch",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "clinic_id",
            sa.String(),
            sa.ForeignKey("clinic.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "state",
            _enum_type(ImportBatchState, "import_batch_state"),
            nullable=False,
            server_default=ImportBatchState.PREVIEW_VALID.value,
        ),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("validation_summary_sha256", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("source_system", _enum_type(SourceSystem, "source_system"), nullable=False),
        sa.Column("export_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("preview_requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("preview_actor", sa.String(254), nullable=False),
        sa.Column("preview_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("preview_upload_disposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(254), nullable=True),
        sa.Column("approval_upload_disposed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attestation_version", sa.String(64), nullable=True),
        sa.Column("attested_channels", sa.JSON(), nullable=True),
        sa.Column("consent_policy_version", sa.String(128), nullable=True),
        sa.Column("consent_policy_hash", sa.String(64), nullable=True),
        sa.Column(
            "consent_authority_granted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("total_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("valid_row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invalid_row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("patient_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("appointment_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_reason_counts", sa.JSON(), nullable=True),
        sa.Column("patients_inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("patients_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("appointments_inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("appointments_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consent_granted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consent_unknown_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opt_out_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata_retention_state",
            sa.String(32),
            nullable=False,
            server_default="retained",
        ),
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
        sa.UniqueConstraint("clinic_id", "id", name="uq_import_batch_clinic_id_id"),
        sa.CheckConstraint("length(file_sha256) = 64", name="ck_import_batch_file_hash_length"),
        sa.CheckConstraint(
            "length(validation_summary_sha256) = 64",
            name="ck_import_batch_summary_hash_length",
        ),
        sa.CheckConstraint(
            "consent_policy_hash IS NULL OR length(consent_policy_hash) = 64",
            name="ck_import_batch_policy_hash_length",
        ),
        sa.CheckConstraint(
            "total_rows >= 0 AND valid_row_count >= 0 AND invalid_row_count >= 0 "
            "AND patient_count >= 0 AND appointment_count >= 0 "
            "AND error_count >= 0 AND patients_inserted >= 0 "
            "AND patients_updated >= 0 AND appointments_inserted >= 0 "
            "AND appointments_updated >= 0 AND consent_granted_count >= 0 "
            "AND consent_unknown_count >= 0 AND opt_out_count >= 0",
            name="ck_import_batch_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "valid_row_count + invalid_row_count = total_rows",
            name="ck_import_batch_row_counts_exact",
        ),
        sa.CheckConstraint(
            "error_count <= 100",
            name="ck_import_batch_error_count_bounded",
        ),
        sa.CheckConstraint(
            "patient_count <= total_rows AND appointment_count <= total_rows",
            name="ck_import_batch_counts_bounded",
        ),
        sa.CheckConstraint(
            "state != 'completed' OR ("
            "completed_at IS NOT NULL AND approved_at IS NOT NULL "
            "AND approved_by IS NOT NULL "
            "AND approval_upload_disposed_at IS NOT NULL)",
            name="ck_import_batch_completed_evidence",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= preview_requested_at",
            name="ck_import_batch_timestamp_order",
        ),
        sa.CheckConstraint(
            "preview_expires_at > preview_requested_at "
            "AND preview_upload_disposed_at <= preview_requested_at "
            "AND (approved_at IS NULL OR approved_at >= preview_requested_at) "
            "AND (approval_upload_disposed_at IS NULL OR "
            "approval_upload_disposed_at >= preview_requested_at) "
            "AND (completed_at IS NULL OR (approved_at IS NOT NULL "
            "AND completed_at >= approved_at "
            "AND approval_upload_disposed_at <= completed_at))",
            name="ck_import_batch_lifecycle_order",
        ),
        sa.CheckConstraint(
            "state != 'completed' OR ("
            "patients_inserted + patients_updated = patient_count "
            "AND appointments_inserted + appointments_updated = appointment_count)",
            name="ck_import_batch_completed_counts_exact",
        ),
    )
    op.create_index(
        "uq_import_batch_live_file",
        "import_batch",
        ["clinic_id", "file_sha256", "schema_version"],
        unique=True,
        postgresql_where=sa.text("state IN ('preview_valid', 'completed')"),
        sqlite_where=sa.text("state IN ('preview_valid', 'completed')"),
    )
    op.create_index("ix_import_batch_clinic_state", "import_batch", ["clinic_id", "state"])
    op.create_index("ix_import_batch_clinic_created", "import_batch", ["clinic_id", "created_at"])

    op.create_table(
        "patient_source_link",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "clinic_id",
            sa.String(),
            sa.ForeignKey("clinic.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("patient_id", sa.String(), nullable=False, index=True),
        sa.Column("provider", _enum_type(SourceSystem, "source_system"), nullable=False),
        sa.Column("source_ref", sa.String(255), nullable=False),
        sa.Column("import_batch_id", sa.String(), nullable=True),
        sa.Column(
            "state",
            _enum_type(SourceLinkState, "source_link_state"),
            nullable=False,
            server_default=SourceLinkState.ACTIVE.value,
        ),
        sa.Column("strategy", _enum_type(MatchStrategy, "match_strategy"), nullable=False),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("resolved_by", sa.String(254), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.UniqueConstraint("clinic_id", "id", name="uq_patient_source_link_clinic_id_id"),
        sa.UniqueConstraint(
            "clinic_id", "provider", "source_ref", name="uq_patient_source_link_provider_ref"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patient.clinic_id", "patient.id"],
            ondelete="CASCADE",
            name="fk_patient_source_link_patient_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "import_batch_id"],
            ["import_batch.clinic_id", "import_batch.id"],
            ondelete="RESTRICT",
            name="fk_patient_source_link_batch_tenant",
        ),
        sa.CheckConstraint(
            "length(evidence_hash) = 64", name="ck_patient_source_link_evidence_hash"
        ),
        sa.CheckConstraint(
            "length(source_ref) >= 1 AND length(source_ref) <= 255",
            name="ck_patient_source_link_ref_bounds",
        ),
    )
    op.create_index(
        "uq_patient_source_link_active",
        "patient_source_link",
        ["clinic_id", "patient_id", "provider"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )
    op.create_index(
        "ix_patient_source_link_clinic_patient",
        "patient_source_link",
        ["clinic_id", "patient_id"],
    )

    op.create_table(
        "import_match_review",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "clinic_id",
            sa.String(),
            sa.ForeignKey("clinic.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("import_batch_id", sa.String(), nullable=False, index=True),
        sa.Column("patient_id", sa.String(), nullable=False, index=True),
        sa.Column("provider", _enum_type(SourceSystem, "source_system"), nullable=False),
        sa.Column("strategy", _enum_type(MatchStrategy, "match_strategy"), nullable=False),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column(
            "state",
            _enum_type(ImportMatchReviewState, "import_match_review_state"),
            nullable=False,
            server_default=ImportMatchReviewState.PENDING.value,
        ),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidate_evidence_hash", sa.String(64), nullable=True),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("resolved_by", sa.String(254), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_link_id", sa.String(), nullable=True),
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
        sa.UniqueConstraint(
            "clinic_id",
            "import_batch_id",
            "patient_id",
            "provider",
            name="uq_import_match_review_scope",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patient.clinic_id", "patient.id"],
            ondelete="CASCADE",
            name="fk_import_match_review_patient_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "import_batch_id"],
            ["import_batch.clinic_id", "import_batch.id"],
            ondelete="CASCADE",
            name="fk_import_match_review_batch_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "source_link_id"],
            ["patient_source_link.clinic_id", "patient_source_link.id"],
            ondelete="RESTRICT",
            name="fk_import_match_review_source_link_tenant",
        ),
        sa.CheckConstraint(
            "candidate_count >= 0",
            name="ck_import_match_review_candidates_nonnegative",
        ),
        sa.CheckConstraint(
            "candidate_evidence_hash IS NULL OR length(candidate_evidence_hash) = 64",
            name="ck_import_match_review_evidence_hash",
        ),
        sa.CheckConstraint(
            "(state = 'linked' AND source_link_id IS NOT NULL "
            "AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL) OR "
            "(state = 'dismissed' AND source_link_id IS NULL "
            "AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL) OR "
            "(state NOT IN ('linked', 'dismissed') AND source_link_id IS NULL "
            "AND resolved_by IS NULL AND resolved_at IS NULL)",
            name="ck_import_match_review_resolution_state",
        ),
    )
    op.create_index(
        "ix_import_match_review_clinic_state",
        "import_match_review",
        ["clinic_id", "state"],
    )

    op.create_table(
        "rights_alias_tombstone",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "clinic_id",
            sa.String(),
            sa.ForeignKey("clinic.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("rights_request_id", sa.String(), nullable=False, index=True),
        sa.Column("provider", _enum_type(SourceSystem, "source_system"), nullable=False),
        sa.Column("subject_key_hash", sa.String(64), nullable=False),
        sa.Column("subject_key_version", sa.String(64), nullable=False),
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
        sa.UniqueConstraint(
            "clinic_id", "subject_key_hash", name="uq_rights_alias_tombstone_subject"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "rights_request_id"],
            ["rights_request.clinic_id", "rights_request.id"],
            ondelete="RESTRICT",
            name="fk_rights_alias_tombstone_request_tenant",
        ),
        sa.CheckConstraint(
            "length(subject_key_hash) = 64",
            name="ck_rights_alias_tombstone_hash_length",
        ),
    )
    op.create_index(
        "ix_rights_alias_tombstone_clinic_hash",
        "rights_alias_tombstone",
        ["clinic_id", "subject_key_hash"],
    )

    if bind.dialect.name == "postgresql":
        _apply_policies()


def downgrade() -> None:
    """Remove PR-08 schema only when it retains no import or rights state."""
    bind = op.get_bind()
    if _pr08_row_count(bind):
        raise RuntimeError(
            "0021 contains retained import/link/review/tombstone state; "
            "roll back by disabling CSV import, not by downgrading the schema"
        )
    for table in _TABLES:
        op.drop_table(table)
    if bind.dialect.name == "postgresql":
        for _, name in reversed(_ENUMS):
            postgresql.ENUM(name=name).drop(bind, checkfirst=True)
    # audit_action enum values are intentionally irreversible in PostgreSQL.


def _pr08_row_count(bind) -> int:
    """Count retained PR-08 rows across every tenant, even under forced RLS.

    The migration role owns the tables but is not BYPASSRLS, so a plain count
    sees nothing. Mirror 0019: relax the clinic catalogue, then count each
    tenant's rows under its own GUC and restore the forced policy.
    """
    if bind.dialect.name != "postgresql":
        return sum(
            int(bind.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608
            for table in _TABLES
        )

    bind.execute(sa.text("ALTER TABLE clinic NO FORCE ROW LEVEL SECURITY"))
    total = 0
    clinic_ids = list(bind.execute(sa.text("SELECT id FROM clinic ORDER BY id")).scalars())
    for clinic_id in clinic_ids:
        bind.execute(
            sa.text("SELECT set_config(:setting, :clinic_id, true)"),
            {"setting": RLS_GUC, "clinic_id": clinic_id},
        )
        for table in _TABLES:
            total += int(
                bind.scalar(
                    sa.text(
                        f"SELECT count(*) FROM {table} WHERE clinic_id = :clinic_id"  # noqa: S608
                    ),
                    {"clinic_id": clinic_id},
                )
                or 0
            )
    bind.execute(
        sa.text("SELECT set_config(:setting, '', true)"),
        {"setting": RLS_GUC},
    )
    bind.execute(sa.text("ALTER TABLE clinic FORCE ROW LEVEL SECURITY"))
    return total


def _apply_policies() -> None:
    predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
    for table in _TABLES:
        policy = f"{table}_tenant_isolation"
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
        op.execute(
            sa.text(
                f"CREATE POLICY {policy} ON {table} USING ({predicate}) WITH CHECK ({predicate})"
            )
        )


def _validate_adopted_schema(bind) -> None:
    """Adopt only a byte-identical model schema created by ``create_all``."""
    inspector = sa.inspect(bind)
    for table, expected in _MODEL_COLUMNS.items():
        actual = {column["name"] for column in inspector.get_columns(table)}
        if actual != expected:
            raise RuntimeError(f"0021 cannot adopt an incompatible {table} table")
