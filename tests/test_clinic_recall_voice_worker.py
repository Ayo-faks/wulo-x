"""Tests for the Phase 3 NO_REPLY to voice hand-off worker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import func, select
from src.clinic_recall.durable.enqueue import enqueue_sms_effect
from src.clinic_recall.enums import (
    AuditAction,
    CampaignStatus,
    CampaignType,
    Channel,
    ExternalEffectState,
    ExternalEffectType,
    InteractionDirection,
    OutreachState,
    SkipReason,
)
from src.clinic_recall.models import (
    Appointment,
    AuditLog,
    Campaign,
    Clinic,
    ExternalEffect,
    Interaction,
    OutreachJob,
    Patient,
)
from src.clinic_recall.voice_worker import (
    ArtCallInitiator,
    CallInitiationDisposition,
    CallInitiationReason,
    TwilioCallInitiator,
    build_call_initiator,
    run_voice_cadence,
)

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


class _FailIfCalledInitiator:
    name = "fail-if-called"

    def initiate_call(self, **_kwargs):
        raise AssertionError("voice cadence must not call a provider initiator")


def _allow_programme(_session, _clinic_id, _job, _now) -> bool:
    return True


def _plan_voice(sqlite_session, clinic_id: str):
    return run_voice_cadence(
        sqlite_session,
        clinic_id,
        NOW,
        initiator=_FailIfCalledInitiator(),
        programme_gate=_allow_programme,
    )


def _seed_no_reply_job(
    sqlite_session,
    *,
    call_consent: bool = True,
    recording_consent: bool = False,
) -> str:
    clinic_id = "clinic-voice-worker"
    sqlite_session.add(
        Clinic(
            id=clinic_id,
            name="Voice Worker Clinic",
            sms_number="+447700920000",
            timezone="Europe/London",
            daily_caps=200,
        )
    )
    sqlite_session.add(
        Patient(
            id="patient-voice-worker",
            clinic_id=clinic_id,
            source_ref="P-VOICE-WORKER",
            name="Voice Worker Patient",
            phone="+447700920001",
            email="voice-worker@example.test",
            consent_flags={
                "call": call_consent,
                "sms": True,
                "email": True,
                "recording": recording_consent,
            },
            opt_out_flags={},
        )
    )
    sqlite_session.add(
        Appointment(
            id="appointment-voice-worker",
            clinic_id=clinic_id,
            patient_id="patient-voice-worker",
            source_ref="A-VOICE-WORKER",
            status="missed",
            start_at=datetime(2026, 6, 20, 9, 0, tzinfo=UTC),
        )
    )
    sqlite_session.add(
        Campaign(
            id="campaign-voice-worker",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    sqlite_session.add(
        OutreachJob(
            id="job-voice-worker",
            clinic_id=clinic_id,
            campaign_id="campaign-voice-worker",
            patient_id="patient-voice-worker",
            appointment_id="appointment-voice-worker",
            channel=Channel.SMS,
            state=OutreachState.NO_REPLY,
        )
    )
    sqlite_session.flush()
    return clinic_id


def _add_sms_effect(
    sqlite_session,
    clinic_id: str,
    *,
    dispatch_started_at: datetime,
    state: ExternalEffectState = ExternalEffectState.SUCCEEDED,
    provider_status: str = "delivery_succeeded",
) -> ExternalEffect:
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-voice-worker",
        idempotency_key="cadence:sms:job-voice-worker",
        available_at=dispatch_started_at,
    )
    effect.state = state
    effect.dispatch_started_at = dispatch_started_at
    effect.provider_status = provider_status
    effect.provider_resource_id = "SM-synthetic-terminal"
    effect.completed_at = dispatch_started_at + timedelta(minutes=1)
    sqlite_session.flush()
    return effect


def test_voice_cadence_enqueues_one_call_without_provider_io(sqlite_session):
    clinic_id = _seed_no_reply_job(sqlite_session)
    _add_sms_effect(
        sqlite_session,
        clinic_id,
        dispatch_started_at=NOW - timedelta(hours=49),
    )
    initiator = _FailIfCalledInitiator()

    first = run_voice_cadence(
        sqlite_session,
        clinic_id,
        NOW,
        initiator=initiator,
        programme_gate=_allow_programme,
    )
    second = run_voice_cadence(
        sqlite_session,
        clinic_id,
        NOW,
        initiator=initiator,
        programme_gate=_allow_programme,
    )

    job = sqlite_session.get(OutreachJob, "job-voice-worker")
    call_effects = (
        sqlite_session.execute(
            select(ExternalEffect).where(ExternalEffect.effect_type == ExternalEffectType.CALL)
        )
        .scalars()
        .all()
    )

    assert first.calls_enqueued == 1
    assert first.calls_initiated == 0
    assert second.calls_enqueued == 0
    assert second.call_existing == 1
    assert job.state == OutreachState.NO_REPLY
    assert job.attempts == 0
    assert len(call_effects) == 1
    assert call_effects[0].state == ExternalEffectState.PENDING
    assert call_effects[0].payload == {
        "intent": "recall_fallback",
        "outreach_job_id": "job-voice-worker",
    }
    assert sqlite_session.execute(select(Interaction)).scalars().all() == []
    assert sqlite_session.execute(select(AuditLog)).scalars().all() == []


def test_voice_cadence_applies_limit_after_due_candidate_priority(sqlite_session):
    clinic_id = _seed_no_reply_job(sqlite_session)
    target_job = sqlite_session.get(OutreachJob, "job-voice-worker")
    target_job.created_at = NOW
    sqlite_session.add(
        Patient(
            id="patient-historical-not-actionable",
            clinic_id=clinic_id,
            source_ref="P-HISTORICAL-NOT-ACTIONABLE",
            name="Historical Synthetic Patient",
            phone="+447700920099",
            consent_flags={"call": True, "sms": True},
            opt_out_flags={},
        )
    )
    sqlite_session.add(
        Appointment(
            id="appointment-historical-not-actionable",
            clinic_id=clinic_id,
            patient_id="patient-historical-not-actionable",
            source_ref="A-HISTORICAL-NOT-ACTIONABLE",
            status="missed",
            start_at=NOW - timedelta(days=20),
        )
    )
    sqlite_session.flush()
    sqlite_session.add(
        OutreachJob(
            id="job-historical-not-actionable",
            clinic_id=clinic_id,
            campaign_id="campaign-voice-worker",
            patient_id="patient-historical-not-actionable",
            appointment_id="appointment-historical-not-actionable",
            channel=Channel.SMS,
            state=OutreachState.COMPLETED,
            created_at=NOW - timedelta(days=10),
        )
    )
    _add_sms_effect(
        sqlite_session,
        clinic_id,
        dispatch_started_at=NOW - timedelta(hours=49),
    )
    sqlite_session.flush()

    result = run_voice_cadence(
        sqlite_session,
        clinic_id,
        NOW,
        programme_gate=_allow_programme,
        limit=1,
    )

    call_effect = sqlite_session.execute(
        select(ExternalEffect).where(ExternalEffect.effect_type == ExternalEffectType.CALL)
    ).scalar_one()
    assert result.calls_enqueued == 1
    assert call_effect.aggregate_id == "job-voice-worker"


def test_voice_cadence_does_not_precompute_recording_or_patient_data(sqlite_session):
    clinic_id = _seed_no_reply_job(sqlite_session, recording_consent=True)
    _add_sms_effect(
        sqlite_session,
        clinic_id,
        dispatch_started_at=NOW - timedelta(hours=49),
    )

    result = _plan_voice(sqlite_session, clinic_id)

    call_effect = sqlite_session.execute(
        select(ExternalEffect).where(ExternalEffect.effect_type == ExternalEffectType.CALL)
    ).scalar_one()
    assert result.calls_enqueued == 1
    assert call_effect.payload == {
        "intent": "recall_fallback",
        "outreach_job_id": "job-voice-worker",
    }
    serialized = str(call_effect.payload)
    assert "patient-voice-worker" not in serialized
    assert "+447700920001" not in serialized
    assert "record" not in serialized
    assert sqlite_session.execute(select(AuditLog)).scalars().all() == []


def test_voice_cadence_does_not_plan_after_existing_outbound_call(sqlite_session):
    clinic_id = _seed_no_reply_job(sqlite_session)
    _add_sms_effect(
        sqlite_session,
        clinic_id,
        dispatch_started_at=NOW - timedelta(hours=49),
    )
    sqlite_session.add(
        Interaction(
            id="interaction-existing-call",
            clinic_id=clinic_id,
            outreach_job_id="job-voice-worker",
            channel=Channel.CALL,
            direction=InteractionDirection.OUTBOUND,
            content="existing-call",
            occurred_at=NOW,
        )
    )
    sqlite_session.flush()

    result = _plan_voice(sqlite_session, clinic_id)

    assert result.skipped["outbound_call_exists"] == 1
    assert sqlite_session.execute(select(func.count()).select_from(Interaction)).scalar() == 1
    assert (
        sqlite_session.execute(
            select(ExternalEffect).where(ExternalEffect.effect_type == ExternalEffectType.CALL)
        )
        .scalars()
        .all()
        == []
    )


def test_voice_cadence_honours_call_consent_before_enqueue(sqlite_session):
    clinic_id = _seed_no_reply_job(sqlite_session, call_consent=False)
    _add_sms_effect(
        sqlite_session,
        clinic_id,
        dispatch_started_at=NOW - timedelta(hours=49),
    )

    result = _plan_voice(sqlite_session, clinic_id)

    job = sqlite_session.get(OutreachJob, "job-voice-worker")
    assert result.skipped[SkipReason.NO_CONSENT.value] == 1
    assert job.state == OutreachState.NO_REPLY
    assert (
        sqlite_session.execute(
            select(ExternalEffect).where(ExternalEffect.effect_type == ExternalEffectType.CALL)
        )
        .scalars()
        .all()
        == []
    )
    assert sqlite_session.execute(select(AuditLog.action)).scalars().all() == [
        AuditAction.SKIP_CANDIDATE
    ]


def test_voice_cadence_defers_quiet_hours_without_mutating_sms_job(sqlite_session):
    clinic_id = _seed_no_reply_job(sqlite_session)
    _add_sms_effect(
        sqlite_session,
        clinic_id,
        dispatch_started_at=NOW - timedelta(hours=49),
    )
    patient = sqlite_session.get(Patient, "patient-voice-worker")
    patient.contact_prefs = {"quiet_start_hour": 8, "quiet_end_hour": 20}

    result = _plan_voice(sqlite_session, clinic_id)

    job = sqlite_session.get(OutreachJob, "job-voice-worker")
    assert result.skipped[SkipReason.QUIET_HOURS.value] == 1
    assert job.state == OutreachState.NO_REPLY
    assert job.next_action_at is None
    assert (
        sqlite_session.execute(
            select(ExternalEffect).where(ExternalEffect.effect_type == ExternalEffectType.CALL)
        )
        .scalars()
        .all()
        == []
    )


@pytest.mark.parametrize(
    ("age", "state", "provider_status", "skip_reason"),
    [
        (timedelta(hours=47), ExternalEffectState.SUCCEEDED, "delivery_succeeded", "sms_wait_48h"),
        (timedelta(hours=47), ExternalEffectState.SUCCEEDED, "delivery_failed", "sms_wait_48h"),
        (
            timedelta(hours=49),
            ExternalEffectState.RECONCILE_REQUIRED,
            "provider_unresolved",
            "sms_not_terminal",
        ),
        (timedelta(hours=49), ExternalEffectState.SUCCEEDED, "accepted", "sms_not_terminal"),
        (
            timedelta(hours=49),
            ExternalEffectState.SUCCEEDED,
            "provider_observed",
            "sms_not_terminal",
        ),
    ],
)
def test_voice_cadence_requires_48_hours_and_terminal_sms_evidence(
    sqlite_session,
    age,
    state,
    provider_status,
    skip_reason,
):
    clinic_id = _seed_no_reply_job(sqlite_session)
    _add_sms_effect(
        sqlite_session,
        clinic_id,
        dispatch_started_at=NOW - age,
        state=state,
        provider_status=provider_status,
    )

    result = _plan_voice(sqlite_session, clinic_id)

    assert result.skipped[skip_reason] == 1
    assert (
        sqlite_session.execute(
            select(ExternalEffect).where(ExternalEffect.effect_type == ExternalEffectType.CALL)
        )
        .scalars()
        .all()
        == []
    )


def test_voice_cadence_reconciliation_after_48_hours_enables_next_tick(sqlite_session):
    clinic_id = _seed_no_reply_job(sqlite_session)
    sms_effect = _add_sms_effect(
        sqlite_session,
        clinic_id,
        dispatch_started_at=NOW - timedelta(hours=49),
        state=ExternalEffectState.RECONCILE_REQUIRED,
        provider_status="provider_unresolved",
    )

    first = _plan_voice(sqlite_session, clinic_id)
    sms_effect.state = ExternalEffectState.SUCCEEDED
    sms_effect.provider_status = "delivery_failed"
    sqlite_session.flush()
    second = _plan_voice(sqlite_session, clinic_id)

    assert first.skipped["sms_not_terminal"] == 1
    assert second.calls_enqueued == 1


def test_voice_cadence_programme_gate_is_fail_closed_until_pr13_binding(sqlite_session):
    clinic_id = _seed_no_reply_job(sqlite_session)
    _add_sms_effect(
        sqlite_session,
        clinic_id,
        dispatch_started_at=NOW - timedelta(hours=49),
    )

    result = run_voice_cadence(
        sqlite_session,
        clinic_id,
        NOW,
        initiator=_FailIfCalledInitiator(),
    )

    assert result.skipped["programme_gate_unbound"] == 1
    assert (
        sqlite_session.execute(
            select(ExternalEffect).where(ExternalEffect.effect_type == ExternalEffectType.CALL)
        )
        .scalars()
        .all()
        == []
    )


@pytest.mark.parametrize(
    ("stop_kind", "skip_reason"),
    [
        ("reply", "inbound_reply"),
        ("opt_out", SkipReason.OPTED_OUT.value),
        ("completed", "outreach_completed"),
        ("escalated", "outreach_escalated"),
        ("paused", "campaign_not_active"),
    ],
)
def test_voice_cadence_stop_states_block_call_enqueue(
    sqlite_session,
    stop_kind,
    skip_reason,
):
    clinic_id = _seed_no_reply_job(sqlite_session)
    _add_sms_effect(
        sqlite_session,
        clinic_id,
        dispatch_started_at=NOW - timedelta(hours=49),
    )
    job = sqlite_session.get(OutreachJob, "job-voice-worker")
    patient = sqlite_session.get(Patient, "patient-voice-worker")
    if stop_kind == "reply":
        sqlite_session.add(
            Interaction(
                id="interaction-inbound-reply",
                clinic_id=clinic_id,
                outreach_job_id=job.id,
                channel=Channel.SMS,
                direction=InteractionDirection.INBOUND,
                occurred_at=NOW,
            )
        )
    elif stop_kind == "opt_out":
        patient.opt_out_flags = {"call": True}
    elif stop_kind == "completed":
        job.state = OutreachState.COMPLETED
    elif stop_kind == "escalated":
        job.state = OutreachState.ESCALATED
    elif stop_kind == "paused":
        sqlite_session.get(Campaign, "campaign-voice-worker").status = CampaignStatus.PAUSED
    sqlite_session.flush()

    result = _plan_voice(sqlite_session, clinic_id)

    assert result.skipped[skip_reason] == 1
    assert (
        sqlite_session.execute(
            select(ExternalEffect).where(ExternalEffect.effect_type == ExternalEffectType.CALL)
        )
        .scalars()
        .all()
        == []
    )


def test_voice_cadence_missing_contactability_blocks_call_enqueue(sqlite_session):
    clinic_id = _seed_no_reply_job(sqlite_session)
    _add_sms_effect(
        sqlite_session,
        clinic_id,
        dispatch_started_at=NOW - timedelta(hours=49),
    )
    sqlite_session.get(Patient, "patient-voice-worker").phone = None

    result = _plan_voice(sqlite_session, clinic_id)

    assert result.skipped[SkipReason.NOT_CONTACTABLE.value] == 1
    assert (
        sqlite_session.execute(
            select(ExternalEffect).where(ExternalEffect.effect_type == ExternalEffectType.CALL)
        )
        .scalars()
        .all()
        == []
    )


@pytest.mark.parametrize(
    ("contact_count", "daily_cap", "skip_reason"),
    [
        (3, 200, SkipReason.FREQUENCY_CAP.value),
        (1, 1, SkipReason.DAILY_CAP.value),
    ],
)
def test_voice_cadence_rechecks_frequency_and_daily_caps(
    sqlite_session,
    contact_count,
    daily_cap,
    skip_reason,
):
    clinic_id = _seed_no_reply_job(sqlite_session)
    _add_sms_effect(
        sqlite_session,
        clinic_id,
        dispatch_started_at=NOW - timedelta(hours=49),
    )
    sqlite_session.get(Clinic, clinic_id).daily_caps = daily_cap
    for index in range(contact_count):
        sqlite_session.add(
            Interaction(
                id=f"interaction-cap-{index}",
                clinic_id=clinic_id,
                outreach_job_id="job-voice-worker",
                channel=Channel.SMS,
                direction=InteractionDirection.OUTBOUND,
                occurred_at=NOW - timedelta(minutes=index + 1),
            )
        )
    sqlite_session.flush()

    result = _plan_voice(sqlite_session, clinic_id)

    assert result.skipped[skip_reason] == 1
    assert (
        sqlite_session.execute(
            select(ExternalEffect).where(ExternalEffect.effect_type == ExternalEffectType.CALL)
        )
        .scalars()
        .all()
        == []
    )


def test_voice_cadence_cancels_pending_call_after_inbound_reply(sqlite_session):
    clinic_id = _seed_no_reply_job(sqlite_session)
    _add_sms_effect(
        sqlite_session,
        clinic_id,
        dispatch_started_at=NOW - timedelta(hours=49),
    )
    first = _plan_voice(sqlite_session, clinic_id)
    sqlite_session.add(
        Interaction(
            id="interaction-reply-after-plan",
            clinic_id=clinic_id,
            outreach_job_id="job-voice-worker",
            channel=Channel.SMS,
            direction=InteractionDirection.INBOUND,
            occurred_at=NOW,
        )
    )
    sqlite_session.flush()

    second = _plan_voice(sqlite_session, clinic_id)
    third = _plan_voice(sqlite_session, clinic_id)

    call_effect = sqlite_session.execute(
        select(ExternalEffect).where(ExternalEffect.effect_type == ExternalEffectType.CALL)
    ).scalar_one()
    assert first.calls_enqueued == 1
    assert second.calls_canceled == 1
    assert third.calls_canceled == 0
    assert call_effect.state == ExternalEffectState.CANCELED
    assert call_effect.provider_status == "not_dispatched"
    assert call_effect.last_error_code == "inbound_reply"


def test_twilio_call_initiator_places_call_with_hosted_twiml_context() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/2010-04-01/Accounts/AC123/Calls.json"
        body = parse_qs(request.content.decode("utf-8"))
        assert body["From"] == ["+447700900002"]
        assert body["To"] == ["+447700900001"]
        assert "Record" not in body
        assert "MachineDetection" not in body
        assert request.headers.get("Authorization", "").startswith("Basic ")

        twiml_url = body["Url"][0]
        parsed = urlsplit(twiml_url)
        query = parse_qs(parsed.query)
        assert parsed.scheme == "https"
        assert parsed.netloc == "clinic.example.test"
        assert parsed.path == "/api/v1/voice/twilio/twiml"
        assert query["session_id"][0].startswith("twilio-session-")
        assert query["scenario"] == ["rebooking"]
        assert query["clinic_id"] == ["clinic-voice-worker"]
        assert query["patient_id"] == ["patient-voice-worker"]
        assert query["outreach_job_id"] == ["job-voice-worker"]
        assert query["record_call"] == ["false"]
        return httpx.Response(201, json={"sid": "CA123", "status": "queued"})

    initiator = TwilioCallInitiator(
        account_sid="AC123",
        auth_token="secret",
        from_number="+447700900002",
        twiml_url="https://clinic.example.test/api/v1/voice/twilio/twiml",
        transport=httpx.MockTransport(handler),
    )

    result = initiator.initiate_call(
        target_number="+447700900001",
        context={
            "source": "non_durable_compatibility",
            "scenario": "rebooking",
            "clinic_id": "clinic-voice-worker",
            "patient_id": "patient-voice-worker",
            "outreach_job_id": "job-voice-worker",
            "record_call": False,
        },
    )

    assert result.successful is True
    assert result.call_id == "CA123"
    assert result.provider == "twilio"


def test_twilio_call_initiator_requests_dual_channel_recording_with_callback(monkeypatch) -> None:
    monkeypatch.setenv(
        "TWILIO_RECORDING_STATUS_CALLBACK_URL",
        "https://clinic.example.test/api/v1/voice/twilio/recording-status",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode("utf-8"))
        assert body["Record"] == ["true"]
        assert body["RecordingChannels"] == ["dual"]
        assert body["RecordingStatusCallback"] == [
            "https://clinic.example.test/api/v1/voice/twilio/recording-status"
        ]
        assert body["RecordingStatusCallbackEvent"] == ["completed"]
        return httpx.Response(201, json={"sid": "CA-rec"})

    initiator = TwilioCallInitiator(
        account_sid="AC123",
        auth_token="secret",
        from_number="+447700900002",
        twiml_url="https://clinic.example.test/api/v1/voice/twilio/twiml",
        transport=httpx.MockTransport(handler),
    )

    result = initiator.initiate_call(
        target_number="+447700900001",
        context={"scenario": "customer_support", "record_call": True},
    )

    assert result.successful is True


def test_twilio_call_initiator_rejects_clinic_recall_recording_at_creation() -> None:
    initiator = TwilioCallInitiator(
        account_sid="AC123",
        auth_token="secret",
        from_number="+447700900002",
        twiml_url="https://clinic.example.test/api/v1/voice/twilio/twiml",
        transport=httpx.MockTransport(
            lambda _request: pytest.fail("Clinic Recall recording reached Twilio create")
        ),
    )

    result = initiator.initiate_call(
        target_number="+447700900001",
        context={
            "source": "non_durable_compatibility",
            "scenario": "rebooking",
            "clinic_id": "clinic-voice-worker",
            "record_call": True,
        },
    )

    assert result.successful is False
    assert result.disposition == CallInitiationDisposition.NOT_DISPATCHED
    assert result.reason_code == CallInitiationReason.DURABLE_POLICY_REJECTED


def test_twilio_call_initiator_defaults_status_callback_to_public_base(monkeypatch) -> None:
    monkeypatch.delenv("TWILIO_VOICE_STATUS_CALLBACK_URL", raising=False)
    monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", "https://clinic.example.test")

    def handler(request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode("utf-8"))
        assert body["StatusCallback"] == [
            "https://clinic.example.test/api/v1/voice/twilio/call-status"
        ]
        assert body["StatusCallbackEvent"] == [
            "initiated",
            "ringing",
            "answered",
            "completed",
        ]
        return httpx.Response(201, json={"sid": "CA-status"})

    initiator = TwilioCallInitiator(
        account_sid="AC123",
        auth_token="secret",
        from_number="+447700900002",
        twiml_url="https://clinic.example.test/api/v1/voice/twilio/twiml",
        transport=httpx.MockTransport(handler),
    )

    result = initiator.initiate_call(
        target_number="+447700900001",
        context={"scenario": "rebooking"},
    )

    assert result.successful is True


def test_twilio_call_initiator_scopes_callback_urls_without_stream_token(
    monkeypatch,
) -> None:
    from urllib.parse import parse_qs, urlsplit

    from src.clinic_recall.durable.callbacks import generate_effect_token

    token = generate_effect_token("clinic-voice-callback-token")
    recording_token = generate_effect_token("clinic-voice-callback-token")
    monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", "https://clinic.example.test")

    def handler(request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode("utf-8"))
        assert parse_qs(urlsplit(body["Url"][0]).query)["effect_token"] == [token]
        assert parse_qs(urlsplit(body["StatusCallback"][0]).query)["effect_token"] == [token]
        assert parse_qs(urlsplit(body["RecordingStatusCallback"][0]).query)["effect_token"] == [
            recording_token
        ]
        assert "effect_token" not in body["Url"][0].split("effect_token=", 1)[0]
        return httpx.Response(201, json={"sid": "CA-token"})

    initiator = TwilioCallInitiator(
        account_sid="AC123",
        auth_token="secret",
        from_number="+447700900002",
        twiml_url="https://clinic.example.test/api/v1/voice/twilio/twiml",
        media_stream_url="wss://clinic.example.test/api/v1/twilio/stream",
        transport=httpx.MockTransport(handler),
    )

    result = initiator.initiate_call(
        target_number="+447700900001",
        context={
            "source": "non_durable_compatibility",
            "scenario": "customer_support",
            "clinic_id": "clinic-voice-callback-token",
            "record_call": True,
            "effect_token": token,
            "recording_effect_token": recording_token,
        },
    )

    assert result.successful is True


def test_inline_stream_twiml_announces_recording_only_when_consented() -> None:
    from src.clinic_recall.voice_worker import (
        RECORDING_ANNOUNCEMENT,
        _twilio_connect_stream_twiml,
    )

    recorded = _twilio_connect_stream_twiml(
        "wss://clinic.example.test/api/v1/twilio/stream", {"record_call": "true"}
    )
    assert f"<Say>{RECORDING_ANNOUNCEMENT}</Say><Connect>" in recorded

    unrecorded = _twilio_connect_stream_twiml(
        "wss://clinic.example.test/api/v1/twilio/stream", {"record_call": "false"}
    )
    assert "<Say>" not in unrecorded


def test_twilio_call_initiator_applies_time_limit_and_demo_context() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode("utf-8"))
        assert body["TimeLimit"] == ["75"]
        assert "Record" not in body
        twiml_url = body["Url"][0]
        query = parse_qs(urlsplit(twiml_url).query)
        assert query["source"] == ["clinic_recall_demo"]
        assert query["scenario"] == ["demo"]
        assert query["max_call_seconds"] == ["60"]
        assert query["demo_token"] == ["signed-token"]
        return httpx.Response(201, json={"sid": "CA-demo"})

    initiator = TwilioCallInitiator(
        account_sid="AC123",
        auth_token="secret",
        from_number="+447700900002",
        twiml_url="https://clinic.example.test/api/v1/voice/twilio/twiml",
        transport=httpx.MockTransport(handler),
    )

    result = initiator.initiate_call(
        target_number="+447700900001",
        context={
            "source": "clinic_recall_demo",
            "scenario": "demo",
            "demo_token": "signed-token",
            "max_call_seconds": 60,
            "time_limit_seconds": 75,
            "record_call": False,
        },
    )

    assert result.successful is True
    assert result.call_id == "CA-demo"


def test_twilio_call_initiator_can_send_inline_stream_twiml() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode("utf-8"))
        assert "Url" not in body
        assert "Record" not in body
        twiml = body["Twiml"][0]
        assert '<Connect><Stream url="wss://clinic.example.test/api/v1/twilio/stream">' in twiml
        assert '<Parameter name="scenario" value="rebooking" />' in twiml
        assert '<Parameter name="record_call" value="false" />' in twiml
        return httpx.Response(201, json={"sid": "CA456"})

    initiator = TwilioCallInitiator(
        account_sid="AC123",
        auth_token="secret",
        from_number="+447700900002",
        media_stream_url="wss://clinic.example.test/api/v1/twilio/stream",
        inline_twiml=True,
        transport=httpx.MockTransport(handler),
    )

    result = initiator.initiate_call(
        target_number="+447700900001",
        context={"scenario": "rebooking", "record_call": False},
    )

    assert result.successful is True
    assert result.call_id == "CA456"


def test_twilio_call_initiator_fails_closed_when_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_FROM_PHONE_NUMBER", raising=False)
    initiator = TwilioCallInitiator(twiml_url="https://clinic.example.test/twiml")

    result = initiator.initiate_call(target_number="+447700900001", context={})

    assert result.successful is False
    assert result.provider == "twilio"
    assert "TWILIO_ACCOUNT_SID" in (result.error or "")
    assert "TWILIO_AUTH_TOKEN" in (result.error or "")
    assert "TWILIO_FROM_PHONE_NUMBER" in (result.error or "")


def test_build_call_initiator_selects_twilio_with_shared_sms_voice_number(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("TWILIO_FROM_PHONE_NUMBER", "+447700900002")
    monkeypatch.setenv("TWILIO_VOICE_TWIML_URL", "https://clinic.example.test/twiml")

    initiator = build_call_initiator()

    assert isinstance(initiator, TwilioCallInitiator)
    assert initiator.from_number == "+447700900002"


def test_build_call_initiator_defaults_to_art_when_auto_has_art_url(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_PROVIDER", "auto")
    monkeypatch.setenv("BASE_URL", "https://clinic.example.test")

    initiator = build_call_initiator()

    assert isinstance(initiator, ArtCallInitiator)


def test_build_call_initiator_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="VOICE_PROVIDER"):
        build_call_initiator("carrier-pigeon")
