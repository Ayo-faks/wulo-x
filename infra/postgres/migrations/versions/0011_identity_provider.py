"""add identity provider to clinic_identity_mapping

Google login shares the EasyAuth principal path with Entra, but Google `sub`
and Entra `oid` live in different namespaces. Scope identity mapping rows to
their provider so a subject/email from one provider can never match a login
from another (fail-closed account linking).

Revision ID: 0011_identity_provider
Revises: 0010_incident_reports
Create Date: 2026-07-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_identity_provider"
down_revision: str | None = "0010_incident_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "clinic_identity_mapping"


def _table_snapshot(with_provider: bool) -> sa.Table:
    """Pre/post-migration table shape for batch recreation on SQLite.

    Mirrors the 0007 create_table; Postgres uses in-place ALTERs instead.
    """
    columns: list[sa.schema.SchemaItem] = [
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("clinic_id", sa.String(), nullable=False),
    ]
    if with_provider:
        columns.append(sa.Column("provider", sa.String(), server_default="aad", nullable=False))
    columns.extend(
        [
            sa.Column("subject", sa.String(), nullable=True),
            sa.Column("email", sa.String(), nullable=True),
            sa.Column("roles", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(), server_default="active", nullable=False),
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
            sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        ]
    )
    if with_provider:
        columns.extend(
            [
                sa.UniqueConstraint(
                    "provider", "subject", name="uq_clinic_identity_mapping_provider_subject"
                ),
                sa.UniqueConstraint(
                    "provider", "email", name="uq_clinic_identity_mapping_provider_email"
                ),
            ]
        )
    else:
        columns.extend(
            [
                sa.UniqueConstraint("subject", name="uq_clinic_identity_mapping_subject"),
                sa.UniqueConstraint("email", name="uq_clinic_identity_mapping_email"),
            ]
        )
    return sa.Table(_TABLE, sa.MetaData(), *columns)


def upgrade() -> None:
    """Add provider column and provider-scoped uniqueness.

    Guarded for fresh installs where earlier migrations materialize tables
    from the live models (which already include the provider column and the
    provider-scoped constraints).
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    uniques = {constraint["name"] for constraint in inspector.get_unique_constraints(_TABLE)}
    indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}

    if bind.dialect.name == "postgresql":
        if "provider" not in columns:
            op.add_column(
                _TABLE, sa.Column("provider", sa.String(), server_default="aad", nullable=False)
            )
        if "uq_clinic_identity_mapping_subject" in uniques:
            op.drop_constraint("uq_clinic_identity_mapping_subject", _TABLE, type_="unique")
        if "uq_clinic_identity_mapping_email" in uniques:
            op.drop_constraint("uq_clinic_identity_mapping_email", _TABLE, type_="unique")
        if "uq_clinic_identity_mapping_provider_subject" not in uniques:
            op.create_unique_constraint(
                "uq_clinic_identity_mapping_provider_subject", _TABLE, ["provider", "subject"]
            )
        if "uq_clinic_identity_mapping_provider_email" not in uniques:
            op.create_unique_constraint(
                "uq_clinic_identity_mapping_provider_email", _TABLE, ["provider", "email"]
            )
    else:
        with op.batch_alter_table(_TABLE, copy_from=_table_snapshot(with_provider=False)) as batch:
            batch.add_column(
                sa.Column("provider", sa.String(), server_default="aad", nullable=False)
            )
            batch.drop_constraint("uq_clinic_identity_mapping_subject", type_="unique")
            batch.drop_constraint("uq_clinic_identity_mapping_email", type_="unique")
            batch.create_unique_constraint(
                "uq_clinic_identity_mapping_provider_subject", ["provider", "subject"]
            )
            batch.create_unique_constraint(
                "uq_clinic_identity_mapping_provider_email", ["provider", "email"]
            )
    if f"ix_{_TABLE}_provider" not in indexes:
        op.create_index(f"ix_{_TABLE}_provider", _TABLE, ["provider"])


def downgrade() -> None:
    """Drop provider column and restore global subject/email uniqueness.

    Only safe when no non-aad rows exist; duplicate subjects/emails across
    providers would violate the restored constraints.
    """
    op.drop_index(f"ix_{_TABLE}_provider", table_name=_TABLE)
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint("uq_clinic_identity_mapping_provider_subject", _TABLE, type_="unique")
        op.drop_constraint("uq_clinic_identity_mapping_provider_email", _TABLE, type_="unique")
        op.create_unique_constraint("uq_clinic_identity_mapping_subject", _TABLE, ["subject"])
        op.create_unique_constraint("uq_clinic_identity_mapping_email", _TABLE, ["email"])
        op.drop_column(_TABLE, "provider")
    else:
        with op.batch_alter_table(_TABLE, copy_from=_table_snapshot(with_provider=True)) as batch:
            batch.drop_constraint("uq_clinic_identity_mapping_provider_subject", type_="unique")
            batch.drop_constraint("uq_clinic_identity_mapping_provider_email", type_="unique")
            batch.create_unique_constraint("uq_clinic_identity_mapping_subject", ["subject"])
            batch.create_unique_constraint("uq_clinic_identity_mapping_email", ["email"])
            batch.drop_column("provider")
