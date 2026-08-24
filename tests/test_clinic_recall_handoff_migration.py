"""Migration contracts for PR-12 receipted handoffs and ageing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from src.clinic_recall.models import (
    TENANT_TABLES,
    BookingAction,
    Escalation,
    ExternalEffectHandoff,
    HandoffReceipt,
    InboundStaffTask,
)

MIGRATION_PATH = Path(
    "infra/postgres/migrations/versions/0024_receipted_handoffs.py"
)
RECEIPT_COLUMNS = {
    "id",
    "clinic_id",
    "escalation_id",
    "inbound_staff_task_id",
    "booking_action_id",
    "external_effect_handoff_id",
    "severity",
    "delivery_state",
    "queued_at",
    "due_at",
    "sent_at",
    "delivered_at",
    "acknowledged_at",
    "acknowledged_by",
    "resolved_at",
    "resolved_by",
    "policy_version",
    "policy_sha256",
    "policy_critical_minutes",
    "policy_high_minutes",
    "policy_normal_business_hours",
    "severity_generation",
    "notification_count",
    "escalation_level",
    "alternate_state",
    "alternate_requested_at",
    "created_at",
    "updated_at",
}


def _config(database_url: str, monkeypatch) -> Config:
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    return Config("infra/postgres/alembic.ini")


def _unique_names(table: sa.Table) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint) and constraint.name
    }


def test_receipt_model_is_minimized_and_all_owners_have_tenant_keys() -> None:
    assert "handoff_receipt" in TENANT_TABLES
    assert set(HandoffReceipt.__table__.c.keys()) == RECEIPT_COLUMNS
    assert "uq_escalation_clinic_id_id" in _unique_names(Escalation.__table__)
    assert "uq_inbound_staff_task_clinic_id_id" in _unique_names(
        InboundStaffTask.__table__
    )
    assert "uq_booking_action_clinic_id_id" in _unique_names(BookingAction.__table__)
    assert "uq_external_effect_handoff_clinic_id_id" in _unique_names(
        ExternalEffectHandoff.__table__
    )
    forbidden = {
        "patient_id",
        "patient_name",
        "phone",
        "email",
        "destination",
        "message_body",
        "transcript",
        "provider_response",
        "provider_error",
        "payload",
    }
    assert forbidden.isdisjoint(RECEIPT_COLUMNS)


def test_0024_declares_linear_head_rls_backfill_and_downgrade_guard() -> None:
    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "0024_receipted_handoffs"' in migration
    assert 'down_revision: str | None = "0023_identity_evidence_tiers"' in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "external_effect_handoff" in migration
    assert "handoff_notification" in migration
    assert "acknowledge" in migration
    assert "refuse PR-12 downgrade with retained receipt evidence" in migration
    assert "sa.JSON" not in migration


def test_sqlite_zero_to_0024_matches_models_and_empty_round_trip(
    tmp_path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'pr12-zero.db'}"
    config = _config(database_url, monkeypatch)

    command.upgrade(config, "0024_receipted_handoffs")
    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    assert {column["name"] for column in inspector.get_columns("handoff_receipt")} == (
        RECEIPT_COLUMNS
    )
    assert {constraint["name"] for constraint in inspector.get_unique_constraints("escalation")} >= {
        "uq_escalation_clinic_id_id"
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("external_effect_handoff")
    } >= {"uq_external_effect_handoff_clinic_id_id"}
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0024_receipted_handoffs"
        )

    command.downgrade(config, "0023_identity_evidence_tiers")
    assert "handoff_receipt" not in sa.inspect(engine).get_table_names()
    command.upgrade(config, "0024_receipted_handoffs")
    assert "handoff_receipt" in sa.inspect(engine).get_table_names()
    engine.dispose()


def test_0024_backfills_open_owner_without_effect_and_refuses_downgrade(
    tmp_path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'pr12-retained.db'}"
    config = _config(database_url, monkeypatch)
    command.upgrade(config, "0023_identity_evidence_tiers")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO clinic (id, name) VALUES ('clinic-pr12', 'PR12')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO patient (id, clinic_id, source_ref, name) VALUES "
                "('patient-pr12', 'clinic-pr12', 'P-PR12', 'Synthetic')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO escalation ("
                "id, clinic_id, patient_id, reason, priority, status, created_at, updated_at"
                ") VALUES ("
                "'escalation-pr12', 'clinic-pr12', 'patient-pr12', 'urgent', "
                "'high', 'open', :created_at, :created_at)"
            ),
            {"created_at": "2026-07-27T12:00:00+00:00"},
        )

    command.upgrade(config, "0024_receipted_handoffs")
    with engine.connect() as connection:
        receipt = connection.execute(
            sa.text(
                "SELECT severity, escalation_id, queued_at, due_at "
                "FROM handoff_receipt WHERE clinic_id = 'clinic-pr12'"
            )
        ).mappings().one()
        effect_count = connection.scalar(
            sa.text(
                "SELECT count(*) FROM external_effect "
                "WHERE effect_type = 'handoff_notification'"
            )
        )
    assert receipt["severity"] == "critical"
    assert receipt["escalation_id"] == "escalation-pr12"
    queued_at = datetime.fromisoformat(receipt["queued_at"]).replace(tzinfo=UTC)
    due_at = datetime.fromisoformat(receipt["due_at"]).replace(tzinfo=UTC)
    assert queued_at == datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    assert due_at == datetime(2026, 7, 27, 12, 5, tzinfo=UTC)
    assert effect_count == 0

    with pytest.raises(
        RuntimeError,
        match="refuse PR-12 downgrade with retained receipt evidence",
    ):
        command.downgrade(config, "0023_identity_evidence_tiers")
    engine.dispose()