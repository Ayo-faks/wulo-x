"""add phase 5 feedback enum values

Revision ID: 0005_phase5_feedback_enums
Revises: 0004_phase3_availability_slots
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_phase5_feedback_enums"
down_revision: str | None = "0004_phase3_availability_slots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add closed-vocabulary values needed by post-visit feedback."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text("ALTER TYPE interaction_intent ADD VALUE IF NOT EXISTS 'feedback'"))
    op.execute(sa.text("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'record_feedback'"))


def downgrade() -> None:
    """Enum value removal is intentionally irreversible in PostgreSQL."""
    return