"""add clinic sms number for inbound routing

Revision ID: 0003_add_clinic_sms_number
Revises: 0002_reconcile_phase0_seed
Create Date: 2026-06-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_add_clinic_sms_number"
down_revision: str | None = "0002_reconcile_phase0_seed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the inbound SMS number used to resolve a clinic before RLS scope."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    clinic_columns = {column["name"] for column in inspector.get_columns("clinic")}
    if "sms_number" not in clinic_columns:
        op.add_column("clinic", sa.Column("sms_number", sa.String(), nullable=True))

    unique_constraints = {constraint["name"] for constraint in inspector.get_unique_constraints("clinic")}
    if "uq_clinic_sms_number" not in unique_constraints:
        op.create_unique_constraint("uq_clinic_sms_number", "clinic", ["sms_number"])


def downgrade() -> None:
    """Remove inbound SMS routing number support."""
    op.drop_constraint("uq_clinic_sms_number", "clinic", type_="unique")
    op.drop_column("clinic", "sms_number")