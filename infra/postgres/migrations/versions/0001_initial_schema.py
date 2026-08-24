"""initial clinic recall schema + row-level security

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-06-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from src.clinic_recall.models import Base
from src.clinic_recall.rls import apply_rls_policies, drop_rls_policies

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create every table from the ORM metadata and lock tenant tables with RLS."""
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    if bind.dialect.name == "postgresql":
        apply_rls_policies(bind)


def downgrade() -> None:
    """Drop the RLS policies and every table."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        drop_rls_policies(bind)
    Base.metadata.drop_all(bind=bind)
    # Drop the native enum types create_all/drop_all leaves behind on PostgreSQL.
    if bind.dialect.name == "postgresql":
        for enum_name in (
            "appointment_status",
            "reason_code",
            "channel",
            "campaign_type",
            "campaign_status",
            "outreach_state",
            "interaction_direction",
            "interaction_intent",
            "interaction_outcome",
            "booking_action_type",
            "booking_action_status",
            "escalation_reason",
            "escalation_priority",
            "escalation_status",
            "audit_action",
            "prompt_proposal_status",
            "clinic_phone_provider",
            "clinic_phone_purpose",
            "clinic_phone_status",
            "inbound_call_status",
            "inbound_staff_task_kind",
            "inbound_staff_task_status",
        ):
            op.execute(sa.text(f"DROP TYPE IF EXISTS {enum_name}"))
