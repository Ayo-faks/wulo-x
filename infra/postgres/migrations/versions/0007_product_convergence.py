"""add product convergence identity and prompt proposal tables

Revision ID: 0007_product_convergence
Revises: 0006_phase5_gdpr_audit_enums
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from src.clinic_recall.enums import PromptProposalStatus
from src.clinic_recall.models import RLS_GUC

revision: str = "0007_product_convergence"
down_revision: str | None = "0006_phase5_gdpr_audit_enums"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _prompt_proposal_status_enum() -> sa.Enum:
    return sa.Enum(
        PromptProposalStatus,
        name="prompt_proposal_status",
        native_enum=True,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


def upgrade() -> None:
    """Add durable identity mappings and governed prompt proposal records."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "clinic_identity_mapping" not in tables:
        op.create_table(
            "clinic_identity_mapping",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("clinic_id", sa.String(), nullable=False),
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
            sa.UniqueConstraint("subject", name="uq_clinic_identity_mapping_subject"),
            sa.UniqueConstraint("email", name="uq_clinic_identity_mapping_email"),
        )

    if "prompt_proposal" not in tables:
        op.create_table(
            "prompt_proposal",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("clinic_id", sa.String(), nullable=False),
            sa.Column("actor", sa.String(), nullable=False),
            sa.Column(
                "status",
                _prompt_proposal_status_enum(),
                server_default=PromptProposalStatus.SUBMITTED.value,
                nullable=False,
            ),
            sa.Column("proposed_prompt", sa.Text(), nullable=False),
            sa.Column("diff", sa.Text(), nullable=False),
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
        )

    _ensure_indexes(sa.inspect(bind))

    if bind.dialect.name == "postgresql":
        policy = "prompt_proposal_tenant_isolation"
        predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
        op.execute(sa.text("ALTER TABLE prompt_proposal ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text("ALTER TABLE prompt_proposal FORCE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON prompt_proposal"))
        op.execute(
            sa.text(
                f"CREATE POLICY {policy} ON prompt_proposal "
                f"USING ({predicate}) WITH CHECK ({predicate})"
            )
        )


def _ensure_indexes(inspector: sa.Inspector) -> None:
    identity_indexes = {
        index["name"] for index in inspector.get_indexes("clinic_identity_mapping")
    }
    if "ix_clinic_identity_mapping_clinic_id" not in identity_indexes:
        op.create_index(
            "ix_clinic_identity_mapping_clinic_id", "clinic_identity_mapping", ["clinic_id"]
        )
    if "ix_clinic_identity_mapping_subject" not in identity_indexes:
        op.create_index(
            "ix_clinic_identity_mapping_subject", "clinic_identity_mapping", ["subject"]
        )
    if "ix_clinic_identity_mapping_email" not in identity_indexes:
        op.create_index("ix_clinic_identity_mapping_email", "clinic_identity_mapping", ["email"])
    if "ix_clinic_identity_mapping_clinic_status" not in identity_indexes:
        op.create_index(
            "ix_clinic_identity_mapping_clinic_status",
            "clinic_identity_mapping",
            ["clinic_id", "status"],
        )

    proposal_indexes = {index["name"] for index in inspector.get_indexes("prompt_proposal")}
    if "ix_prompt_proposal_clinic_id" not in proposal_indexes:
        op.create_index("ix_prompt_proposal_clinic_id", "prompt_proposal", ["clinic_id"])
    if "ix_prompt_proposal_clinic_status" not in proposal_indexes:
        op.create_index(
            "ix_prompt_proposal_clinic_status", "prompt_proposal", ["clinic_id", "status"]
        )
    if "ix_prompt_proposal_clinic_created" not in proposal_indexes:
        op.create_index(
            "ix_prompt_proposal_clinic_created", "prompt_proposal", ["clinic_id", "created_at"]
        )


def downgrade() -> None:
    """Remove product convergence tables."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP POLICY IF EXISTS prompt_proposal_tenant_isolation ON prompt_proposal"))
        op.execute(sa.text("ALTER TABLE prompt_proposal DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_prompt_proposal_clinic_created", table_name="prompt_proposal")
    op.drop_index("ix_prompt_proposal_clinic_status", table_name="prompt_proposal")
    op.drop_index("ix_prompt_proposal_clinic_id", table_name="prompt_proposal")
    op.drop_table("prompt_proposal")
    op.drop_index("ix_clinic_identity_mapping_clinic_status", table_name="clinic_identity_mapping")
    op.drop_index("ix_clinic_identity_mapping_email", table_name="clinic_identity_mapping")
    op.drop_index("ix_clinic_identity_mapping_subject", table_name="clinic_identity_mapping")
    op.drop_index("ix_clinic_identity_mapping_clinic_id", table_name="clinic_identity_mapping")
    op.drop_table("clinic_identity_mapping")
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP TYPE IF EXISTS prompt_proposal_status"))