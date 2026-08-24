"""Ordinary-role PostgreSQL race and RLS proof for PR-12 handoffs."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from src.clinic_recall.db import clinic_scope
from src.clinic_recall.enums import (
    CampaignStatus,
    CampaignType,
    Channel,
    ClinicPhoneProvider,
    ClinicPhonePurpose,
    ClinicPhoneStatus,
    EscalationReason,
    ExternalEffectState,
    ExternalEffectType,
    InboundCallStatus,
    InboundStaffTaskKind,
    OutreachState,
    PilotProgrammeState,
)
from src.clinic_recall.escalation import escalate_to_staff
from src.clinic_recall.handoff_ageing import run_handoff_ageing_once
from src.clinic_recall.inbound_staff_tasks import create_inbound_staff_task
from src.clinic_recall.models import (
    Campaign,
    Clinic,
    ClinicPhoneNumber,
    Escalation,
    ExternalEffect,
    HandoffReceipt,
    InboundCall,
    InboundStaffTask,
    OutreachJob,
    Patient,
    PilotProgramme,
)
from src.clinic_recall.pilot_controls import create_programme
from src.clinic_recall.staff_queue import (
    QueueDecision,
    acknowledge_queue_item,
    resolve_queue_item,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
CLINIC_A = "clinic-handoff-pg-a"
CLINIC_B = "clinic-handoff-pg-b"


def _reset(engine, monkeypatch) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(sa.text("DROP SCHEMA public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))
    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        engine.url.render_as_string(hide_password=False),
    )
    command.upgrade(Config("infra/postgres/alembic.ini"), "0024_receipted_handoffs")


def _reset_to(engine, monkeypatch, revision: str) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(sa.text("DROP SCHEMA public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))
    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        engine.url.render_as_string(hide_password=False),
    )
    command.upgrade(Config("infra/postgres/alembic.ini"), revision)


def _seed(engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            session.add(Clinic(id=CLINIC_A, name="Handoff PostgreSQL A"))
        session.commit()
        with clinic_scope(session, CLINIC_B):
            session.add(Clinic(id=CLINIC_B, name="Handoff PostgreSQL B"))
        session.commit()
        with clinic_scope(session, CLINIC_A):
            session.add(
                Patient(
                    id="patient-handoff-pg-a",
                    clinic_id=CLINIC_A,
                    source_ref="P-HANDOFF-PG-A",
                    name="Synthetic A",
                )
            )
            session.add(
                Campaign(
                    id="campaign-handoff-pg-a",
                    clinic_id=CLINIC_A,
                    type=CampaignType.RECOVERY,
                    status=CampaignStatus.ACTIVE,
                )
            )
            session.add(
                ClinicPhoneNumber(
                    id="phone-handoff-pg-a",
                    clinic_id=CLINIC_A,
                    provider=ClinicPhoneProvider.TWILIO,
                    phone_number="opaque-clinic-route",
                    purpose=ClinicPhonePurpose.INBOUND,
                    status=ClinicPhoneStatus.ACTIVE,
                )
            )
            session.flush()
            session.add(
                OutreachJob(
                    id="job-handoff-pg-a",
                    clinic_id=CLINIC_A,
                    campaign_id="campaign-handoff-pg-a",
                    patient_id="patient-handoff-pg-a",
                    channel=Channel.CALL,
                    state=OutreachState.QUEUED,
                )
            )
            session.add(
                InboundCall(
                    id="call-handoff-pg-a",
                    clinic_id=CLINIC_A,
                    clinic_phone_number_id="phone-handoff-pg-a",
                    provider=ClinicPhoneProvider.TWILIO,
                    provider_call_id="CA11111111111111111111111111111111",
                    called_number="opaque-clinic-route",
                    caller_number_hash="sha256:synthetic-handoff-pg",
                    status=InboundCallStatus.STARTED,
                )
            )
        session.commit()


def test_postgres_0024_forces_rls_and_denies_cross_tenant_sql(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset(engine, monkeypatch)
    _seed(engine)
    with Session(engine, expire_on_commit=False) as session:
        escalate_to_staff(
            session,
            CLINIC_A,
            patient_id="patient-handoff-pg-a",
            outreach_job_id="job-handoff-pg-a",
            reason=EscalationReason.URGENT,
            now=NOW,
        )
        session.commit()

    with engine.connect() as connection:
        role = connection.execute(
            sa.text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        ).one()
        rls = connection.execute(
            sa.text(
                "SELECT relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE oid = 'handoff_receipt'::regclass"
            )
        ).one()
        policy = connection.scalar(
            sa.text(
                "SELECT policyname FROM pg_policies "
                "WHERE tablename = 'handoff_receipt'"
            )
        )
    assert tuple(role) == (False, False)
    assert tuple(rls) == (True, True)
    assert policy == "handoff_receipt_tenant_isolation"

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_B):
            assert session.execute(sa.text("SELECT id FROM handoff_receipt")).all() == []
            assert session.execute(
                sa.text("UPDATE handoff_receipt SET notification_count = 7")
            ).rowcount == 0
        session.rollback()


def test_postgres_0023_to_0024_backfills_without_notification_and_refuses_downgrade(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to(engine, monkeypatch, "0023_identity_evidence_tiers")
    _seed(engine)
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            session.add(
                Escalation(
                    id="escalation-pre-pr12",
                    clinic_id=CLINIC_A,
                    patient_id="patient-handoff-pg-a",
                    outreach_job_id="job-handoff-pg-a",
                    reason=EscalationReason.URGENT,
                )
            )
        session.commit()

    command.upgrade(Config("infra/postgres/alembic.ini"), "0024_receipted_handoffs")
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            receipt = session.scalar(
                sa.select(HandoffReceipt).where(
                    HandoffReceipt.escalation_id == "escalation-pre-pr12"
                )
            )
            effect_count = session.scalar(
                sa.select(sa.func.count())
                .select_from(ExternalEffect)
                .where(
                    ExternalEffect.effect_type
                    == ExternalEffectType.HANDOFF_NOTIFICATION
                )
            )
    assert receipt is not None
    assert receipt.severity.value == "critical"
    assert receipt.due_at == receipt.queued_at + timedelta(minutes=5)
    assert effect_count == 0

    with pytest.raises(
        RuntimeError,
        match="refuse PR-12 downgrade with retained receipt evidence",
    ):
        command.downgrade(
            Config("infra/postgres/alembic.ini"),
            "0023_identity_evidence_tiers",
        )
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0024_receipted_handoffs"
        )
        with pytest.raises(DBAPIError):
            with clinic_scope(session, CLINIC_B):
                session.execute(
                    sa.text(
                        "INSERT INTO handoff_receipt ("
                        "id, clinic_id, escalation_id, severity, delivery_state, "
                        "queued_at, due_at, policy_version, policy_sha256, "
                        "policy_critical_minutes, policy_high_minutes, "
                        "policy_normal_business_hours, severity_generation, "
                        "notification_count, escalation_level, alternate_state"
                        ") VALUES ("
                        "'receipt-cross-tenant', :clinic_id, 'missing', 'normal', "
                        "'queued', :now, :now, 'pilot-handoff-sla-v1', :hash, "
                        "5, 15, 4, 0, 0, 0, 'not_requested')"
                    ),
                    {"clinic_id": CLINIC_A, "now": NOW, "hash": "a" * 64},
                )
        session.rollback()


def test_postgres_concurrent_owner_creation_converges_to_one_receipt_and_effect(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset(engine, monkeypatch)
    _seed(engine)
    barrier = threading.Barrier(2)

    def create_escalation(_worker: int) -> str:
        with Session(engine, expire_on_commit=False) as session:
            barrier.wait()
            result = escalate_to_staff(
                session,
                CLINIC_A,
                patient_id="patient-handoff-pg-a",
                outreach_job_id="job-handoff-pg-a",
                reason=EscalationReason.AMBIGUOUS,
                now=NOW,
            )
            session.commit()
            return result.escalation_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner_ids = list(pool.map(create_escalation, (1, 2)))
    assert len(set(owner_ids)) == 1
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            assert session.scalar(sa.select(sa.func.count()).select_from(Escalation)) == 1
            assert session.scalar(sa.select(sa.func.count()).select_from(HandoffReceipt)) == 1
            assert session.scalar(
                sa.select(sa.func.count())
                .select_from(ExternalEffect)
                .where(
                    ExternalEffect.effect_type
                    == ExternalEffectType.HANDOFF_NOTIFICATION
                )
            ) == 1


def test_postgres_concurrent_inbound_creation_converges_to_one_receipt_and_effect(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset(engine, monkeypatch)
    _seed(engine)
    barrier = threading.Barrier(2)

    def create_task(_worker: int) -> str:
        with Session(engine, expire_on_commit=False) as session:
            barrier.wait()
            result = create_inbound_staff_task(
                session,
                CLINIC_A,
                inbound_call_id="call-handoff-pg-a",
                kind=InboundStaffTaskKind.ESCALATION,
                reason="clinical",
                now=NOW,
            )
            session.commit()
            return result.task_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner_ids = list(pool.map(create_task, (1, 2)))
    assert len(set(owner_ids)) == 1
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            assert session.scalar(
                sa.select(sa.func.count()).select_from(InboundStaffTask)
            ) == 1
            assert session.scalar(sa.select(sa.func.count()).select_from(HandoffReceipt)) == 1
            assert session.scalar(
                sa.select(sa.func.count())
                .select_from(ExternalEffect)
                .where(
                    ExternalEffect.effect_type
                    == ExternalEffectType.HANDOFF_NOTIFICATION
                )
            ) == 1


def test_postgres_severity_upgrade_racing_dispatch_keeps_highest_facts(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset(engine, monkeypatch)
    _seed(engine)
    with Session(engine, expire_on_commit=False) as session:
        result = escalate_to_staff(
            session,
            CLINIC_A,
            patient_id="patient-handoff-pg-a",
            outreach_job_id="job-handoff-pg-a",
            reason=EscalationReason.AMBIGUOUS,
            now=NOW,
        )
        with clinic_scope(session, CLINIC_A):
            effect = session.scalar(
                sa.select(ExternalEffect).where(
                    ExternalEffect.effect_type
                    == ExternalEffectType.HANDOFF_NOTIFICATION
                )
            )
            assert effect is not None
            effect.state = ExternalEffectState.DISPATCHING
            effect.lease_owner = "worker-racing-upgrade"
            effect.lease_expires_at = NOW + timedelta(minutes=5)
            effect.dispatch_started_at = NOW
        session.commit()

    barrier = threading.Barrier(2)

    def upgrade(reason: EscalationReason) -> EscalationReason:
        with Session(engine, expire_on_commit=False) as session:
            barrier.wait()
            upgraded = escalate_to_staff(
                session,
                CLINIC_A,
                patient_id="patient-handoff-pg-a",
                outreach_job_id="job-handoff-pg-a",
                reason=reason,
                now=(
                    NOW + timedelta(minutes=2)
                    if reason == EscalationReason.URGENT
                    else NOW + timedelta(minutes=1)
                ),
            )
            session.commit()
            return upgraded.reason

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(upgrade, (EscalationReason.CLINICAL, EscalationReason.URGENT)))
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            owner = session.get(Escalation, result.escalation_id)
            receipt = session.scalar(
                sa.select(HandoffReceipt).where(
                    HandoffReceipt.escalation_id == result.escalation_id
                )
            )
            effects = list(
                session.scalars(
                    sa.select(ExternalEffect).where(
                        ExternalEffect.effect_type
                        == ExternalEffectType.HANDOFF_NOTIFICATION
                    )
                )
            )
    assert owner is not None and owner.reason == EscalationReason.URGENT
    assert receipt is not None
    assert receipt.severity.value == "critical"
    assert receipt.queued_at == NOW
    assert receipt.due_at == NOW + timedelta(minutes=5)
    assert receipt.severity_generation in {1, 2}
    assert receipt.notification_count == receipt.severity_generation + 1
    assert len(effects) == receipt.notification_count
    assert len({effect.idempotency_key for effect in effects}) == len(effects)


def test_postgres_acknowledge_racing_resolve_preserves_both_facts(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset(engine, monkeypatch)
    _seed(engine)
    with Session(engine, expire_on_commit=False) as session:
        owner_id = escalate_to_staff(
            session,
            CLINIC_A,
            patient_id="patient-handoff-pg-a",
            outreach_job_id="job-handoff-pg-a",
            reason=EscalationReason.CLINICAL,
            now=NOW,
        ).escalation_id
        session.commit()
    barrier = threading.Barrier(2)

    def acknowledge() -> str:
        with Session(engine, expire_on_commit=False) as session:
            barrier.wait()
            try:
                acknowledge_queue_item(
                    session,
                    CLINIC_A,
                    f"escalation:{owner_id}",
                    staff_actor="staff:ack",
                    now=NOW + timedelta(seconds=1),
                )
                session.commit()
                return "acknowledged"
            except ValueError:
                session.rollback()
                return "already_resolved"

    def resolve() -> str:
        with Session(engine, expire_on_commit=False) as session:
            barrier.wait()
            resolve_queue_item(
                session,
                CLINIC_A,
                f"escalation:{owner_id}",
                QueueDecision.RESOLVE,
                staff_actor="staff:resolve",
                now=NOW + timedelta(seconds=1),
                pilot_gate=lambda *_args: True,
            )
            session.commit()
            return "resolved"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = {pool.submit(acknowledge), pool.submit(resolve)}
        results = {future.result() for future in outcomes}
    assert "resolved" in results
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            owner = session.get(Escalation, owner_id)
            receipt = session.scalar(
                sa.select(HandoffReceipt).where(HandoffReceipt.escalation_id == owner_id)
            )
    assert owner is not None and owner.status.value == "resolved"
    assert receipt is not None
    assert receipt.acknowledged_at is not None
    assert receipt.resolved_at is not None
    assert receipt.resolved_by == "staff:resolve"


def test_postgres_overlapping_ageing_requests_one_page_and_one_pause(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset(engine, monkeypatch)
    _seed(engine)
    with Session(engine, expire_on_commit=False) as session:
        create_programme(
            session,
            clinic_id=CLINIC_A,
            programme_id="programme-handoff-pg-a",
            environment="production",
            release_identity="sha256:handoff-pg",
        )
        escalate_to_staff(
            session,
            CLINIC_A,
            patient_id="patient-handoff-pg-a",
            outreach_job_id="job-handoff-pg-a",
            reason=EscalationReason.URGENT,
            now=NOW,
        )
        session.commit()
    barrier = threading.Barrier(2)

    def age(_worker: int):
        barrier.wait()
        return run_handoff_ageing_once(
            lambda: Session(engine, expire_on_commit=False),
            clinic_id=CLINIC_A,
            now=NOW + timedelta(minutes=6),
            enabled=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(age, (1, 2)))
    assert sum(result.alternate_requested for result in results) == 1
    assert sum(result.programmes_paused for result in results) == 1
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            programme = session.get(PilotProgramme, "programme-handoff-pg-a")
    assert programme is not None and programme.state == PilotProgrammeState.PAUSED