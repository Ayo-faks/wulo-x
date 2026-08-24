"""enforce active Clinic Recall task idempotency

Revision ID: 0013_recall_task_idempotency
Revises: 0012_call_records
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_recall_task_idempotency"
down_revision: str | None = "0012_call_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_STATUSES = "'open', 'acknowledged'"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                sa.text(
                    "ALTER TYPE escalation_status ADD VALUE IF NOT EXISTS 'cancelled'"
                )
            )

    inspector = sa.inspect(bind)
    escalation_columns = {
        column["name"] for column in inspector.get_columns("escalation")
    }
    if "outreach_job_id" not in escalation_columns:
        op.add_column(
            "escalation",
            sa.Column("outreach_job_id", sa.String(), nullable=True),
        )
        op.create_foreign_key(
            "fk_escalation_outreach_job_id",
            "escalation",
            "outreach_job",
            ["outreach_job_id"],
            ["id"],
            ondelete="SET NULL",
        )
    _ensure_plain_index(
        "escalation",
        "ix_escalation_outreach_job_id",
        ["outreach_job_id"],
    )

    op.execute(
        sa.text(
            """
            UPDATE escalation AS escalation_row
            SET outreach_job_id = interaction_row.outreach_job_id
            FROM interaction AS interaction_row
            WHERE escalation_row.outreach_job_id IS NULL
              AND escalation_row.context_ref = interaction_row.id
              AND escalation_row.clinic_id = interaction_row.clinic_id
            """
        )
    )

    if bind.dialect.name == "postgresql":
        _cancel_duplicate_inbound_tasks("inbound_call_id")
        _cancel_duplicate_inbound_tasks("inbound_message_id")
        _cancel_duplicate_escalations()

    _ensure_active_indexes()


def downgrade() -> None:
    _drop_index_if_present(
        "escalation",
        "uq_escalation_active_outreach_job",
    )
    _drop_index_if_present(
        "inbound_staff_task",
        "uq_inbound_staff_task_active_message_kind",
    )
    _drop_index_if_present(
        "inbound_staff_task",
        "uq_inbound_staff_task_active_call_kind",
    )
    _drop_index_if_present("escalation", "ix_escalation_outreach_job_id")

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    escalation_columns = {
        column["name"] for column in inspector.get_columns("escalation")
    }
    if "outreach_job_id" in escalation_columns:
        foreign_keys = {
            constraint.get("name")
            for constraint in inspector.get_foreign_keys("escalation")
        }
        if "fk_escalation_outreach_job_id" in foreign_keys:
            op.drop_constraint(
                "fk_escalation_outreach_job_id",
                "escalation",
                type_="foreignkey",
            )
        op.drop_column("escalation", "outreach_job_id")
    # PostgreSQL enum values are intentionally irreversible on downgrade.


def _ensure_plain_index(table: str, name: str, columns: list[str]) -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes(table)}
    if name not in indexes:
        op.create_index(name, table, columns)


def _ensure_active_indexes() -> None:
    bind = op.get_bind()
    inbound_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("inbound_staff_task")
    }
    active = sa.text(f"status IN ({_ACTIVE_STATUSES})")
    if "uq_inbound_staff_task_active_call_kind" not in inbound_indexes:
        op.create_index(
            "uq_inbound_staff_task_active_call_kind",
            "inbound_staff_task",
            ["clinic_id", "inbound_call_id", "kind"],
            unique=True,
            postgresql_where=sa.text(
                f"inbound_call_id IS NOT NULL AND status IN ({_ACTIVE_STATUSES})"
            ),
            sqlite_where=sa.text(
                f"inbound_call_id IS NOT NULL AND status IN ({_ACTIVE_STATUSES})"
            ),
        )
    if "uq_inbound_staff_task_active_message_kind" not in inbound_indexes:
        op.create_index(
            "uq_inbound_staff_task_active_message_kind",
            "inbound_staff_task",
            ["clinic_id", "inbound_message_id", "kind"],
            unique=True,
            postgresql_where=sa.text(
                f"inbound_message_id IS NOT NULL AND status IN ({_ACTIVE_STATUSES})"
            ),
            sqlite_where=sa.text(
                f"inbound_message_id IS NOT NULL AND status IN ({_ACTIVE_STATUSES})"
            ),
        )
    escalation_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("escalation")
    }
    if "uq_escalation_active_outreach_job" not in escalation_indexes:
        op.create_index(
            "uq_escalation_active_outreach_job",
            "escalation",
            ["clinic_id", "outreach_job_id"],
            unique=True,
            postgresql_where=sa.text(
                f"outreach_job_id IS NOT NULL AND {active.text}"
            ),
            sqlite_where=sa.text(
                f"outreach_job_id IS NOT NULL AND {active.text}"
            ),
        )


def _cancel_duplicate_inbound_tasks(anchor_column: str) -> None:
    op.execute(
        sa.text(
            f"""
            WITH ranked AS (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY clinic_id, {anchor_column}, kind
                           ORDER BY
                               CASE priority
                                   WHEN 'high' THEN 3
                                   WHEN 'normal' THEN 2
                                   WHEN 'low' THEN 1
                                   ELSE 0
                               END DESC,
                               CASE reason
                                   WHEN 'urgent' THEN 7
                                   WHEN 'safeguarding' THEN 6
                                   WHEN 'distress' THEN 5
                                   WHEN 'clinical' THEN 4
                                   WHEN 'complaint' THEN 3
                                   WHEN 'opt_out_identity_unclear' THEN 2
                                   ELSE 1
                               END DESC,
                               created_at ASC,
                               id ASC
                       ) AS winner_rank
                FROM inbound_staff_task
                WHERE {anchor_column} IS NOT NULL
                  AND status IN ({_ACTIVE_STATUSES})
            )
            UPDATE inbound_staff_task AS task
            SET status = 'cancelled'
            FROM ranked
            WHERE task.id = ranked.id
              AND ranked.winner_rank > 1
            """
        )
    )


def _cancel_duplicate_escalations() -> None:
    op.execute(
        sa.text(
            f"""
            WITH ranked AS (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY clinic_id, outreach_job_id
                           ORDER BY
                               CASE priority
                                   WHEN 'high' THEN 3
                                   WHEN 'normal' THEN 2
                                   WHEN 'low' THEN 1
                                   ELSE 0
                               END DESC,
                               CASE reason
                                   WHEN 'urgent' THEN 5
                                   WHEN 'clinical' THEN 4
                                   WHEN 'complaint' THEN 3
                                   WHEN 'ambiguous' THEN 2
                                   ELSE 1
                               END DESC,
                               created_at ASC,
                               id ASC
                       ) AS winner_rank
                FROM escalation
                WHERE outreach_job_id IS NOT NULL
                  AND status IN ({_ACTIVE_STATUSES})
            )
            UPDATE escalation AS escalation_row
            SET status = 'cancelled'
            FROM ranked
            WHERE escalation_row.id = ranked.id
              AND ranked.winner_rank > 1
            """
        )
    )


def _drop_index_if_present(table: str, name: str) -> None:
    indexes = {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)
    }
    if name in indexes:
        op.drop_index(name, table_name=table)