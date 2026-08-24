"""Deterministic per-call recording-consent contracts for PR-09."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from src.clinic_recall.enums import (
    CallRecordingStatus,
    ClinicPhoneProvider,
    ExternalEffectType,
    InteractionDirection,
    RecordingConsentState,
)
from src.clinic_recall.models import Base, CallRecord, Clinic, ExternalEffect, Patient
from src.clinic_recall.recording import (
    RecordingConsentError,
    RecordingDisclosure,
    ensure_call_record,
    finalize_call_transcript,
    mark_recording_consent_asked,
    parse_recording_consent,
    record_recording_consent_evidence,
    recording_disclosure_from_environment,
    withdraw_recording_consent,
)
from src.clinic_recall.rights import (
    RightsPolicy,
    SubjectFrozenError,
    SubjectKey,
    SubjectKeyring,
    request_patient_erasure,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
DISCLOSURE = RecordingDisclosure(
    text="Synthetic AI purpose and recording disclosure. Do you agree, yes or no?",
    version="synthetic-pr09-v1",
)


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add(Clinic(id="clinic-a", name="Clinic A"))
        db.commit()
        yield db


def _ledger(session: Session) -> CallRecord:
    return ensure_call_record(
        session,
        "clinic-a",
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id="CA" + "a" * 32,
        session_id="twilio-session-consent",
        direction=InteractionDirection.INBOUND,
        scenario="inbound_clinic",
        patient_id=None,
        consent_snapshot=None,
        now=NOW,
    )


@pytest.mark.parametrize(
    ("text", "dtmf", "confidence", "expected"),
    [
        ("yes", None, 0.95, RecordingConsentState.GRANTED),
        ("yes please", None, 0.95, RecordingConsentState.GRANTED),
        ("I consent", None, 0.95, RecordingConsentState.GRANTED),
        ("no", None, 0.95, RecordingConsentState.DECLINED),
        ("no thank you", None, 0.95, RecordingConsentState.DECLINED),
        (None, "1", None, RecordingConsentState.GRANTED),
        (None, "2", None, RecordingConsentState.DECLINED),
        (None, None, None, RecordingConsentState.AMBIGUOUS),
        ("yes", None, 0.4, RecordingConsentState.AMBIGUOUS),
        ("maybe yes", None, 0.95, RecordingConsentState.AMBIGUOUS),
        ("yes", "2", 0.95, RecordingConsentState.AMBIGUOUS),
    ],
)
def test_strict_recording_consent_parser(
    text: str | None,
    dtmf: str | None,
    confidence: float | None,
    expected: RecordingConsentState,
) -> None:
    assert parse_recording_consent(text=text, dtmf=dtmf, confidence=confidence) == expected


def test_disclosure_configuration_fails_closed_until_complete_and_approved() -> None:
    assert recording_disclosure_from_environment({}) is None
    assert (
        recording_disclosure_from_environment(
            {
                "CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT": DISCLOSURE.text,
                "CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION": DISCLOSURE.version,
                "CLINIC_RECALL_RECORDING_DISCLOSURE_APPROVED": "false",
            }
        )
        is None
    )
    assert (
        recording_disclosure_from_environment(
            {
                "CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT": DISCLOSURE.text,
                "CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION": DISCLOSURE.version,
                "CLINIC_RECALL_RECORDING_DISCLOSURE_APPROVED": "true",
            }
        )
        == DISCLOSURE
    )
    assert (
        recording_disclosure_from_environment(
            {
                "AZURE_APPCONFIG_ENDPOINT": "https://config.example.test",
                "CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT": DISCLOSURE.text,
                "CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION": DISCLOSURE.version,
                "CLINIC_RECALL_RECORDING_DISCLOSURE_APPROVED": "true",
            }
        )
        is None
    )
    assert (
        recording_disclosure_from_environment(
            {
                "AZURE_APPCONFIG_ENDPOINT": "https://config.example.test",
                "CLINIC_RECALL_RECORDING_DISCLOSURE_REFRESHED_AT": NOW.isoformat(),
                "CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS": "60",
                "CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT": DISCLOSURE.text,
                "CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION": DISCLOSURE.version,
                "CLINIC_RECALL_RECORDING_DISCLOSURE_APPROVED": "true",
            },
            now=NOW,
        )
        == DISCLOSURE
    )
    for refreshed_at in (
        "not-a-timestamp",
        (NOW - timedelta(seconds=61)).isoformat(),
        (NOW + timedelta(seconds=1)).isoformat(),
    ):
        assert (
            recording_disclosure_from_environment(
                {
                    "AZURE_APPCONFIG_ENDPOINT": "https://config.example.test",
                    "CLINIC_RECALL_RECORDING_DISCLOSURE_REFRESHED_AT": refreshed_at,
                    "CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS": "60",
                    "CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT": DISCLOSURE.text,
                    "CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION": DISCLOSURE.version,
                    "CLINIC_RECALL_RECORDING_DISCLOSURE_APPROVED": "true",
                },
                now=NOW,
            )
            is None
        )


def test_consent_state_is_minimized_and_raw_answer_is_never_persisted(session: Session) -> None:
    record = _ledger(session)
    mark_recording_consent_asked(
        session,
        clinic_id="clinic-a",
        call_record_id=record.id,
        disclosure=DISCLOSURE,
        source="twilio_gather",
        now=NOW,
    )
    decided = record_recording_consent_evidence(
        session,
        clinic_id="clinic-a",
        call_record_id=record.id,
        text="yes please",
        dtmf=None,
        confidence=0.99,
        source="speech",
        now=NOW + timedelta(seconds=4),
    )

    assert decided.consent_state == RecordingConsentState.GRANTED
    assert decided.consent_asked_at == NOW
    assert decided.consent_decided_at == NOW + timedelta(seconds=4)
    assert decided.consent_decision_source == "speech"
    assert decided.consent_version == DISCLOSURE.version
    assert decided.recording_status == CallRecordingStatus.NONE
    persisted_values = {
        column.name: getattr(decided, column.name) for column in CallRecord.__table__.columns
    }
    assert "yes please" not in str(persisted_values).lower()
    assert DISCLOSURE.text not in str(persisted_values)


def test_correction_is_allowed_only_before_recording_authority(session: Session) -> None:
    record = _ledger(session)
    mark_recording_consent_asked(
        session,
        clinic_id="clinic-a",
        call_record_id=record.id,
        disclosure=DISCLOSURE,
        source="twilio_gather",
        now=NOW,
    )
    ambiguous = record_recording_consent_evidence(
        session,
        clinic_id="clinic-a",
        call_record_id=record.id,
        text="not sure",
        dtmf=None,
        confidence=0.9,
        source="speech",
        now=NOW + timedelta(seconds=2),
    )
    assert ambiguous.consent_state == RecordingConsentState.AMBIGUOUS

    corrected = record_recording_consent_evidence(
        session,
        clinic_id="clinic-a",
        call_record_id=record.id,
        text=None,
        dtmf="1",
        confidence=None,
        source="dtmf",
        now=NOW + timedelta(seconds=3),
        correction=True,
    )
    assert corrected.consent_state == RecordingConsentState.GRANTED

    corrected.recording_requested_at = NOW + timedelta(seconds=4)
    with pytest.raises(RecordingConsentError, match="recording authority"):
        record_recording_consent_evidence(
            session,
            clinic_id="clinic-a",
            call_record_id=record.id,
            text="no",
            dtmf=None,
            confidence=0.99,
            source="speech",
            now=NOW + timedelta(seconds=5),
            correction=True,
        )


def test_consent_decision_replay_is_idempotent(session: Session) -> None:
    record = _ledger(session)
    mark_recording_consent_asked(
        session,
        clinic_id="clinic-a",
        call_record_id=record.id,
        disclosure=DISCLOSURE,
        source="twilio_gather",
        now=NOW,
    )
    first = record_recording_consent_evidence(
        session,
        clinic_id="clinic-a",
        call_record_id=record.id,
        text=None,
        dtmf="2",
        confidence=None,
        source="dtmf",
        now=NOW + timedelta(seconds=2),
    )
    replay = record_recording_consent_evidence(
        session,
        clinic_id="clinic-a",
        call_record_id=record.id,
        text=None,
        dtmf="2",
        confidence=None,
        source="dtmf",
        now=NOW + timedelta(seconds=8),
    )

    assert replay is first
    assert replay.consent_state == RecordingConsentState.DECLINED
    assert replay.consent_decided_at == NOW + timedelta(seconds=2)


def test_withdrawal_commits_one_exact_recording_stop_effect(session: Session) -> None:
    record = _ledger(session)
    record.consent_state = RecordingConsentState.GRANTED
    record.consent_version = DISCLOSURE.version
    record.recording_sid = "RE" + "b" * 32
    record.recording_status = CallRecordingStatus.IN_PROGRESS
    record.recording_started_at = NOW
    session.commit()

    first = withdraw_recording_consent(
        session,
        clinic_id="clinic-a",
        call_record_id=record.id,
        source="dtmf",
        now=NOW + timedelta(seconds=10),
    )
    replay = withdraw_recording_consent(
        session,
        clinic_id="clinic-a",
        call_record_id=record.id,
        source="dtmf",
        now=NOW + timedelta(seconds=12),
    )
    session.commit()

    effect = session.query(ExternalEffect).filter_by(effect_type=ExternalEffectType.RECORDING).one()
    assert replay is first
    assert first.consent_state == RecordingConsentState.WITHDRAWN
    assert first.consent_decision_source.value == "dtmf"
    assert first.recording_status == CallRecordingStatus.STOP_PENDING
    assert first.recording_stop_requested_at == NOW + timedelta(seconds=10)
    assert effect.max_attempts == 1
    assert effect.payload == {
        "intent": "recording_stop",
        "call_record_id": record.id,
    }
    assert session.query(ExternalEffect).count() == 1


def test_frozen_subject_rejects_new_call_ledger_and_consent_content(
    session: Session,
) -> None:
    session.add(
        Patient(
            id="patient-a",
            clinic_id="clinic-a",
            source_ref="P-A",
            name="Synthetic Patient",
        )
    )
    session.flush()
    record = ensure_call_record(
        session,
        "clinic-a",
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id="CA" + "b" * 32,
        session_id="session-before-freeze",
        direction=InteractionDirection.OUTBOUND,
        scenario="rebooking",
        patient_id="patient-a",
        consent_snapshot=None,
        now=NOW,
    )
    mark_recording_consent_asked(
        session,
        clinic_id="clinic-a",
        call_record_id=record.id,
        disclosure=DISCLOSURE,
        source="twilio_gather",
        now=NOW,
    )
    request_patient_erasure(
        session,
        clinic_id="clinic-a",
        patient_id="patient-a",
        confirm_token="ERASE patient-a",
        request_identity="tests-recording-freeze",
        actor_role="dpo",
        actor_reference="tests-recording-operator",
        keyring=SubjectKeyring(
            current=SubjectKey("tests-recording-v1", b"tests-recording-freeze-key")
        ),
        policy=RightsPolicy("tests-recording-policy-v1", "a" * 64, timedelta(days=28)),
        now=NOW,
    )

    with pytest.raises(SubjectFrozenError, match="subject_frozen"):
        record_recording_consent_evidence(
            session,
            clinic_id="clinic-a",
            call_record_id=record.id,
            text="yes",
            dtmf=None,
            confidence=0.99,
            source="speech",
            now=NOW + timedelta(seconds=1),
        )
    with pytest.raises(SubjectFrozenError, match="subject_frozen"):
        ensure_call_record(
            session,
            "clinic-a",
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id="CA" + "c" * 32,
            session_id="session-after-freeze",
            direction=InteractionDirection.OUTBOUND,
            scenario="rebooking",
            patient_id="patient-a",
            consent_snapshot=None,
            now=NOW + timedelta(seconds=1),
        )

    record.recording_status = CallRecordingStatus.COMPLETED
    assert finalize_call_transcript(
        session,
        clinic_id="clinic-a",
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id=record.provider_call_id or "",
        transcript=[{"speaker": "patient", "text": "must not persist"}],
        ended_at=NOW + timedelta(seconds=2),
    )
    assert record.ended_at == NOW + timedelta(seconds=2)
    assert record.transcript is None
