"""Unit tests for consented call-record persistence and blob path helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.clinic_recall.enums import (
    CallRecordingStatus,
    ClinicPhoneProvider,
    ExternalEffectState,
    ExternalEffectType,
    InboundCallStatus,
    InteractionDirection,
    RecordingConsentState,
)
from src.clinic_recall.models import (
    Base,
    CallRecord,
    Clinic,
    ExternalEffect,
    InboundCall,
    Patient,
)
from src.clinic_recall.recording import (
    CallRecordError,
    RecordingBlobStore,
    RecordingStoreError,
    bind_call_record_provider_identity,
    ensure_call_record,
    finalize_call_transcript,
    mark_recording_failed,
    mark_recording_stored,
    recording_blob_path,
)

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(Clinic(id="clinic-a", name="Clinic A"))
        db.add(
            Patient(
                id="patient-a",
                clinic_id="clinic-a",
                source_ref="src-patient-a",
                name="Test Patient",
                phone="+447700900123",
                consent_flags={"recording": True},
            )
        )
        db.commit()
        yield db


def _ensure(session: Session) -> CallRecord:
    return ensure_call_record(
        session,
        "clinic-a",
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id="CA123",
        session_id="twilio-session-1",
        direction=InteractionDirection.OUTBOUND,
        scenario="rebooking",
        patient_id=None,
        consent_snapshot=None,
        now=NOW,
    )


def _seed_call_anchors(session: Session) -> tuple[ExternalEffect, InboundCall]:
    effect = ExternalEffect(
        id="effect-call-ledger",
        clinic_id="clinic-a",
        aggregate_type="outreach_job",
        aggregate_id="job-call-ledger",
        effect_type=ExternalEffectType.CALL,
        idempotency_key="call-ledger",
        payload_version=1,
        payload={"intent": "recall_fallback", "outreach_job_id": "job-call-ledger"},
        request_hash="a" * 64,
        state=ExternalEffectState.DISPATCHING,
        available_at=NOW,
        max_attempts=1,
    )
    inbound = InboundCall(
        id="inbound-call-ledger",
        clinic_id="clinic-a",
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id="CA" + "b" * 32,
        called_number="+441234567890",
        status=InboundCallStatus.STARTED,
    )
    session.add_all([effect, inbound])
    session.flush()
    return effect, inbound


def test_ensure_call_record_creates_unrecorded_all_call_ledger_row(session: Session) -> None:
    record = _ensure(session)
    session.commit()

    stored = session.execute(select(CallRecord)).scalar_one()
    assert stored.id == record.id
    assert stored.clinic_id == "clinic-a"
    assert stored.consent_state == RecordingConsentState.NOT_ASKED
    assert stored.recording_status == CallRecordingStatus.NONE
    assert stored.direction == InteractionDirection.OUTBOUND
    assert stored.patient_id is None
    assert stored.consent_snapshot is None
    assert stored.recording_sid is None
    assert stored.recording_blob_path is None
    assert stored.transcript is None
    assert stored.started_at is not None


def test_outbound_ledger_binds_call_effect_before_provider_identity(session: Session) -> None:
    effect, _inbound = _seed_call_anchors(session)
    record = ensure_call_record(
        session,
        "clinic-a",
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id=None,
        external_effect_id=effect.id,
        session_id=None,
        direction=InteractionDirection.OUTBOUND,
        scenario="rebooking",
        patient_id=None,
        consent_snapshot=None,
        now=NOW,
    )
    session.commit()

    assert record.external_effect_id == effect.id
    assert record.inbound_call_id is None
    assert record.provider_call_id is None

    bound = bind_call_record_provider_identity(
        session,
        clinic_id="clinic-a",
        call_record_id=record.id,
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id="CA" + "c" * 32,
    )
    replay = bind_call_record_provider_identity(
        session,
        clinic_id="clinic-a",
        call_record_id=record.id,
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id="CA" + "c" * 32,
    )
    assert replay is bound
    assert bound.provider_call_id == "CA" + "c" * 32

    with pytest.raises(CallRecordError, match="provider call identity conflict"):
        bind_call_record_provider_identity(
            session,
            clinic_id="clinic-a",
            call_record_id=record.id,
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id="CA" + "d" * 32,
        )


def test_inbound_ledger_is_idempotent_by_inbound_call_anchor(session: Session) -> None:
    _effect, inbound = _seed_call_anchors(session)
    first = ensure_call_record(
        session,
        "clinic-a",
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id=inbound.provider_call_id,
        inbound_call_id=inbound.id,
        session_id="twilio-inbound-ledger",
        direction=InteractionDirection.INBOUND,
        scenario="inbound_clinic",
        patient_id=None,
        consent_snapshot=None,
        now=NOW,
    )
    session.commit()
    second = ensure_call_record(
        session,
        "clinic-a",
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id=inbound.provider_call_id,
        inbound_call_id=inbound.id,
        session_id="twilio-inbound-ledger",
        direction=InteractionDirection.INBOUND,
        scenario="inbound_clinic",
        patient_id=None,
        consent_snapshot=None,
        now=NOW,
    )

    assert second is first
    assert first.inbound_call_id == inbound.id
    assert first.external_effect_id is None


def test_inbound_anchor_requires_matching_provider_identity(session: Session) -> None:
    _effect, inbound = _seed_call_anchors(session)

    with pytest.raises(CallRecordError, match="inbound call provider identity conflict"):
        ensure_call_record(
            session,
            "clinic-a",
            provider=ClinicPhoneProvider.ACS,
            provider_call_id=None,
            inbound_call_id=inbound.id,
            session_id="acs-inbound-ledger",
            direction=InteractionDirection.INBOUND,
            scenario="inbound_clinic",
            patient_id=None,
            consent_snapshot=None,
            now=NOW,
        )

    with pytest.raises(CallRecordError, match="inbound provider call identity conflict"):
        ensure_call_record(
            session,
            "clinic-a",
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id="CA" + "e" * 32,
            inbound_call_id=inbound.id,
            session_id="twilio-inbound-ledger",
            direction=InteractionDirection.INBOUND,
            scenario="inbound_clinic",
            patient_id=None,
            consent_snapshot=None,
            now=NOW,
        )


def test_patient_anchor_requires_same_tenant(session: Session) -> None:
    session.add(Clinic(id="clinic-b", name="Clinic B"))
    session.add(
        Patient(
            id="patient-b",
            clinic_id="clinic-b",
            source_ref="src-patient-b",
            name="Other Tenant Patient",
        )
    )
    session.flush()

    with pytest.raises(CallRecordError, match="patient anchor is invalid"):
        ensure_call_record(
            session,
            "clinic-a",
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id="CA" + "f" * 32,
            session_id="twilio-cross-tenant-patient",
            direction=InteractionDirection.OUTBOUND,
            scenario="rebooking",
            patient_id="patient-b",
            consent_snapshot=None,
            now=NOW,
        )


def test_ensure_call_record_is_idempotent(session: Session) -> None:
    first = _ensure(session)
    session.commit()
    second = _ensure(session)

    assert second.id == first.id
    assert len(session.execute(select(CallRecord)).scalars().all()) == 1


def test_finalize_call_transcript_attaches_turns(session: Session) -> None:
    _ensure(session)
    session.commit()

    turns = [
        {"role": "assistant", "text": "Hello, this is Clinic Recall.", "t": 1.2},
        {"role": "user", "text": "Hi, I'd like to rebook.", "t": 4.8},
    ]
    record = session.execute(select(CallRecord)).scalar_one()
    record.recording_status = CallRecordingStatus.IN_PROGRESS
    assert finalize_call_transcript(
        session,
        clinic_id="clinic-a",
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id="CA123",
        transcript=turns,
        ended_at=NOW,
    )
    session.commit()

    stored = session.execute(select(CallRecord)).scalar_one()
    assert stored.transcript == turns
    assert stored.ended_at is not None


def test_finalize_call_transcript_returns_false_without_row(session: Session) -> None:
    assert not finalize_call_transcript(
        session,
        clinic_id="clinic-a",
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id="CA-missing",
        transcript=[],
        ended_at=NOW,
    )


def test_finalize_unrecorded_call_sets_end_time_without_transcript(session: Session) -> None:
    record = _ensure(session)
    session.commit()

    assert finalize_call_transcript(
        session,
        clinic_id="clinic-a",
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id="CA123",
        transcript=None,
        ended_at=NOW,
    )
    session.commit()

    assert record.ended_at == NOW
    assert record.transcript is None
    assert record.recording_status == CallRecordingStatus.NONE


def test_mark_recording_stored_and_failed(session: Session) -> None:
    _ensure(session)
    session.commit()

    stored = mark_recording_stored(
        session,
        clinic_id="clinic-a",
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id="CA123",
        recording_sid="RE123",
        blob_path="clinic-a/CA123/RE123.wav",
        duration_s=58,
    )
    assert stored is not None
    assert stored.recording_status == CallRecordingStatus.STORED
    assert stored.recording_blob_path == "clinic-a/CA123/RE123.wav"
    assert stored.recording_duration_s == 58

    failed = mark_recording_failed(
        session,
        clinic_id="clinic-a",
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id="CA123",
        recording_sid="RE123",
    )
    assert failed is not None
    assert failed.recording_status == CallRecordingStatus.FAILED

    assert (
        mark_recording_stored(
            session,
            clinic_id="clinic-a",
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id="CA-missing",
            recording_sid="RE1",
            blob_path="x",
            duration_s=None,
        )
        is None
    )


def test_recording_blob_path_is_tenant_prefixed() -> None:
    assert recording_blob_path("clinic-a", "CA123", "RE456") == "clinic-a/CA123/RE456.wav"


def test_recording_blob_store_fails_closed_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("RECORDINGS_BLOB_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("RECORDINGS_BLOB_ACCOUNT_URL", raising=False)
    store = RecordingBlobStore()
    assert store.configured is False
    with pytest.raises(RecordingStoreError):
        store.upload("clinic-a/CA123/RE1.wav", b"audio")
