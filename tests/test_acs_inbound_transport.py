from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from apps.artagent.backend.api.v1.handlers import acs_call_lifecycle
from apps.artagent.backend.api.v1.handlers.acs_call_lifecycle import ACSLifecycleHandler
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.clinic_recall.enums import (
    CallRecordingStatus,
    ClinicPhoneProvider,
    ClinicPhonePurpose,
    ClinicPhoneStatus,
    RecordingConsentState,
)
from src.clinic_recall.models import (
    Base,
    CallRecord,
    Clinic,
    ClinicPhoneNumber,
    InboundCall,
    Patient,
)
from src.clinic_recall.pilot_controls import (
    create_programme,
    enroll_participant,
    mark_programme_dark,
    release_cumulative_limit,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


class _Span:
    def is_recording(self) -> bool:
        return False

    def set_status(self, _status) -> None:
        pass


class _AcsCaller:
    async def answer_incoming_call(self, *, incoming_call_context: str, stream_mode):
        assert incoming_call_context == "incoming-token"
        return SimpleNamespace(call_connection_id="acs-connection-1")


class _ConnManager:
    def __init__(self) -> None:
        self.contexts: dict[str, dict[str, str]] = {}

    async def set_call_context(self, call_id: str, context: dict[str, str]) -> None:
        self.contexts[call_id] = context


def _factory() -> sessionmaker[Session]:
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
            ClinicPhoneNumber(
                id="phone-acs-a",
                clinic_id="clinic-a",
                provider=ClinicPhoneProvider.ACS,
                phone_number="+15551230000",
                purpose=ClinicPhonePurpose.INBOUND,
                status=ClinicPhoneStatus.ACTIVE,
                config={"record_call": True},
            )
        )
    return factory


async def test_acs_inbound_call_sets_provider_switch_context(monkeypatch) -> None:
    factory = _factory()
    monkeypatch.setattr(acs_call_lifecycle, "get_sessionmaker", lambda: factory)
    conn_manager = _ConnManager()
    app_state = SimpleNamespace(conn_manager=conn_manager)

    response = await ACSLifecycleHandler()._handle_incoming_call(
        {
            "incomingCallContext": "incoming-token",
            "correlationId": "corr-123",
            "from": {"kind": "phoneNumber", "phoneNumber": {"value": "+15559991111"}},
            "to": {"kind": "phoneNumber", "phoneNumber": {"value": "+1 (555) 123-0000"}},
            "serverCallId": "server-call",
        },
        _AcsCaller(),
        _Span(),
        app_state=app_state,
    )

    assert response.status_code == 200
    context = conn_manager.contexts["acs-connection-1"]
    assert context["source"] == "clinic_recall_inbound"
    assert context["provider"] == "acs"
    assert context["scenario"] == "inbound_clinic"
    assert context["clinic_id"] == "clinic-a"
    assert context["called_number_id"] == "phone-acs-a"
    assert context["called_number"] == "+15551230000"
    assert context["provider_call_id"] == "acs-connection-1"
    assert context["caller_number_hash"].startswith("sha256:")
    assert context["record_call"] == "false"
    with factory() as session:
        call = session.query(InboundCall).one()
        assert call.provider == ClinicPhoneProvider.ACS
        assert call.provider_call_id == "corr-123"
        call_record = session.query(CallRecord).one()
        assert call_record.provider == ClinicPhoneProvider.ACS
        assert call_record.provider_call_id == "acs-connection-1"


async def test_acs_inbound_recording_stays_off_without_pr09_per_call_consent(
    monkeypatch,
) -> None:
    factory = _factory()
    with factory.begin() as session:
        for ordinal in range(1, 6):
            session.add(
                Patient(
                    id=f"patient-acs-recording-{ordinal}",
                    clinic_id="clinic-a",
                    source_ref=f"patient-acs-recording-{ordinal}",
                    name=f"Synthetic ACS Recording {ordinal}",
                )
            )
        programme = create_programme(
            session,
            clinic_id="clinic-a",
            programme_id="pilot-acs-recording",
            environment="production",
            release_identity="sha256:acs-recording",
        )
        mark_programme_dark(
            session,
            clinic_id="clinic-a",
            programme_id=programme.id,
            actor="operator:test",
            evidence_hash="d" * 64,
            now=NOW - timedelta(minutes=1),
        )
        session.flush()
        for ordinal in range(1, 6):
            enroll_participant(
                session,
                clinic_id="clinic-a",
                programme_id=programme.id,
                patient_id=f"patient-acs-recording-{ordinal}",
                now=NOW,
            )
        release_cumulative_limit(
            session,
            clinic_id="clinic-a",
            programme_id=programme.id,
            cumulative_limit=5,
            actor="operator:test",
            evidence_hash="a" * 64,
            now=NOW,
        )
    monkeypatch.setattr(acs_call_lifecycle, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("CLINIC_RECALL_PILOT_OUTREACH_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_VOICE_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_RECORDING_ENABLED", "true")
    monkeypatch.setenv(
        "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT",
        (datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
    )
    monkeypatch.setenv("CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS", "60")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "CLINIC_RECALL_PILOT_RELEASE_IDENTITY",
        "sha256:acs-recording",
    )
    conn_manager = _ConnManager()

    response = await ACSLifecycleHandler()._handle_incoming_call(
        {
            "incomingCallContext": "incoming-token",
            "correlationId": "corr-recording",
            "from": {"kind": "phoneNumber", "phoneNumber": {"value": "+15559991111"}},
            "to": {"kind": "phoneNumber", "phoneNumber": {"value": "+15551230000"}},
        },
        _AcsCaller(),
        _Span(),
        record_call=True,
        app_state=SimpleNamespace(conn_manager=conn_manager),
    )

    assert response.status_code == 200
    assert conn_manager.contexts["acs-connection-1"]["record_call"] == "false"
    with factory() as session:
        ledger = session.query(CallRecord).one()
        assert ledger.provider == ClinicPhoneProvider.ACS
        assert ledger.consent_state == RecordingConsentState.NOT_ASKED
        assert ledger.recording_status == CallRecordingStatus.NONE
        assert ledger.recording_sid is None
