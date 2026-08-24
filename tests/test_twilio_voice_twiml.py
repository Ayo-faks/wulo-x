from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest
from apps.artagent.backend.api.v1.endpoints import voice
from apps.artagent.backend.api.v1.endpoints.sms import _make_twilio_signature
from apps.artagent.backend.api.v1.endpoints.voice import router
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.clinic_recall.durable.callbacks import generate_effect_token
from src.clinic_recall.durable.enqueue import enqueue_call_effect
from src.clinic_recall.enums import (
    CallRecordingStatus,
    CampaignStatus,
    CampaignType,
    Channel,
    ClinicPhoneProvider,
    ClinicPhonePurpose,
    ClinicPhoneStatus,
    ExternalEffectState,
    ExternalEffectType,
    InteractionDirection,
    OutreachState,
    PilotProgrammeState,
    RecordingConsentState,
)
from src.clinic_recall.models import (
    Base,
    CallRecord,
    Campaign,
    Clinic,
    ClinicPhoneNumber,
    ExternalEffect,
    InboundCall,
    OutreachJob,
    Patient,
    PilotProgramme,
    ProviderCallbackReceipt,
)
from src.clinic_recall.pilot_controls import (
    create_programme,
    enroll_participant,
    mark_programme_dark,
    release_cumulative_limit,
)

NOW = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
ACCOUNT_SID = "AC" + "a" * 32
CALL_SID = "CA" + "b" * 32
DISCLOSURE_TEXT = (
    "This call may be recorded for service quality. Say yes or no, or press 1 for yes and 2 for no."
)


def _voice_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/voice")
    return app


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_phone_route(
    factory: sessionmaker[Session], *, status: ClinicPhoneStatus = ClinicPhoneStatus.ACTIVE
) -> None:
    with factory.begin() as session:
        session.add(Clinic(id="clinic-a", name="Clinic A"))
        session.add(
            ClinicPhoneNumber(
                id="phone-a",
                clinic_id="clinic-a",
                provider=ClinicPhoneProvider.TWILIO,
                phone_number="+15551230000",
                purpose=ClinicPhonePurpose.INBOUND,
                status=status,
            )
        )


def _enable_recording_consent(
    monkeypatch: pytest.MonkeyPatch,
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        session.add(
            PilotProgramme(
                id="pilot-recording-consent",
                clinic_id="clinic-a",
                environment="production",
                release_identity="sha256:recording-consent",
                state=PilotProgrammeState.ACTIVE,
                active_cumulative_limit=5,
                released_at=NOW,
                released_by="operator:test",
                release_evidence_hash="a" * 64,
            )
        )
    monkeypatch.setenv("CLINIC_RECALL_PILOT_OUTREACH_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_VOICE_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_RECORDING_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RECORDING_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RECORDING_PROVIDER", "twilio")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "CLINIC_RECALL_PILOT_RELEASE_IDENTITY",
        "sha256:recording-consent",
    )
    monkeypatch.setenv(
        "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT",
        datetime.now(UTC).isoformat(),
    )
    monkeypatch.setenv("CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS", "60")
    monkeypatch.setenv("CLINIC_RECALL_RECORDING_DISCLOSURE_APPROVED", "true")
    monkeypatch.setenv("CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT", DISCLOSURE_TEXT)
    monkeypatch.setenv(
        "CLINIC_RECALL_RECORDING_DISCLOSURE_REFRESHED_AT",
        datetime.now(UTC).isoformat(),
    )
    monkeypatch.setenv(
        "CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION",
        "synthetic-pr09-v1",
    )


def _durable_session_factory() -> tuple[sessionmaker[Session], str]:
    factory = _session_factory()
    clinic_id = "clinic-durable-twiml"
    with factory.begin() as session:
        session.add(
            Clinic(
                id=clinic_id,
                name="Durable TwiML Clinic",
                timezone="Europe/London",
            )
        )
        session.add(Clinic(id="clinic-durable-twiml-other", name="Other Clinic"))
        session.add(
            Patient(
                id="patient-durable-twiml",
                clinic_id=clinic_id,
                source_ref="patient-durable-twiml",
                name="Synthetic Durable TwiML Patient",
                phone="+447700900301",
                consent_flags={"call": True},
                opt_out_flags={},
            )
        )
        session.add(
            Campaign(
                id="campaign-durable-twiml",
                clinic_id=clinic_id,
                type=CampaignType.RECOVERY,
                status=CampaignStatus.ACTIVE,
            )
        )
        session.add(
            OutreachJob(
                id="job-durable-twiml",
                clinic_id=clinic_id,
                campaign_id="campaign-durable-twiml",
                patient_id="patient-durable-twiml",
                channel=Channel.SMS,
                state=OutreachState.NO_REPLY,
            )
        )
        session.flush()
        effect, _ = enqueue_call_effect(
            session,
            clinic_id=clinic_id,
            outreach_job_id="job-durable-twiml",
            idempotency_key="cadence:call:job-durable-twiml",
            available_at=NOW,
        )
        effect.state = ExternalEffectState.DISPATCHING
        effect.attempt_count = 1
        effect.lease_owner = "worker-durable-twiml"
        programme = create_programme(
            session,
            clinic_id=clinic_id,
            programme_id="pilot-durable-twiml",
            environment="production",
            release_identity="sha256:durable-twiml",
        )
        mark_programme_dark(
            session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            actor="operator:test",
            evidence_hash="d" * 64,
            now=NOW,
        )
        for ordinal in range(2, 6):
            patient_id = f"patient-durable-twiml-{ordinal}"
            session.add(
                Patient(
                    id=patient_id,
                    clinic_id=clinic_id,
                    source_ref=patient_id,
                    name=f"Synthetic TwiML Patient {ordinal}",
                )
            )
        session.flush()
        for patient_id in ["patient-durable-twiml"] + [
            f"patient-durable-twiml-{ordinal}" for ordinal in range(2, 6)
        ]:
            enroll_participant(
                session,
                clinic_id=clinic_id,
                programme_id=programme.id,
                patient_id=patient_id,
                now=NOW,
            )
        release_cumulative_limit(
            session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            cumulative_limit=5,
            actor="operator:test",
            evidence_hash="a" * 64,
            now=NOW,
        )
        return factory, effect.callback_token


def _configure_durable_twiml(
    monkeypatch: pytest.MonkeyPatch,
    factory: sessionmaker[Session],
) -> None:
    monkeypatch.delenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT_SID)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "synthetic-secret")
    monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", "https://clinic.example.test")
    monkeypatch.setenv(
        "TWILIO_MEDIA_STREAM_URL",
        "wss://clinic.example.test/api/v1/twilio/stream",
    )
    monkeypatch.setenv("CLINIC_RECALL_CALLBACK_APPLICATION_ENABLED", "false")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_OUTREACH_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_VOICE_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_RECORDING_ENABLED", "false")
    monkeypatch.setenv(
        "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT",
        datetime.now(UTC).isoformat(),
    )
    monkeypatch.setenv("CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS", "60")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "CLINIC_RECALL_PILOT_RELEASE_IDENTITY",
        "sha256:durable-twiml",
    )
    monkeypatch.setattr(voice, "get_sessionmaker", lambda: factory)


def _post_signed_durable_twiml(
    client: TestClient,
    *,
    effect_token: str | None,
    answered_by: str | None,
    clinic_id: str | None = None,
    call_sid: str | None = CALL_SID,
    signature: str | None = None,
):
    query = {
        "source": "clinic_recall_voice_worker",
    }
    if clinic_id is not None:
        query.update(
            {
                "scenario": "rebooking",
                "clinic_id": clinic_id,
                "patient_id": "patient-durable-twiml",
                "outreach_job_id": "job-durable-twiml",
                "record_call": "false",
            }
        )
    if effect_token is not None:
        query["effect_token"] = effect_token
    path = f"/api/v1/voice/twilio/twiml?{urlencode(query)}"
    data = {
        "AccountSid": ACCOUNT_SID,
    }
    if call_sid is not None:
        data["CallSid"] = call_sid
    if answered_by is not None:
        data["AnsweredBy"] = answered_by
    public_url = f"https://clinic.example.test{path}"
    signed = signature or _make_twilio_signature(
        public_url,
        data,
        "synthetic-secret",
    )
    return client.post(
        path,
        data=data,
        headers={"X-Twilio-Signature": signed},
    )


def _assert_silent_hangup(twiml: str) -> None:
    assert "<Hangup" in twiml
    for forbidden in ("<Say", "<Play", "<Connect", "<Stream"):
        assert forbidden not in twiml


@pytest.mark.parametrize(
    ("answered_by", "streams", "receipt_count"),
    [
        ("human", True, 1),
        ("machine_start", False, 1),
        ("fax", False, 1),
        ("unknown", False, 1),
        (None, False, 0),
        ("machine_end_beep", False, 0),
    ],
)
def test_signed_durable_amd_streams_only_for_human(
    monkeypatch: pytest.MonkeyPatch,
    answered_by: str | None,
    streams: bool,
    receipt_count: int,
) -> None:
    factory, effect_token = _durable_session_factory()
    _configure_durable_twiml(monkeypatch, factory)

    response = _post_signed_durable_twiml(
        TestClient(_voice_test_app()),
        effect_token=effect_token,
        answered_by=answered_by,
    )

    assert response.status_code == 200
    assert effect_token not in response.text
    if streams:
        assert response.text.count("<Connect>") == 1
        assert response.text.count("<Stream ") == 1
    else:
        _assert_silent_hangup(response.text)
    with factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(ProviderCallbackReceipt))
            == receipt_count
        )


def test_durable_human_after_programme_pause_never_starts_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, effect_token = _durable_session_factory()
    _configure_durable_twiml(monkeypatch, factory)
    with factory.begin() as session:
        programme = session.get(PilotProgramme, "pilot-durable-twiml")
        assert programme is not None
        programme.state = PilotProgrammeState.PAUSED

    response = _post_signed_durable_twiml(
        TestClient(_voice_test_app()),
        effect_token=effect_token,
        answered_by="human",
    )

    assert response.status_code == 200
    _assert_silent_hangup(response.text)


def test_durable_human_with_invalid_signature_creates_no_receipt_or_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, effect_token = _durable_session_factory()
    _configure_durable_twiml(monkeypatch, factory)

    response = _post_signed_durable_twiml(
        TestClient(_voice_test_app()),
        effect_token=effect_token,
        answered_by="human",
        signature="invalid-signature",
    )

    assert response.status_code == 401
    assert "<Stream" not in response.text
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 0


def test_durable_human_without_call_sid_never_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, effect_token = _durable_session_factory()
    _configure_durable_twiml(monkeypatch, factory)

    response = _post_signed_durable_twiml(
        TestClient(_voice_test_app()),
        effect_token=effect_token,
        answered_by="human",
        call_sid=None,
    )

    assert response.status_code == 200
    _assert_silent_hangup(response.text)


@pytest.mark.parametrize(
    "effect_token_factory",
    [
        lambda: None,
        lambda: "malformed-effect-token",
        lambda: generate_effect_token("clinic-durable-twiml-other"),
    ],
)
def test_durable_human_with_missing_invalid_or_unbound_token_hangs_up_silently(
    monkeypatch: pytest.MonkeyPatch,
    effect_token_factory,
) -> None:
    factory, _effect_token = _durable_session_factory()
    _configure_durable_twiml(monkeypatch, factory)

    response = _post_signed_durable_twiml(
        TestClient(_voice_test_app()),
        effect_token=effect_token_factory(),
        answered_by="human",
    )

    assert response.status_code == 200
    _assert_silent_hangup(response.text)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 0


def test_durable_human_with_cross_tenant_context_never_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, effect_token = _durable_session_factory()
    _configure_durable_twiml(monkeypatch, factory)

    response = _post_signed_durable_twiml(
        TestClient(_voice_test_app()),
        effect_token=effect_token,
        answered_by="human",
        clinic_id="clinic-durable-twiml-other",
    )

    assert response.status_code == 200
    _assert_silent_hangup(response.text)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 1


def test_durable_human_callback_persistence_failure_never_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, effect_token = _durable_session_factory()
    _configure_durable_twiml(monkeypatch, factory)

    def fail_persistence(*_args, **_kwargs):
        raise voice._DurableCallbackError(500)

    monkeypatch.setattr(voice, "_persist_durable_callback", fail_persistence)
    response = _post_signed_durable_twiml(
        TestClient(_voice_test_app()),
        effect_token=effect_token,
        answered_by="human",
    )

    assert response.status_code == 200
    _assert_silent_hangup(response.text)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 0


@pytest.mark.parametrize(
    "effect_state",
    [
        ExternalEffectState.PENDING,
        ExternalEffectState.LEASED,
        ExternalEffectState.REJECTED,
        ExternalEffectState.DEAD_LETTER,
        ExternalEffectState.CANCELED,
    ],
)
def test_durable_human_never_streams_for_non_dispatchable_effect_state(
    monkeypatch: pytest.MonkeyPatch,
    effect_state: ExternalEffectState,
) -> None:
    factory, effect_token = _durable_session_factory()
    _configure_durable_twiml(monkeypatch, factory)
    with factory.begin() as session:
        effect = session.execute(select(ExternalEffect)).scalar_one()
        effect.state = effect_state
        effect.lease_owner = None
        effect.lease_expires_at = None

    response = _post_signed_durable_twiml(
        TestClient(_voice_test_app()),
        effect_token=effect_token,
        answered_by="human",
    )

    assert response.status_code == 200
    _assert_silent_hangup(response.text)


def test_durable_human_never_streams_after_prior_non_human_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, effect_token = _durable_session_factory()
    _configure_durable_twiml(monkeypatch, factory)
    client = TestClient(_voice_test_app())

    machine = _post_signed_durable_twiml(
        client,
        effect_token=effect_token,
        answered_by="machine_start",
    )
    human = _post_signed_durable_twiml(
        client,
        effect_token=effect_token,
        answered_by="human",
    )

    _assert_silent_hangup(machine.text)
    _assert_silent_hangup(human.text)
    with factory() as session:
        statuses = set(session.scalars(select(ProviderCallbackReceipt.normalized_status)))
        assert statuses == {"human", "machine_start"}


def test_durable_human_never_streams_with_pending_provider_identity_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, effect_token = _durable_session_factory()
    _configure_durable_twiml(monkeypatch, factory)
    client = TestClient(_voice_test_app())
    other_call_sid = "CA" + "c" * 32

    first = _post_signed_durable_twiml(
        client,
        effect_token=effect_token,
        answered_by="human",
        call_sid=other_call_sid,
    )
    conflicting = _post_signed_durable_twiml(
        client,
        effect_token=effect_token,
        answered_by="human",
        call_sid=CALL_SID,
    )

    assert first.text.count("<Stream ") == 1
    _assert_silent_hangup(conflicting.text)


def test_durable_human_may_stream_when_signed_receipt_resolves_dispatch_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, effect_token = _durable_session_factory()
    _configure_durable_twiml(monkeypatch, factory)
    with factory.begin() as session:
        effect = session.execute(select(ExternalEffect)).scalar_one()
        effect.state = ExternalEffectState.RECONCILE_REQUIRED
        effect.last_error_class = "ProviderDispatchError"
        effect.last_error_code = "transport_error"
        effect.lease_owner = None
        effect.lease_expires_at = None

    response = _post_signed_durable_twiml(
        TestClient(_voice_test_app()),
        effect_token=effect_token,
        answered_by="human",
    )

    assert response.status_code == 200
    assert response.text.count("<Connect>") == 1
    assert response.text.count("<Stream ") == 1


def test_durable_outbound_uses_same_pre_model_recording_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, effect_token = _durable_session_factory()
    _configure_durable_twiml(monkeypatch, factory)
    monkeypatch.setenv("CLINIC_RECALL_PILOT_RECORDING_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RECORDING_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RECORDING_PROVIDER", "twilio")
    monkeypatch.setenv("CLINIC_RECALL_RECORDING_DISCLOSURE_APPROVED", "true")
    monkeypatch.setenv("CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT", DISCLOSURE_TEXT)
    monkeypatch.setenv(
        "CLINIC_RECALL_RECORDING_DISCLOSURE_REFRESHED_AT",
        datetime.now(UTC).isoformat(),
    )
    monkeypatch.setenv(
        "CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION",
        "synthetic-pr09-v1",
    )
    client = TestClient(_voice_test_app())

    initial = _post_signed_durable_twiml(
        client,
        effect_token=effect_token,
        answered_by="human",
        call_sid=CALL_SID,
    )

    assert initial.status_code == 200
    assert '<Gather input="speech dtmf"' in initial.text
    assert "<Connect>" not in initial.text
    assert "recording-consent" in initial.text
    with factory() as session:
        ledger = session.execute(select(CallRecord)).scalar_one()
        assert ledger.external_effect_id is not None
        assert ledger.provider_call_id == CALL_SID
        assert ledger.patient_id == "patient-durable-twiml"
        assert ledger.direction == InteractionDirection.OUTBOUND
        assert ledger.consent_state == RecordingConsentState.ASKED

    callback_path = (
        "/api/v1/voice/twilio/recording-consent?"
        + urlencode(
            {
                "source": "clinic_recall_voice_worker",
                "effect_token": effect_token,
            }
        )
    )
    callback_data = {
        "AccountSid": ACCOUNT_SID,
        "CallSid": CALL_SID,
        "Digits": "1",
    }
    callback_signature = _make_twilio_signature(
        f"https://clinic.example.test{callback_path}",
        callback_data,
        "synthetic-secret",
    )

    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    unsigned = client.post(callback_path, data=callback_data)
    assert unsigned.status_code == 401
    monkeypatch.delenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", raising=False)

    callback = client.post(
        callback_path,
        data=callback_data,
        headers={"X-Twilio-Signature": callback_signature},
    )

    assert callback.status_code == 200
    assert "<Connect>" in callback.text
    assert '<Parameter name="record_call" value="false" />' in callback.text
    with factory() as session:
        ledger = session.execute(select(CallRecord)).scalar_one()
        recording_effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.RECORDING
            )
        ).scalar_one()
        assert ledger.consent_state == RecordingConsentState.GRANTED
        assert ledger.consent_decision_source.value == "dtmf"
        assert ledger.recording_status == CallRecordingStatus.START_PENDING
        assert recording_effect.aggregate_id == ledger.id


@pytest.mark.parametrize("callback_base", [None, "http://clinic.example.test"])
def test_durable_recording_consent_requires_explicit_https_callback_base(
    monkeypatch: pytest.MonkeyPatch,
    callback_base: str | None,
) -> None:
    factory, effect_token = _durable_session_factory()
    _configure_durable_twiml(monkeypatch, factory)
    monkeypatch.setenv("CLINIC_RECALL_PILOT_RECORDING_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RECORDING_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RECORDING_PROVIDER", "twilio")
    monkeypatch.setenv("CLINIC_RECALL_RECORDING_DISCLOSURE_APPROVED", "true")
    monkeypatch.setenv("CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT", DISCLOSURE_TEXT)
    monkeypatch.setenv(
        "CLINIC_RECALL_RECORDING_DISCLOSURE_REFRESHED_AT",
        datetime.now(UTC).isoformat(),
    )
    monkeypatch.setenv(
        "CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION",
        "synthetic-pr09-v1",
    )
    monkeypatch.delenv("BASE_URL", raising=False)
    if callback_base is None:
        monkeypatch.delenv("TWILIO_WEBHOOK_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", callback_base)
    monkeypatch.setattr(voice, "_validate_twilio_signature", lambda *_args: True)

    response = _post_signed_durable_twiml(
        TestClient(_voice_test_app()),
        effect_token=effect_token,
        answered_by="human",
        call_sid=CALL_SID,
    )

    assert response.status_code == 200
    assert "<Gather" not in response.text
    assert "<Connect>" in response.text
    assert effect_token not in response.text
    with factory() as session:
        ledger = session.execute(select(CallRecord)).scalar_one()
        assert ledger.consent_state == RecordingConsentState.NOT_ASKED


def test_twilio_voice_twiml_connects_media_stream_with_context(monkeypatch) -> None:
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setenv("TWILIO_MEDIA_STREAM_URL", "wss://clinic.example.test/api/v1/twilio/stream")
    client = TestClient(_voice_test_app())

    response = client.get(
        "/api/v1/voice/twilio/twiml",
        params={
            "session_id": "twilio-session-1",
            "scenario": "rebooking",
            "clinic_id": "clinic-a",
            "patient_id": "patient-a",
            "outreach_job_id": "job-a",
            "record_call": "true",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert '<Connect><Stream url="wss://clinic.example.test/api/v1/twilio/stream">' in response.text
    assert '<Parameter name="session_id" value="twilio-session-1" />' in response.text
    assert '<Parameter name="scenario" value="rebooking" />' in response.text
    assert '<Parameter name="clinic_id" value="clinic-a" />' in response.text
    assert '<Parameter name="patient_id" value="patient-a" />' in response.text
    assert '<Parameter name="outreach_job_id" value="job-a" />' in response.text
    assert '<Parameter name="record_call" value="false" />' in response.text
    assert "<Say>" not in response.text


def test_twilio_voice_twiml_routes_inbound_called_number_to_trusted_context(monkeypatch) -> None:
    factory = _session_factory()
    _seed_phone_route(factory)
    monkeypatch.setattr(voice, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_MEDIA_STREAM_URL", "wss://clinic.example.test/api/v1/twilio/stream")
    client = TestClient(_voice_test_app())

    response = client.post(
        "/api/v1/voice/twilio/twiml",
        data={
            "AccountSid": "AC123",
            "CallSid": "CA123",
            "To": "+1 (555) 123-0000",
            "From": "+1 555 999 1111",
        },
    )

    assert response.status_code == 200
    assert '<Parameter name="source" value="clinic_recall_inbound" />' in response.text
    assert '<Parameter name="provider" value="twilio" />' in response.text
    assert '<Parameter name="provider_call_id" value="CA123" />' in response.text
    assert '<Parameter name="clinic_id" value="clinic-a" />' in response.text
    assert '<Parameter name="scenario" value="inbound_clinic" />' in response.text
    assert '<Parameter name="call_direction" value="inbound" />' in response.text
    assert '<Parameter name="called_number_id" value="phone-a" />' in response.text
    assert '<Parameter name="called_number" value="+15551230000" />' in response.text
    assert '<Parameter name="caller_number_hash" value="sha256:' in response.text
    assert "+15559991111" not in response.text
    with factory() as session:
        call = session.execute(select(InboundCall)).scalar_one()
        ledger = session.execute(select(CallRecord)).scalar_one()
        assert call.provider == ClinicPhoneProvider.TWILIO
        assert call.provider_call_id == "CA123"
        assert call.clinic_id == "clinic-a"
        assert ledger.inbound_call_id == call.id
        assert ledger.provider_call_id == call.provider_call_id
        assert ledger.patient_id is None
        assert ledger.direction == InteractionDirection.INBOUND
        assert ledger.consent_state == RecordingConsentState.NOT_ASKED
        assert ledger.recording_status == CallRecordingStatus.NONE


def test_inbound_recording_consent_is_gathered_before_model_and_minimized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _session_factory()
    _seed_phone_route(factory)
    _enable_recording_consent(monkeypatch, factory)
    monkeypatch.setattr(voice, "get_sessionmaker", lambda: factory)
    monkeypatch.delenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT_SID)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "synthetic-secret")
    monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", "https://clinic.example.test")
    monkeypatch.setenv(
        "TWILIO_MEDIA_STREAM_URL",
        "wss://clinic.example.test/api/v1/twilio/stream",
    )
    dispatched: list[str] = []
    monkeypatch.setattr(
        voice,
        "_dispatch_recording_effect_batch",
        lambda clinic_id: dispatched.append(clinic_id),
    )
    client = TestClient(_voice_test_app())
    initial_path = "/api/v1/voice/twilio/twiml"
    initial_data = {
        "AccountSid": ACCOUNT_SID,
        "CallSid": CALL_SID,
        "To": "+15551230000",
        "From": "+15559991111",
    }
    initial_signature = _make_twilio_signature(
        f"https://clinic.example.test{initial_path}",
        initial_data,
        "synthetic-secret",
    )

    initial = client.post(
        initial_path,
        data=initial_data,
        headers={"X-Twilio-Signature": initial_signature},
    )

    assert initial.status_code == 200
    assert '<Gather input="speech dtmf"' in initial.text
    assert DISCLOSURE_TEXT in initial.text
    assert "/api/v1/voice/twilio/recording-consent" in initial.text
    assert "<Connect>" not in initial.text
    with factory() as session:
        ledger = session.execute(select(CallRecord)).scalar_one()
        assert ledger.consent_state == RecordingConsentState.ASKED
        assert ledger.consent_version == "synthetic-pr09-v1"
        assert ledger.recording_status == CallRecordingStatus.NONE
        assert session.scalar(
            select(func.count()).select_from(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.RECORDING
            )
        ) == 0

    callback_path = "/api/v1/voice/twilio/recording-consent"
    callback_data = {
        **initial_data,
        "SpeechResult": "yes please",
        "Confidence": "0.99",
    }
    callback_signature = _make_twilio_signature(
        f"https://clinic.example.test{callback_path}",
        callback_data,
        "synthetic-secret",
    )

    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    unsigned = client.post(callback_path, data=callback_data)
    assert unsigned.status_code == 401
    monkeypatch.delenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", raising=False)

    callback = client.post(
        callback_path,
        data=callback_data,
        headers={"X-Twilio-Signature": callback_signature},
    )

    assert callback.status_code == 200
    assert dispatched == ["clinic-a"]
    assert "<Gather" not in callback.text
    assert "<Connect>" in callback.text
    assert '<Parameter name="record_call" value="false" />' in callback.text
    assert "yes please" not in callback.text.lower()
    with factory() as session:
        ledger = session.execute(select(CallRecord)).scalar_one()
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.RECORDING
            )
        ).scalar_one()
        assert ledger.consent_state == RecordingConsentState.GRANTED
        assert ledger.consent_decision_source.value == "speech"
        assert ledger.recording_status == CallRecordingStatus.START_PENDING
        assert "yes please" not in str(ledger.__dict__).lower()
        assert effect.aggregate_type == "call_record"
        assert effect.aggregate_id == ledger.id
        assert effect.payload == {
            "intent": "recording_start",
            "call_record_id": ledger.id,
        }

    with factory.begin() as session:
        ledger = session.execute(select(CallRecord)).scalar_one()
        ledger.recording_sid = "RE" + "7" * 32
        ledger.recording_status = CallRecordingStatus.IN_PROGRESS
        ledger.recording_started_at = NOW

    replay = client.post(
        callback_path,
        data=callback_data,
        headers={"X-Twilio-Signature": callback_signature},
    )

    assert replay.status_code == 200
    assert "<Connect>" in replay.text
    assert dispatched == ["clinic-a"]
    with factory() as session:
        assert session.scalar(
            select(func.count()).select_from(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.RECORDING
            )
        ) == 1


def test_twilio_voice_twiml_fails_closed_for_unmapped_inbound_number(monkeypatch) -> None:
    factory = _session_factory()
    monkeypatch.setattr(voice, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    client = TestClient(_voice_test_app())

    response = client.post(
        "/api/v1/voice/twilio/twiml",
        data={
            "AccountSid": "AC123",
            "CallSid": "CA123",
            "To": "+15551230000",
            "From": "+15559991111",
        },
    )

    assert response.status_code == 200
    assert "<Hangup" in response.text
    assert "<Stream" not in response.text


def test_twilio_voice_twiml_fails_closed_for_inactive_inbound_number(monkeypatch) -> None:
    factory = _session_factory()
    _seed_phone_route(factory, status=ClinicPhoneStatus.INACTIVE)
    monkeypatch.setattr(voice, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    client = TestClient(_voice_test_app())

    response = client.post(
        "/api/v1/voice/twilio/twiml",
        data={
            "AccountSid": "AC123",
            "CallSid": "CA123",
            "To": "+15551230000",
            "From": "+15559991111",
        },
    )

    assert response.status_code == 200
    assert "<Hangup" in response.text
    assert "<Stream" not in response.text


def test_twilio_voice_twiml_validates_signed_post(monkeypatch) -> None:
    monkeypatch.delenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", "https://clinic.example.test")
    client = TestClient(_voice_test_app())
    path = "/api/v1/voice/twilio/twiml?session_id=twilio-session-1"
    data = {"AccountSid": "AC123", "CallSid": "CA123"}
    signature = _make_twilio_signature(
        "https://clinic.example.test/api/v1/voice/twilio/twiml?session_id=twilio-session-1",
        data,
        "secret",
    )

    response = client.post(path, data=data, headers={"X-Twilio-Signature": signature})

    assert response.status_code == 200
    assert "twilio-session-1" in response.text


def test_twilio_voice_twiml_rejects_bad_signature(monkeypatch) -> None:
    monkeypatch.delenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", "https://clinic.example.test")
    client = TestClient(_voice_test_app())

    response = client.post(
        "/api/v1/voice/twilio/twiml?session_id=twilio-session-1",
        data={"AccountSid": "AC123", "CallSid": "CA123"},
        headers={"X-Twilio-Signature": "bad"},
    )

    assert response.status_code == 401
