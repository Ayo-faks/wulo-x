"""Adversarial cross-clinic isolation tests for Phase 5."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from apps.artagent.backend.registries.toolstore import clinic_recall as clinic_tools
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.clinic_recall.availability import AvailabilitySlotInput, upsert_availability_slots
from src.clinic_recall.enums import (
    AppointmentStatus,
    AuditAction,
    CampaignStatus,
    CampaignType,
    Channel,
    ExternalEffectType,
    OutreachState,
)
from src.clinic_recall.models import (
    Appointment,
    AuditLog,
    AvailabilitySlot,
    Base,
    BookingAction,
    Campaign,
    Clinic,
    ExternalEffect,
    OutreachJob,
    Patient,
)
from src.clinic_recall.rights import (
    RightsPolicy,
    SubjectKey,
    SubjectKeyring,
    request_patient_erasure,
)

from tests.identity_evidence_support import grant_synthetic_t2

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def _factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        _seed(session, "clinic-a")
        _seed(session, "clinic-b")
        session.commit()
    return factory


def _seed(session: Session, clinic_id: str) -> None:
    session.add(Clinic(id=clinic_id, name=f"{clinic_id} Clinic", timezone="Europe/London"))
    session.add(
        Patient(
            id=f"patient-{clinic_id}",
            clinic_id=clinic_id,
            source_ref=f"P-{clinic_id}",
            name=f"{clinic_id} Patient",
            phone=f"+44770094{1 if clinic_id == 'clinic-a' else 2:04d}",
            email=f"{clinic_id}@example.test",
            consent_flags={"sms": True, "email": True, "call": True},
            opt_out_flags={},
        )
    )
    session.add(
        Campaign(
            id=f"campaign-{clinic_id}",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    session.add(
        Appointment(
            id=f"appointment-{clinic_id}",
            clinic_id=clinic_id,
            patient_id=f"patient-{clinic_id}",
            source_ref=f"A-{clinic_id}",
            status=AppointmentStatus.MISSED,
            start_at=NOW - timedelta(days=7),
            value=Decimal("80.00"),
        )
    )
    session.add(
        OutreachJob(
            id=f"job-{clinic_id}",
            clinic_id=clinic_id,
            campaign_id=f"campaign-{clinic_id}",
            patient_id=f"patient-{clinic_id}",
            appointment_id=f"appointment-{clinic_id}",
            channel=Channel.SMS,
            state=OutreachState.QUEUED,
        )
    )
    upsert_availability_slots(
        session,
        clinic_id,
        [
            AvailabilitySlotInput(
                source_ref=f"slot-{clinic_id}",
                start_at=NOW + timedelta(days=1),
                end_at=NOW + timedelta(days=1, minutes=30),
                source_provider="cliniko",
                business_id="920000001",
                clinician_id="930000001",
                appointment_type_id="940000001",
                fetched_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
            )
        ],
        now=NOW,
    )


@pytest.fixture
def tool_factory(monkeypatch) -> sessionmaker[Session]:
    factory = _factory()
    monkeypatch.setattr(clinic_tools, "get_sessionmaker", lambda: factory)
    return factory


def _tool_t2_args(tool_factory, monkeypatch, suffix: str) -> dict[str, str]:
    with tool_factory.begin() as session:
        identity_service, identity_context = grant_synthetic_t2(
            session,
            clinic_id="clinic-a",
            patient_id="patient-clinic-a",
            channel=Channel.CALL,
            now=NOW,
            suffix=suffix,
        )
    monkeypatch.setattr(
        clinic_tools,
        "runtime_identity_service",
        lambda _now: identity_service,
    )
    return {
        "_patient_id": "patient-clinic-a",
        "_identity_evidence_id": identity_context.evidence_id,
        "_identity_session_id": identity_context.session_id,
        "_identity_route_id": identity_context.route_id,
    }


@pytest.mark.asyncio
async def test_voice_tools_reject_foreign_patient_job_and_slot_ids(tool_factory):
    base_args = {
        "_clinic_id": "clinic-a",
        "_patient_id": "patient-clinic-b",
        "_outreach_job_id": "job-clinic-b",
        "now": NOW.isoformat(),
    }

    availability = await clinic_tools.get_availability(
        {
            "_clinic_id": "clinic-a",
            "window_start": NOW.isoformat(),
            "window_end": (NOW + timedelta(days=2)).isoformat(),
        }
    )
    book = await clinic_tools.book_slot({**base_args, "slot_id": "slot-does-not-exist"})
    reschedule = await clinic_tools.reschedule(
        {**base_args, "appointment_id": "appointment-clinic-b", "slot_id": "slot-does-not-exist"}
    )
    sms = await clinic_tools.send_sms({"_clinic_id": "clinic-a", "_outreach_job_id": "job-clinic-b"})
    email = await clinic_tools.send_email({"_clinic_id": "clinic-a", "_outreach_job_id": "job-clinic-b"})
    escalation = await clinic_tools.escalate_to_staff({**base_args, "reason": "clinical"})
    opt_out = await clinic_tools.record_opt_out(
        {"_clinic_id": "clinic-a", "_patient_id": "patient-clinic-b", "channel": "sms"}
    )

    assert availability == {"success": False, "error": "identity_t2_required"}
    for result in (book, reschedule, sms, email, escalation, opt_out):
        assert result["success"] is False


@pytest.mark.asyncio
async def test_log_outcome_rejects_foreign_outreach_job_before_audit(tool_factory):
    result = await clinic_tools.log_outcome(
        {
            "_clinic_id": "clinic-a",
            "_outreach_job_id": "job-clinic-b",
            "outcome": "declined",
            "summary": "foreign job attempt",
        }
    )

    assert result["success"] is False
    with tool_factory() as session:
        assert session.execute(select(func.count()).select_from(AuditLog)).scalar() == 0


@pytest.mark.asyncio
async def test_log_outcome_rejects_frozen_subject_before_audit_or_plaintext_log(
    tool_factory,
    caplog,
):
    with tool_factory.begin() as session:
        request_patient_erasure(
            session,
            clinic_id="clinic-a",
            patient_id="patient-clinic-a",
            confirm_token="ERASE patient-clinic-a",
            request_identity="tests-log-outcome-freeze",
            actor_role="dpo",
            actor_reference="tests-log-outcome-operator",
            keyring=SubjectKeyring(
                current=SubjectKey("tests-log-v1", b"tests-log-outcome-key")
            ),
            policy=RightsPolicy("tests-log-policy-v1", "a" * 64, timedelta(days=28)),
            now=NOW,
        )

    result = await clinic_tools.log_outcome(
        {
            "_clinic_id": "clinic-a",
            "_outreach_job_id": "job-clinic-a",
            "outcome": "declined",
            "summary": "must not reach logs",
        }
    )

    assert result["success"] is False
    assert result["error"] == "subject_frozen"
    assert "must not reach logs" not in caplog.text
    with tool_factory() as session:
        actions = session.execute(select(AuditLog.action)).scalars().all()
        assert actions.count(AuditAction.PLACE_CALL) == 0


@pytest.mark.asyncio
async def test_get_availability_localizes_naive_window_to_clinic_timezone(
    tool_factory,
    monkeypatch,
):
    identity_args = _tool_t2_args(tool_factory, monkeypatch, "availability-naive")
    result = await clinic_tools.get_availability(
        {
            "_clinic_id": "clinic-a",
            **identity_args,
            "now": NOW.isoformat(),
            "window_start": "2026-06-28T00:00:00",
            "window_end": "2026-06-29T00:00:00",
        }
    )

    assert result["success"] is True
    assert len(result["slots"]) == 1


@pytest.mark.asyncio
async def test_get_availability_naive_window_matches_aware_window(
    tool_factory,
    monkeypatch,
):
    identity_args = _tool_t2_args(tool_factory, monkeypatch, "availability-aware")
    naive = await clinic_tools.get_availability(
        {
            "_clinic_id": "clinic-a",
            **identity_args,
            "now": NOW.isoformat(),
            "window_start": "2026-06-28T00:00:00",
            "window_end": "2026-06-29T00:00:00",
        }
    )
    aware = await clinic_tools.get_availability(
        {
            "_clinic_id": "clinic-a",
            **identity_args,
            "now": NOW.isoformat(),
            "window_start": "2026-06-28T00:00:00+01:00",
            "window_end": "2026-06-29T00:00:00+01:00",
        }
    )

    assert naive["success"] is True and aware["success"] is True
    assert [s["slot_id"] for s in naive["slots"]] == [s["slot_id"] for s in aware["slots"]]


@pytest.mark.asyncio
async def test_get_availability_passes_timezone_aware_datetimes_to_core(tool_factory, monkeypatch):
    captured: dict[str, datetime] = {}
    identity_args = _tool_t2_args(tool_factory, monkeypatch, "availability-core")

    def fake_get_real_availability(session, clinic_id, *, now, window_start, window_end, **kwargs):
        captured["window_start"] = window_start
        captured["window_end"] = window_end
        return []

    monkeypatch.setattr(clinic_tools, "get_real_availability", fake_get_real_availability)

    result = await clinic_tools.get_availability(
        {
            "_clinic_id": "clinic-a",
            **identity_args,
            "now": NOW.isoformat(),
            "window_start": "2026-06-28T00:00:00",
            "window_end": "2026-06-29T00:00:00",
        }
    )

    assert result["success"] is True
    assert captured["window_start"].tzinfo is not None
    assert captured["window_start"].utcoffset() is not None
    assert captured["window_end"].tzinfo is not None
    assert captured["window_end"].utcoffset() is not None


@pytest.mark.asyncio
async def test_get_availability_rejects_model_supplied_practitioner(tool_factory):
    result = await clinic_tools.get_availability(
        {
            "_clinic_id": "clinic-a",
            "now": NOW.isoformat(),
            "window_start": NOW.isoformat(),
            "window_end": (NOW + timedelta(days=2)).isoformat(),
            "clinician_id": "930000001",
        }
    )

    assert result == {"success": False, "error": "clinician_filter_not_allowed"}


@pytest.mark.asyncio
async def test_book_tool_without_identity_policy_creates_no_action_or_effect(
    tool_factory,
    monkeypatch,
):
    monkeypatch.delenv("CLINIC_RECALL_DURABLE_CLINIKO_WRITE_ENABLED", raising=False)
    with tool_factory() as session:
        slot_id = session.execute(
            select(AvailabilitySlot.id).where(AvailabilitySlot.clinic_id == "clinic-a")
        ).scalar_one()

    result = await clinic_tools.book_slot(
        {
            "_clinic_id": "clinic-a",
            "_patient_id": "patient-clinic-a",
            "_outreach_job_id": "job-clinic-a",
            "slot_id": slot_id,
            "now": NOW.isoformat(),
        }
    )

    assert result["success"] is False
    assert result["error"] == "identity_t2_required"
    assert result["local_action_recorded"] is False
    assert result["write_back_state"] is None
    assert result["provider_confirmed"] is False
    assert result["staff_handoff_created"] is False
    with tool_factory() as session:
        assert session.scalar(select(func.count()).select_from(BookingAction)) == 0
        assert session.scalar(select(func.count()).select_from(ExternalEffect)) == 0


@pytest.mark.asyncio
async def test_book_tool_uses_server_write_switch_and_never_returns_provider_id(
    tool_factory,
    monkeypatch,
):
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_CLINIKO_WRITE_ENABLED", "true")
    with tool_factory() as session:
        slot_id = session.execute(
            select(AvailabilitySlot.id).where(AvailabilitySlot.clinic_id == "clinic-a")
        ).scalar_one()
        identity_service, identity_context = grant_synthetic_t2(
            session,
            clinic_id="clinic-a",
            patient_id="patient-clinic-a",
            channel=Channel.CALL,
            now=NOW,
            suffix="tool-write-switch",
        )
        session.commit()
    monkeypatch.setattr(
        clinic_tools,
        "runtime_identity_service",
        lambda _now: identity_service,
    )

    result = await clinic_tools.book_slot(
        {
            "_clinic_id": "clinic-a",
            "_patient_id": "patient-clinic-a",
            "_outreach_job_id": "job-clinic-a",
            "_identity_evidence_id": identity_context.evidence_id,
            "_identity_session_id": identity_context.session_id,
            "_identity_route_id": identity_context.route_id,
            "slot_id": slot_id,
            "now": NOW.isoformat(),
            "write_back_enabled": False,
            "provider_id": "950700999",
        }
    )

    assert result["success"] is True
    assert result["write_back_state"] == "pending"
    assert result["provider_confirmed"] is False
    assert result["staff_handoff_created"] is False
    assert "provider_id" not in result
    assert "950700999" not in str(result)
    with tool_factory() as session:
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        assert effect.payload["intent"] == "create"