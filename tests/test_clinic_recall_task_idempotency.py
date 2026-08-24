from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from src.clinic_recall.enums import (
    CampaignStatus,
    CampaignType,
    Channel,
    ClinicPhoneProvider,
    EscalationReason,
    ExternalEffectType,
    HandoffSeverity,
    InboundStaffTaskKind,
    OutreachState,
)
from src.clinic_recall.escalation import escalate_to_staff
from src.clinic_recall.inbound_staff_tasks import create_inbound_staff_task
from src.clinic_recall.models import (
    AuditLog,
    Base,
    Campaign,
    Clinic,
    Escalation,
    ExternalEffect,
    HandoffReceipt,
    InboundCall,
    InboundStaffTask,
    Interaction,
    OutreachJob,
    Patient,
)
from src.clinic_recall.rights import (
    RightsPolicy,
    SubjectFrozenError,
    SubjectKey,
    SubjectKeyring,
    request_patient_erasure,
)

NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
MIGRATION = Path("infra/postgres/migrations/versions/0013_recall_task_idempotency.py")


def _factory() -> sessionmaker[Session]:
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(Clinic(id="clinic-a", name="Clinic A"))
        session.add(
            Patient(
                id="patient-a",
                clinic_id="clinic-a",
                source_ref="patient-a",
                name="Synthetic Patient",
                consent_flags={"call": True},
                opt_out_flags={},
            )
        )
        session.add(
            Campaign(
                id="campaign-a",
                clinic_id="clinic-a",
                type=CampaignType.RECOVERY,
                status=CampaignStatus.ACTIVE,
            )
        )
        session.flush()
        session.add(
            OutreachJob(
                id="job-a",
                clinic_id="clinic-a",
                campaign_id="campaign-a",
                patient_id="patient-a",
                channel=Channel.CALL,
                state=OutreachState.SENT,
            )
        )
        session.add(
            InboundCall(
                id="call-a",
                clinic_id="clinic-a",
                provider=ClinicPhoneProvider.TWILIO,
                provider_call_id="provider-call-a",
                called_number="+15551230000",
            )
        )
    return factory


def test_0013_schema_contract() -> None:
    assert "outreach_job_id" in Escalation.__table__.c
    assert {
        "uq_inbound_staff_task_active_call_kind",
        "uq_inbound_staff_task_active_message_kind",
    } <= {index.name for index in InboundStaffTask.__table__.indexes}
    assert "uq_escalation_active_outreach_job" in {
        index.name for index in Escalation.__table__.indexes
    }
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0013_recall_task_idempotency"' in source
    assert 'down_revision: str | None = "0012_call_records"' in source
    assert "row_number() OVER" in source
    assert "enum values are intentionally irreversible" in source


def test_active_inbound_task_replay_upgrades_once() -> None:
    factory = _factory()
    with factory.begin() as session:
        first = create_inbound_staff_task(
            session,
            "clinic-a",
            inbound_call_id="call-a",
            kind=InboundStaffTaskKind.ESCALATION,
            now=NOW,
            priority="normal",
            reason="ambiguous",
        )
        upgraded = create_inbound_staff_task(
            session,
            "clinic-a",
            inbound_call_id="call-a",
            kind=InboundStaffTaskKind.ESCALATION,
            now=NOW,
            priority="high",
            reason="urgent",
        )

    assert first.created is True
    assert upgraded.task_id == first.task_id
    assert upgraded.idempotent is True
    assert upgraded.upgraded is True
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(InboundStaffTask)) == 1
        assert session.scalar(select(func.count()).select_from(AuditLog)) == 1
        receipt = session.scalar(select(HandoffReceipt))
        assert receipt is not None
        assert receipt.inbound_staff_task_id == first.task_id
        assert receipt.severity == HandoffSeverity.CRITICAL
        assert receipt.severity_generation == 1
        assert session.scalar(
            select(func.count())
            .select_from(ExternalEffect)
            .where(ExternalEffect.effect_type == ExternalEffectType.HANDOFF_NOTIFICATION)
        ) == 2


def test_active_escalation_replay_upgrades_once() -> None:
    factory = _factory()
    with factory.begin() as session:
        first = escalate_to_staff(
            session,
            "clinic-a",
            patient_id="patient-a",
            outreach_job_id="job-a",
            reason=EscalationReason.AMBIGUOUS,
            now=NOW,
        )
        upgraded = escalate_to_staff(
            session,
            "clinic-a",
            patient_id="patient-a",
            outreach_job_id="job-a",
            reason=EscalationReason.URGENT,
            now=NOW,
        )

    assert first.created is True
    assert upgraded.escalation_id == first.escalation_id
    assert upgraded.idempotent is True
    assert upgraded.upgraded is True
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Escalation)) == 1
        assert session.scalar(select(func.count()).select_from(Interaction)) == 1
        assert session.scalar(select(func.count()).select_from(AuditLog)) == 1
        receipt = session.scalar(select(HandoffReceipt))
        assert receipt is not None
        assert receipt.escalation_id == first.escalation_id
        assert receipt.severity == HandoffSeverity.CRITICAL
        assert receipt.severity_generation == 1
        assert session.scalar(
            select(func.count())
            .select_from(ExternalEffect)
            .where(ExternalEffect.effect_type == ExternalEffectType.HANDOFF_NOTIFICATION)
        ) == 2


def test_frozen_subject_rejects_escalation_and_inbound_patient_association() -> None:
    factory = _factory()
    with factory.begin() as session:
        request_patient_erasure(
            session,
            clinic_id="clinic-a",
            patient_id="patient-a",
            confirm_token="ERASE patient-a",
            request_identity="tests-task-freeze",
            actor_role="dpo",
            actor_reference="tests-task-operator",
            keyring=SubjectKeyring(
                current=SubjectKey("tests-task-v1", b"tests-task-freeze-key")
            ),
            policy=RightsPolicy("tests-task-policy-v1", "a" * 64, timedelta(days=28)),
            now=NOW,
        )
        with pytest.raises(SubjectFrozenError, match="subject_frozen"):
            escalate_to_staff(
                session,
                "clinic-a",
                patient_id="patient-a",
                outreach_job_id="job-a",
                reason=EscalationReason.URGENT,
                now=NOW,
                context="must not persist",
            )
        with pytest.raises(SubjectFrozenError, match="subject_frozen"):
            create_inbound_staff_task(
                session,
                "clinic-a",
                inbound_call_id="call-a",
                patient_id="patient-a",
                kind=InboundStaffTaskKind.ESCALATION,
                now=NOW,
                reason="urgent",
            )

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Escalation)) == 0
        assert session.scalar(select(func.count()).select_from(Interaction)) == 0
        assert session.scalar(select(func.count()).select_from(InboundStaffTask)) == 0


def test_frozen_subject_rejects_upgrade_of_existing_linked_inbound_task() -> None:
    factory = _factory()
    with factory.begin() as session:
        created = create_inbound_staff_task(
            session,
            "clinic-a",
            inbound_call_id="call-a",
            patient_id="patient-a",
            kind=InboundStaffTaskKind.ESCALATION,
            now=NOW,
            priority="normal",
            reason="ambiguous",
            summary="pre-freeze summary",
        )
        request_patient_erasure(
            session,
            clinic_id="clinic-a",
            patient_id="patient-a",
            confirm_token="ERASE patient-a",
            request_identity="tests-task-replay-freeze",
            actor_role="dpo",
            actor_reference="tests-task-replay-operator",
            keyring=SubjectKeyring(
                current=SubjectKey("tests-task-replay-v1", b"tests-task-replay-key")
            ),
            policy=RightsPolicy(
                "tests-task-replay-policy-v1",
                "a" * 64,
                timedelta(days=28),
            ),
            now=NOW,
        )
        with pytest.raises(SubjectFrozenError, match="subject_frozen"):
            create_inbound_staff_task(
                session,
                "clinic-a",
                inbound_call_id="call-a",
                kind=InboundStaffTaskKind.ESCALATION,
                now=NOW,
                priority="high",
                reason="urgent",
                summary="must not replace summary",
            )

    with factory() as session:
        task = session.get(InboundStaffTask, created.task_id)
        assert task is not None
        assert task.priority == "normal"
        assert task.reason == "ambiguous"
        assert task.summary == "pre-freeze summary"