"""add phase 5 gdpr audit enum values

Revision ID: 0006_phase5_gdpr_audit_enums
Revises: 0005_phase5_feedback_enums
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_phase5_gdpr_audit_enums"
down_revision: str | None = "0005_phase5_feedback_enums"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add audit actions for GDPR and recording consent."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'recording_consent'"))
    op.execute(sa.text("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'retention_purge'"))
    op.execute(sa.text("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'erase_patient'"))


def downgrade() -> None:
    """Enum value removal is intentionally irreversible in PostgreSQL."""
    return