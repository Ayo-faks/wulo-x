"""Focused contracts for durable provider callback receipts."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from src.clinic_recall.durable import enqueue as enqueue_module
from src.clinic_recall.durable import reconcile as reconcile_module
from src.clinic_recall.durable.callbacks import (
    CallbackValidationError,
    EffectTokenError,
    claim_callback_receipts,
    effect_token_scope_id,
    generate_effect_token,
    parse_effect_token,
    receive_twilio_callback,
    reconcile_once,
)
from src.clinic_recall.durable.config import callback_application_enabled
from src.clinic_recall.durable.effects import (
    claim_effects,
    mark_dispatching,
    mark_succeeded,
)
from src.clinic_recall.durable.enqueue import enqueue_call_effect, enqueue_sms_effect
from src.clinic_recall.durable.reconcile import main as reconcile_main
from src.clinic_recall.enums import (
    ExternalEffectState,
    ExternalEffectType,
    OutreachState,
    ProviderCallbackKind,
    ProviderCallbackReason,
    ProviderCallbackState,
)
from src.clinic_recall.messaging.sender import twilio_sms_status_callback_url
from src.clinic_recall.models import (
    Appointment,
    Campaign,
    Clinic,
    ExternalEffect,
    Interaction,
    OutreachJob,
    Patient,
    ProviderCallbackReceipt,
)

NOW = datetime(2026, 7, 19, 9, 0, tzinfo=UTC)
MESSAGE_SID = "SM" + "a" * 32
CALL_SID = "CA" + "b" * 32
RECORDING_SID = "RE" + "e" * 32


def test_effect_token_round_trips_clinic_scope_without_domain_identifiers() -> None:
    first = generate_effect_token("clinic-callback-a")
    second = generate_effect_token("clinic-callback-a")

    assert first != second
    expected_scope = effect_token_scope_id("clinic-callback-a")
    assert parse_effect_token(first).scope_id == expected_scope
    assert parse_effect_token(second).scope_id == expected_scope
    assert parse_effect_token(generate_effect_token("clinic-callback-b")).scope_id != expected_scope
    assert first.split(".", maxsplit=2)[1] != "Y2xpbmljLWNhbGxiYWNrLWE"
    assert "patient" not in first
    assert "message" not in first
    assert "phone" not in first
    assert len(first) <= 240


@pytest.mark.parametrize(
    "token",
    [
        "",
        "cr1.invalid.token",
        "cr2.missing-random",
        "cr2.***.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "cr2.Y2xpbmljLWE.short",
        "x" * 241,
    ],
)
def test_effect_token_parser_fails_closed_on_malformed_or_oversized_values(
    token: str,
) -> None:
    with pytest.raises(EffectTokenError):
        parse_effect_token(token)


def test_effect_token_generator_rejects_unbounded_or_invalid_clinic_ids() -> None:
    for clinic_id in ("", " clinic-a", "clinic/a", "x" * 129):
        with pytest.raises(EffectTokenError):
            generate_effect_token(clinic_id)


def test_callback_application_environment_switch_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("CLINIC_RECALL_CALLBACK_APPLICATION_ENABLED", raising=False)
    assert callback_application_enabled() is False
    monkeypatch.setenv("CLINIC_RECALL_CALLBACK_APPLICATION_ENABLED", "true")
    assert callback_application_enabled() is True
    monkeypatch.setenv("CLINIC_RECALL_CALLBACK_APPLICATION_ENABLED", "unexpected")
    assert callback_application_enabled() is False


def test_enqueue_persists_one_pre_dispatch_token_for_duplicate_request(
    sqlite_session,
    monkeypatch,
) -> None:
    clinic_id = "clinic-callback-enqueue"
    expected_token = generate_effect_token(clinic_id)
    sqlite_session.add(Clinic(id=clinic_id, name="Callback Enqueue Clinic"))
    sqlite_session.commit()
    monkeypatch.setattr(
        enqueue_module,
        "generate_effect_token",
        lambda scoped_clinic_id: (
            expected_token
            if scoped_clinic_id == clinic_id
            else pytest.fail("unexpected clinic scope")
        ),
        raising=False,
    )

    first, first_created = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-callback-enqueue",
        idempotency_key="recall-sms:job-callback-enqueue",
        available_at=datetime(2026, 7, 19, 9, 0, tzinfo=UTC),
    )
    second, second_created = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-callback-enqueue",
        idempotency_key="recall-sms:job-callback-enqueue",
        available_at=datetime(2026, 7, 19, 9, 0, tzinfo=UTC),
    )

    assert first_created is True
    assert second_created is False
    assert first.callback_token == expected_token
    assert second.callback_token == expected_token


def test_sms_callback_url_includes_token_and_preserves_configured_query(
    monkeypatch,
) -> None:
    from urllib.parse import parse_qs, urlsplit

    token = generate_effect_token("clinic-callback-url")
    monkeypatch.setenv(
        "TWILIO_SMS_STATUS_CALLBACK_URL",
        "https://clinic.example.test/api/v1/sms/twilio?route=durable",
    )

    callback_url = twilio_sms_status_callback_url(token)

    assert callback_url is not None
    parts = urlsplit(callback_url)
    assert parts.scheme == "https"
    assert parts.netloc == "clinic.example.test"
    assert parts.path == "/api/v1/sms/twilio"
    assert parse_qs(parts.query) == {
        "effect_token": [token],
        "route": ["durable"],
    }


def _seed_dispatching_sms_effect(sqlite_session) -> tuple[str, ExternalEffect]:
    clinic_id = "clinic-callback-sms"
    sqlite_session.add(Clinic(id=clinic_id, name="Callback SMS Clinic"))
    sqlite_session.add(
        Patient(
            id="patient-callback-sms",
            clinic_id=clinic_id,
            source_ref="patient-callback-sms",
            name="Synthetic Callback Patient",
            phone="+447700900001",
            consent_flags={"sms": True},
            opt_out_flags={},
        )
    )
    sqlite_session.add(
        Appointment(
            id="appointment-callback-sms",
            clinic_id=clinic_id,
            patient_id="patient-callback-sms",
            source_ref="appointment-callback-sms",
            status="missed",
            start_at=NOW,
        )
    )
    sqlite_session.add(
        Campaign(
            id="campaign-callback-sms",
            clinic_id=clinic_id,
            type="recovery",
            status="active",
        )
    )
    sqlite_session.add(
        OutreachJob(
            id="job-callback-sms",
            clinic_id=clinic_id,
            campaign_id="campaign-callback-sms",
            patient_id="patient-callback-sms",
            appointment_id="appointment-callback-sms",
            channel="sms",
            state=OutreachState.QUEUED,
        )
    )
    sqlite_session.commit()
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-callback-sms",
        idempotency_key="recall-sms:job-callback-sms",
        available_at=NOW,
        max_attempts=1,
    )
    sqlite_session.commit()
    claim_effects(
        sqlite_session,
        clinic_id=clinic_id,
        worker_id="worker-callback",
        now=NOW,
        lease_for=timedelta(minutes=5),
    )
    mark_dispatching(
        sqlite_session,
        clinic_id=clinic_id,
        effect_id=effect.id,
        worker_id="worker-callback",
        now=NOW,
    )
    sqlite_session.commit()
    return clinic_id, effect


def _seed_dispatching_call_effect(sqlite_session) -> tuple[str, ExternalEffect]:
    clinic_id = "clinic-callback-call"
    sqlite_session.add(Clinic(id=clinic_id, name="Callback Call Clinic"))
    sqlite_session.add(
        Patient(
            id="patient-callback-call",
            clinic_id=clinic_id,
            source_ref="patient-callback-call",
            name="Synthetic Callback Call Patient",
            phone="+447700900011",
            consent_flags={"call": True},
            opt_out_flags={},
        )
    )
    sqlite_session.add(
        Campaign(
            id="campaign-callback-call",
            clinic_id=clinic_id,
            type="recovery",
            status="active",
        )
    )
    sqlite_session.add(
        OutreachJob(
            id="job-callback-call",
            clinic_id=clinic_id,
            campaign_id="campaign-callback-call",
            patient_id="patient-callback-call",
            channel="sms",
            state=OutreachState.NO_REPLY,
        )
    )
    sqlite_session.flush()
    effect, _ = enqueue_call_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-callback-call",
        idempotency_key="cadence:call:job-callback-call",
        available_at=NOW,
    )
    sqlite_session.commit()
    claim_effects(
        sqlite_session,
        clinic_id=clinic_id,
        worker_id="worker-call",
        now=NOW,
        lease_for=timedelta(minutes=5),
        effect_types=(ExternalEffectType.CALL,),
    )
    mark_dispatching(
        sqlite_session,
        clinic_id=clinic_id,
        effect_id=effect.id,
        worker_id="worker-call",
        now=NOW,
    )
    sqlite_session.commit()
    return clinic_id, effect


def _seed_dispatching_recording_effect(sqlite_session) -> tuple[str, ExternalEffect]:
    clinic_id = "clinic-callback-recording"
    sqlite_session.add(Clinic(id=clinic_id, name="Callback Recording Clinic"))
    effect = ExternalEffect(
        id="effect-callback-recording",
        clinic_id=clinic_id,
        aggregate_type="callback_test",
        aggregate_id="recording-callback-test",
        effect_type=ExternalEffectType.RECORDING,
        idempotency_key="recall-recording:callback-test",
        callback_token=generate_effect_token(clinic_id),
        payload_version=1,
        payload={"intent": "callback_test"},
        request_hash="3" * 64,
        state=ExternalEffectState.DISPATCHING,
        available_at=NOW,
        attempt_count=1,
        max_attempts=1,
        lease_owner="worker-recording",
    )
    sqlite_session.add(effect)
    sqlite_session.commit()
    return clinic_id, effect


def _sms_fields(status: str, *, message_sid: str | None = MESSAGE_SID) -> dict[str, str]:
    fields = {
        "AccountSid": "AC" + "c" * 32,
        "MessageStatus": status,
        "ErrorCode": "30003" if status == "undelivered" else "",
        "ExtraProviderField": "accepted-for-signature-only",
    }
    if message_sid is not None:
        fields["MessageSid"] = message_sid
    return fields


def test_duplicate_sms_callback_creates_one_receipt_and_applies_once(
    sqlite_session,
) -> None:
    clinic_id, effect = _seed_dispatching_sms_effect(sqlite_session)
    first = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.SMS,
        fields=_sms_fields("delivered"),
        raw_payload=b"synthetic-sms-delivered",
        received_at=NOW,
    )
    sqlite_session.commit()
    second = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.SMS,
        fields={**_sms_fields("delivered"), "AnotherProviderField": "new"},
        raw_payload=b"synthetic-sms-delivered-with-new-extra-field",
        received_at=NOW,
    )
    sqlite_session.commit()
    sqlite_session.expire_all()

    persisted = sqlite_session.get(ExternalEffect, effect.id)
    job = sqlite_session.get(OutreachJob, "job-callback-sms")
    assert first.created is True
    assert second.created is False
    assert first.receipt_id == second.receipt_id
    assert persisted is not None
    assert persisted.state == ExternalEffectState.SUCCEEDED
    assert persisted.provider_resource_id == MESSAGE_SID
    assert persisted.provider_status == "delivery_succeeded"
    assert persisted.attempt_count == 1
    assert job is not None and job.state == OutreachState.DELIVERED
    assert sqlite_session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 1
    assert sqlite_session.scalar(select(func.count()).select_from(Interaction)) == 0


def test_terminal_sms_then_intermediate_callback_is_stale_noop(sqlite_session) -> None:
    _clinic_id, effect = _seed_dispatching_sms_effect(sqlite_session)
    receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.SMS,
        fields=_sms_fields("delivered"),
        raw_payload=b"terminal-first",
        received_at=NOW,
    )
    stale = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.SMS,
        fields=_sms_fields("sending"),
        raw_payload=b"intermediate-late",
        received_at=NOW,
    )
    sqlite_session.commit()
    sqlite_session.expire_all()

    receipt = sqlite_session.get(ProviderCallbackReceipt, stale.receipt_id)
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    job = sqlite_session.get(OutreachJob, "job-callback-sms")
    assert receipt is not None
    assert receipt.state == ProviderCallbackState.APPLIED
    assert receipt.reason_code == ProviderCallbackReason.STALE_NOOP
    assert persisted is not None and persisted.provider_status == "delivery_succeeded"
    assert job is not None and job.state == OutreachState.DELIVERED


def test_conflicting_sms_terminal_evidence_quarantines_effect(sqlite_session) -> None:
    _clinic_id, effect = _seed_dispatching_sms_effect(sqlite_session)
    success = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.SMS,
        fields=_sms_fields("delivered"),
        raw_payload=b"terminal-success",
        received_at=NOW,
    )
    conflict = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.SMS,
        fields=_sms_fields("undelivered"),
        raw_payload=b"terminal-failure",
        received_at=NOW,
    )
    sqlite_session.commit()
    sqlite_session.expire_all()

    persisted = sqlite_session.get(ExternalEffect, effect.id)
    success_receipt = sqlite_session.get(ProviderCallbackReceipt, success.receipt_id)
    conflict_receipt = sqlite_session.get(ProviderCallbackReceipt, conflict.receipt_id)
    assert persisted is not None
    assert persisted.state == ExternalEffectState.RECONCILE_REQUIRED
    assert persisted.last_error_code == "conflicting_terminal"
    assert success_receipt is not None and success_receipt.state == ProviderCallbackState.APPLIED
    assert conflict_receipt is not None
    assert conflict_receipt.state == ProviderCallbackState.RECONCILE_REQUIRED
    assert conflict_receipt.reason_code == ProviderCallbackReason.CONFLICTING_TERMINAL

    later = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.SMS,
        fields=_sms_fields("read"),
        raw_payload=b"later-success-cannot-resolve-conflict",
        received_at=NOW + timedelta(seconds=1),
    )
    sqlite_session.commit()
    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    later_receipt = sqlite_session.get(ProviderCallbackReceipt, later.receipt_id)
    assert persisted is not None
    assert persisted.state == ExternalEffectState.RECONCILE_REQUIRED
    assert persisted.last_error_code == "conflicting_terminal"
    assert later_receipt is not None
    assert later_receipt.state == ProviderCallbackState.RECONCILE_REQUIRED
    assert later_receipt.reason_code == ProviderCallbackReason.CONFLICTING_TERMINAL


def test_later_matching_provider_response_converges_after_early_callback(
    sqlite_session,
) -> None:
    clinic_id, effect = _seed_dispatching_sms_effect(sqlite_session)
    receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.SMS,
        fields=_sms_fields("delivered"),
        raw_payload=b"callback-before-provider-response",
        received_at=NOW,
    )
    sqlite_session.flush()

    settled = mark_succeeded(
        sqlite_session,
        clinic_id=clinic_id,
        effect_id=effect.id,
        worker_id="worker-callback",
        now=NOW + timedelta(seconds=1),
        provider_resource_id=MESSAGE_SID,
    )

    assert settled.state == ExternalEffectState.SUCCEEDED
    assert settled.provider_resource_id == MESSAGE_SID
    assert settled.provider_status == "delivery_succeeded"
    assert settled.attempt_count == 1


def test_voice_callbacks_advance_by_sequence_not_arrival_order(sqlite_session) -> None:
    _clinic_id, effect = _seed_dispatching_call_effect(sqlite_session)
    completed = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.VOICE,
        fields={
            "CallSid": CALL_SID,
            "CallStatus": "completed",
            "SequenceNumber": "3",
            "Timestamp": "Sun, 19 Jul 2026 09:03:00 +0000",
        },
        raw_payload=b"voice-sequence-three",
        received_at=NOW,
    )
    stale = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.VOICE,
        fields={
            "CallSid": CALL_SID,
            "CallStatus": "ringing",
            "SequenceNumber": "1",
            "Timestamp": "Sun, 19 Jul 2026 09:01:00 +0000",
        },
        raw_payload=b"voice-sequence-one",
        received_at=NOW,
    )
    sqlite_session.commit()
    sqlite_session.expire_all()

    persisted = sqlite_session.get(ExternalEffect, effect.id)
    completed_receipt = sqlite_session.get(ProviderCallbackReceipt, completed.receipt_id)
    stale_receipt = sqlite_session.get(ProviderCallbackReceipt, stale.receipt_id)
    job = sqlite_session.get(OutreachJob, "job-callback-call")
    assert persisted is not None
    assert persisted.provider_sequence == 3
    assert persisted.provider_status == "call_completed"
    assert persisted.provider_status not in {"delivery_succeeded", "human_confirmed"}
    assert completed_receipt is not None
    observed_at = completed_receipt.provider_observed_at
    assert observed_at is not None
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    assert observed_at == datetime(2026, 7, 19, 9, 3, tzinfo=UTC)
    assert stale_receipt is not None
    assert stale_receipt.reason_code == ProviderCallbackReason.STALE_NOOP
    assert job is not None and job.state == OutreachState.NO_REPLY


def test_voice_same_sequence_with_different_status_quarantines_identity(
    sqlite_session,
) -> None:
    _clinic_id, effect = _seed_dispatching_call_effect(sqlite_session)
    first = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.VOICE,
        fields={
            "CallSid": CALL_SID,
            "CallStatus": "completed",
            "SequenceNumber": "3",
        },
        raw_payload=b"voice-sequence-three-completed",
        received_at=NOW,
    )
    conflicting = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.VOICE,
        fields={
            "CallSid": CALL_SID,
            "CallStatus": "failed",
            "SequenceNumber": "3",
        },
        raw_payload=b"voice-sequence-three-failed",
        received_at=NOW + timedelta(seconds=1),
    )
    sqlite_session.commit()
    sqlite_session.expire_all()

    persisted = sqlite_session.get(ExternalEffect, effect.id)
    receipt = sqlite_session.get(ProviderCallbackReceipt, first.receipt_id)
    assert conflicting.created is False
    assert conflicting.receipt_id == first.receipt_id
    assert persisted is not None
    assert persisted.state == ExternalEffectState.RECONCILE_REQUIRED
    assert persisted.last_error_code == "conflicting_terminal"
    assert receipt is not None
    assert receipt.state == ProviderCallbackState.RECONCILE_REQUIRED
    assert receipt.reason_code == ProviderCallbackReason.CONFLICTING_TERMINAL


def test_missing_provider_identity_uses_payload_hash_fallback_without_applying(
    sqlite_session,
) -> None:
    _clinic_id, effect = _seed_dispatching_sms_effect(sqlite_session)
    first = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.SMS,
        fields=_sms_fields("sent", message_sid=None),
        raw_payload=b"same-minimized-fallback-payload",
        received_at=NOW,
    )
    second = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.SMS,
        fields=_sms_fields("sent", message_sid=None),
        raw_payload=b"different-raw-payload-with-provider-added-fields",
        received_at=NOW,
    )
    sqlite_session.commit()
    sqlite_session.expire_all()

    receipt = sqlite_session.get(ProviderCallbackReceipt, first.receipt_id)
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert first.created is True
    assert second.created is False
    assert receipt is not None
    assert receipt.provider_resource_id is None
    assert receipt.state == ProviderCallbackState.RECONCILE_REQUIRED
    assert receipt.reason_code == ProviderCallbackReason.MISSING_EVIDENCE
    assert len(receipt.payload_hash) == 64
    assert persisted is not None and persisted.state == ExternalEffectState.DISPATCHING


def test_recording_identity_is_sid_plus_status_and_duplicate_is_noop(
    sqlite_session,
) -> None:
    _clinic_id, effect = _seed_dispatching_recording_effect(sqlite_session)
    fields = {
        "RecordingSid": RECORDING_SID,
        "RecordingStatus": "completed",
        "RecordingStartTime": "Sun, 19 Jul 2026 09:00:00 +0000",
        "RecordingUrl": "https://provider.invalid/sensitive-recording-url",
    }
    first = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.RECORDING,
        fields=fields,
        raw_payload=b"recording-completed",
        received_at=NOW,
    )
    second = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.RECORDING,
        fields=fields,
        raw_payload=b"recording-completed",
        received_at=NOW,
    )
    sqlite_session.commit()
    receipt = sqlite_session.get(ProviderCallbackReceipt, first.receipt_id)

    assert first.created is True
    assert second.created is False
    assert receipt is not None
    assert receipt.provider_resource_id == RECORDING_SID
    assert receipt.normalized_status == "completed"
    assert "sensitive-recording-url" not in repr(receipt.__dict__)
    assert sqlite_session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 1


def test_conflicting_amd_decisions_are_retained_and_quarantined(sqlite_session) -> None:
    _clinic_id, effect = _seed_dispatching_call_effect(sqlite_session)
    human = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.AMD,
        fields={"CallSid": CALL_SID, "AnsweredBy": "human"},
        raw_payload=b"amd-human",
        received_at=NOW,
    )
    machine = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.AMD,
        fields={"CallSid": CALL_SID, "AnsweredBy": "machine_start"},
        raw_payload=b"amd-machine",
        received_at=NOW,
    )
    sqlite_session.commit()
    sqlite_session.expire_all()

    persisted = sqlite_session.get(ExternalEffect, effect.id)
    human_receipt = sqlite_session.get(ProviderCallbackReceipt, human.receipt_id)
    machine_receipt = sqlite_session.get(ProviderCallbackReceipt, machine.receipt_id)
    assert persisted is not None
    assert persisted.state == ExternalEffectState.RECONCILE_REQUIRED
    assert persisted.provider_status == "human_confirmed"
    assert human_receipt is not None and human_receipt.state == ProviderCallbackState.APPLIED
    assert machine_receipt is not None
    assert machine_receipt.state == ProviderCallbackState.RECONCILE_REQUIRED
    assert machine_receipt.reason_code == ProviderCallbackReason.CONFLICTING_TERMINAL

    later_human = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.AMD,
        fields={"CallSid": CALL_SID, "AnsweredBy": "human"},
        raw_payload=b"amd-human-later-match",
        received_at=NOW + timedelta(seconds=2),
    )
    sqlite_session.commit()
    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert later_human.created is False
    assert persisted is not None
    assert persisted.state == ExternalEffectState.RECONCILE_REQUIRED
    assert persisted.last_error_code == "conflicting_terminal"


def test_amd_unknown_remains_visible_and_unresolved(sqlite_session) -> None:
    clinic_id, effect = _seed_dispatching_call_effect(sqlite_session)
    result = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.AMD,
        fields={"CallSid": CALL_SID, "AnsweredBy": "unknown"},
        raw_payload=b"amd-unknown",
        received_at=NOW,
    )
    sqlite_session.commit()
    summary = reconcile_once(
        sessionmaker(bind=sqlite_session.bind, expire_on_commit=False),
        clinic_id=clinic_id,
        worker_id="worker-amd-unknown",
        now=NOW + timedelta(seconds=1),
        enabled=True,
    )
    sqlite_session.expire_all()
    receipt = sqlite_session.get(ProviderCallbackReceipt, result.receipt_id)
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    job = sqlite_session.get(OutreachJob, "job-callback-call")

    assert receipt is not None
    assert receipt.state == ProviderCallbackState.RECONCILE_REQUIRED
    assert receipt.reason_code == ProviderCallbackReason.MISSING_EVIDENCE
    assert persisted is not None
    assert persisted.state == ExternalEffectState.RECONCILE_REQUIRED
    assert persisted.provider_status == "provider_unresolved"
    assert job is not None and job.state == OutreachState.NO_REPLY
    assert summary.claimed == 0
    assert summary.unresolved_effects == 1


def test_human_after_unknown_amd_latches_conflict_instead_of_clearing_it(
    sqlite_session,
) -> None:
    _clinic_id, effect = _seed_dispatching_call_effect(sqlite_session)
    unknown = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.AMD,
        fields={"CallSid": CALL_SID, "AnsweredBy": "unknown"},
        raw_payload=b"amd-unknown-before-human",
        received_at=NOW,
    )
    human = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.AMD,
        fields={"CallSid": CALL_SID, "AnsweredBy": "human"},
        raw_payload=b"amd-human-after-unknown",
        received_at=NOW + timedelta(seconds=1),
    )
    sqlite_session.commit()
    sqlite_session.expire_all()

    persisted = sqlite_session.get(ExternalEffect, effect.id)
    unknown_receipt = sqlite_session.get(ProviderCallbackReceipt, unknown.receipt_id)
    human_receipt = sqlite_session.get(ProviderCallbackReceipt, human.receipt_id)
    assert persisted is not None
    assert persisted.state == ExternalEffectState.RECONCILE_REQUIRED
    assert persisted.provider_status == "provider_unresolved"
    assert persisted.last_error_class == "ProviderCallbackConflict"
    assert persisted.last_error_code == "conflicting_terminal"
    assert unknown_receipt is not None
    assert unknown_receipt.state == ProviderCallbackState.RECONCILE_REQUIRED
    assert human_receipt is not None
    assert human_receipt.state == ProviderCallbackState.RECONCILE_REQUIRED
    assert human_receipt.reason_code == ProviderCallbackReason.CONFLICTING_TERMINAL


def test_call_completion_cannot_clear_unknown_amd_evidence(sqlite_session) -> None:
    _clinic_id, effect = _seed_dispatching_call_effect(sqlite_session)
    receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.AMD,
        fields={"CallSid": CALL_SID, "AnsweredBy": "unknown"},
        raw_payload=b"amd-unknown-before-completion",
        received_at=NOW,
    )
    completion = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.VOICE,
        fields={
            "CallSid": CALL_SID,
            "CallStatus": "completed",
            "SequenceNumber": "4",
        },
        raw_payload=b"call-completed-after-unknown",
        received_at=NOW + timedelta(seconds=1),
    )
    sqlite_session.commit()
    sqlite_session.expire_all()

    persisted = sqlite_session.get(ExternalEffect, effect.id)
    completion_receipt = sqlite_session.get(
        ProviderCallbackReceipt,
        completion.receipt_id,
    )
    job = sqlite_session.get(OutreachJob, "job-callback-call")
    assert persisted is not None
    assert persisted.state == ExternalEffectState.RECONCILE_REQUIRED
    assert persisted.provider_status == "provider_unresolved"
    assert persisted.provider_sequence == 4
    assert persisted.last_error_class == "ProviderCallbackUnresolved"
    assert persisted.last_error_code == ProviderCallbackReason.MISSING_EVIDENCE.value
    assert completion_receipt is not None
    assert completion_receipt.state == ProviderCallbackState.APPLIED
    assert job is not None and job.state == OutreachState.NO_REPLY


def test_early_human_amd_and_provider_response_converge_on_one_call_effect(
    sqlite_session,
) -> None:
    clinic_id, effect = _seed_dispatching_call_effect(sqlite_session)
    callback = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.AMD,
        fields={"CallSid": CALL_SID, "AnsweredBy": "human"},
        raw_payload=b"amd-before-provider-response",
        received_at=NOW,
    )
    settled = mark_succeeded(
        sqlite_session,
        clinic_id=clinic_id,
        effect_id=effect.id,
        worker_id="worker-call",
        now=NOW + timedelta(seconds=1),
        provider_resource_id=CALL_SID,
    )
    sqlite_session.commit()
    sqlite_session.expire_all()

    persisted = sqlite_session.get(ExternalEffect, effect.id)
    receipt = sqlite_session.get(ProviderCallbackReceipt, callback.receipt_id)
    job = sqlite_session.get(OutreachJob, "job-callback-call")
    assert settled.state == ExternalEffectState.SUCCEEDED
    assert persisted is not None
    assert persisted.state == ExternalEffectState.SUCCEEDED
    assert persisted.provider_resource_id == CALL_SID
    assert persisted.provider_status == "human_confirmed"
    assert persisted.attempt_count == 1
    assert receipt is not None and receipt.state == ProviderCallbackState.APPLIED
    assert job is not None and job.state == OutreachState.NO_REPLY
    assert sqlite_session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 1


def test_duplicate_human_amd_fields_create_one_semantic_receipt(
    sqlite_session,
) -> None:
    _clinic_id, effect = _seed_dispatching_call_effect(sqlite_session)
    first = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.AMD,
        fields={"CallSid": CALL_SID, "AnsweredBy": "human", "FutureField": "one"},
        raw_payload=b"amd-human-first",
        received_at=NOW,
    )
    second = receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.AMD,
        fields={"CallSid": CALL_SID, "AnsweredBy": "human", "FutureField": "two"},
        raw_payload=b"amd-human-second-with-new-field",
        received_at=NOW + timedelta(seconds=1),
    )
    sqlite_session.commit()

    assert first.created is True
    assert second.created is False
    assert second.receipt_id == first.receipt_id
    assert sqlite_session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 1


def test_human_amd_is_not_overwritten_or_called_completed_by_later_call_status(
    sqlite_session,
) -> None:
    _clinic_id, effect = _seed_dispatching_call_effect(sqlite_session)
    receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.AMD,
        fields={"CallSid": CALL_SID, "AnsweredBy": "human"},
        raw_payload=b"amd-human-canonical",
        received_at=NOW,
    )
    receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.VOICE,
        fields={
            "CallSid": CALL_SID,
            "CallStatus": "completed",
            "SequenceNumber": "4",
        },
        raw_payload=b"call-completed-is-not-human-proof",
        received_at=NOW + timedelta(seconds=1),
    )
    sqlite_session.commit()
    sqlite_session.expire_all()

    persisted = sqlite_session.get(ExternalEffect, effect.id)
    job = sqlite_session.get(OutreachJob, "job-callback-call")
    assert persisted is not None
    assert persisted.provider_status == "human_confirmed"
    assert persisted.provider_sequence == 4
    assert job is not None and job.state == OutreachState.NO_REPLY


@pytest.mark.parametrize("answered_by", ["machine_start", "fax"])
def test_non_human_amd_is_canonical_but_not_delivery_or_job_completion(
    sqlite_session,
    answered_by: str,
) -> None:
    _clinic_id, effect = _seed_dispatching_call_effect(sqlite_session)
    receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.AMD,
        fields={"CallSid": CALL_SID, "AnsweredBy": answered_by},
        raw_payload=f"amd-{answered_by}".encode(),
        received_at=NOW,
    )
    sqlite_session.commit()
    sqlite_session.expire_all()

    persisted = sqlite_session.get(ExternalEffect, effect.id)
    job = sqlite_session.get(OutreachJob, "job-callback-call")
    assert persisted is not None
    assert persisted.state == ExternalEffectState.SUCCEEDED
    assert persisted.provider_status == "non_human_confirmed"
    assert persisted.provider_status not in {"delivery_succeeded", "human_confirmed"}
    assert job is not None and job.state == OutreachState.NO_REPLY
    assert claim_effects(
        sqlite_session,
        clinic_id="clinic-callback-call",
        worker_id=f"worker-redial-{answered_by}",
        now=NOW + timedelta(seconds=1),
        lease_for=timedelta(minutes=5),
        effect_types=(ExternalEffectType.CALL,),
    ) == []


@pytest.mark.parametrize(
    ("status", "message_sid"),
    [("invented", MESSAGE_SID), ("delivered", "SM" + "x" * 255)],
)
def test_unknown_status_or_oversized_field_creates_no_receipt(
    sqlite_session,
    status: str,
    message_sid: str,
) -> None:
    _clinic_id, effect = _seed_dispatching_sms_effect(sqlite_session)
    with pytest.raises(CallbackValidationError):
        receive_twilio_callback(
            sqlite_session,
            effect_token=effect.callback_token,
            callback_kind=ProviderCallbackKind.SMS,
            fields=_sms_fields(status, message_sid=message_sid),
            raw_payload=b"invalid-callback",
            received_at=NOW,
        )
    assert sqlite_session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 0


def test_oversized_unknown_provider_field_creates_no_receipt(sqlite_session) -> None:
    _clinic_id, effect = _seed_dispatching_sms_effect(sqlite_session)
    with pytest.raises(CallbackValidationError):
        receive_twilio_callback(
            sqlite_session,
            effect_token=effect.callback_token,
            callback_kind=ProviderCallbackKind.SMS,
            fields={**_sms_fields("delivered"), "FutureProviderField": "x" * 16_385},
            raw_payload=b"bounded-provider-shape",
            received_at=NOW,
        )
    assert sqlite_session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 0


def _pending_sms_receipt(
    sqlite_session,
    effect: ExternalEffect,
    *,
    receipt_id: str = "receipt-reconcile",
) -> ProviderCallbackReceipt:
    receipt = ProviderCallbackReceipt(
        id=receipt_id,
        clinic_id=effect.clinic_id,
        external_effect_id=effect.id,
        provider="twilio",
        callback_kind=ProviderCallbackKind.SMS,
        deduplication_hash="a" * 64,
        effect_token_hash="b" * 64,
        provider_resource_id=MESSAGE_SID,
        normalized_status="delivered",
        payload_hash="c" * 64,
        state=ProviderCallbackState.PENDING,
        received_at=NOW,
    )
    sqlite_session.add(receipt)
    sqlite_session.commit()
    return receipt


def test_reconciliation_is_disabled_without_opening_database() -> None:
    result = reconcile_once(
        lambda: pytest.fail("disabled reconciliation opened the database"),
        clinic_id="clinic-disabled",
        worker_id="worker-disabled",
        now=NOW,
    )

    assert result.enabled is False
    assert result.as_summary() == {
        "applied": 0,
        "claimed": 0,
        "conflicts": 0,
        "enabled": False,
        "pending": 0,
        "unresolved_effects": 0,
    }


def test_reconciliation_applies_pending_receipt_once_without_dispatch(
    sqlite_session,
) -> None:
    _clinic_id, effect = _seed_dispatching_sms_effect(sqlite_session)
    _pending_sms_receipt(sqlite_session, effect)
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)

    first = reconcile_once(
        factory,
        clinic_id=effect.clinic_id,
        worker_id="worker-reconcile-a",
        now=NOW,
        enabled=True,
    )
    second = reconcile_once(
        factory,
        clinic_id=effect.clinic_id,
        worker_id="worker-reconcile-b",
        now=NOW + timedelta(minutes=1),
        enabled=True,
    )
    sqlite_session.expire_all()
    receipt = sqlite_session.get(ProviderCallbackReceipt, "receipt-reconcile")
    persisted = sqlite_session.get(ExternalEffect, effect.id)

    assert first.claimed == 1
    assert first.applied == 1
    assert second.claimed == 0
    assert receipt is not None and receipt.state == ProviderCallbackState.APPLIED
    assert persisted is not None and persisted.provider_status == "delivery_succeeded"
    assert persisted.attempt_count == 1


def test_reconciliation_never_invokes_message_or_voice_dispatch(
    sqlite_session,
    monkeypatch,
) -> None:
    from src.clinic_recall.messaging.sender import AcsSmsSender
    from src.clinic_recall.voice_worker import TwilioCallInitiator

    def forbidden_dispatch(*_args, **_kwargs):
        pytest.fail("reconciliation attempted an external dispatch")

    monkeypatch.setattr(AcsSmsSender, "send_sms", forbidden_dispatch)
    monkeypatch.setattr(AcsSmsSender, "send_email", forbidden_dispatch)
    monkeypatch.setattr(TwilioCallInitiator, "initiate_call", forbidden_dispatch)
    _clinic_id, effect = _seed_dispatching_sms_effect(sqlite_session)
    _pending_sms_receipt(sqlite_session, effect, receipt_id="receipt-no-dispatch")

    result = reconcile_once(
        sessionmaker(bind=sqlite_session.bind, expire_on_commit=False),
        clinic_id=effect.clinic_id,
        worker_id="worker-no-dispatch",
        now=NOW,
        enabled=True,
    )

    assert result.applied == 1
    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert persisted is not None
    assert persisted.state not in {
        ExternalEffectState.PENDING,
        ExternalEffectState.LEASED,
        ExternalEffectState.DISPATCHING,
    }


def test_callback_receipt_lease_expiry_is_finite_and_deterministic(
    sqlite_session,
) -> None:
    _clinic_id, effect = _seed_dispatching_sms_effect(sqlite_session)
    _pending_sms_receipt(sqlite_session, effect)

    first = claim_callback_receipts(
        sqlite_session,
        clinic_id=effect.clinic_id,
        worker_id="worker-lease-a",
        now=NOW,
        lease_for=timedelta(minutes=5),
        limit=1,
    )
    sqlite_session.commit()
    before_expiry = claim_callback_receipts(
        sqlite_session,
        clinic_id=effect.clinic_id,
        worker_id="worker-lease-b",
        now=NOW + timedelta(minutes=4),
        lease_for=timedelta(minutes=5),
        limit=1,
    )
    after_expiry = claim_callback_receipts(
        sqlite_session,
        clinic_id=effect.clinic_id,
        worker_id="worker-lease-b",
        now=NOW + timedelta(minutes=6),
        lease_for=timedelta(minutes=5),
        limit=1,
    )

    assert first == ["receipt-reconcile"]
    assert before_expiry == []
    assert after_expiry == ["receipt-reconcile"]


def test_reconciliation_leaves_effect_without_receipt_or_provider_id_unresolved(
    sqlite_session,
) -> None:
    clinic_id = "clinic-retained-timeout"
    sqlite_session.add(Clinic(id=clinic_id, name="Retained Timeout Clinic"))
    effect = ExternalEffect(
        id="effect-retained-timeout",
        clinic_id=clinic_id,
        aggregate_type="outreach_job",
        aggregate_id="job-retained-timeout",
        effect_type=ExternalEffectType.SMS,
        idempotency_key="recall-sms:retained-timeout",
        callback_token=generate_effect_token(clinic_id),
        payload_version=1,
        payload={"intent": "recall", "outreach_job_id": "job-retained-timeout"},
        request_hash="5" * 64,
        state=ExternalEffectState.RECONCILE_REQUIRED,
        available_at=NOW,
        attempt_count=1,
        max_attempts=1,
        last_error_class="ProviderDispatchError",
        last_error_code="provider_outcome_unknown",
    )
    sqlite_session.add(effect)
    sqlite_session.commit()
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)

    result = reconcile_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-retained-timeout",
        now=NOW,
        enabled=True,
    )
    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)

    assert result.claimed == 0
    assert result.unresolved_effects == 1
    assert persisted is not None
    assert persisted.state == ExternalEffectState.RECONCILE_REQUIRED
    assert persisted.provider_resource_id is None
    assert persisted.last_error_code == "provider_outcome_unknown"


def test_callback_transaction_rollback_restores_effect_and_receipt_state(
    sqlite_session,
) -> None:
    _clinic_id, effect = _seed_dispatching_sms_effect(sqlite_session)
    receive_twilio_callback(
        sqlite_session,
        effect_token=effect.callback_token,
        callback_kind=ProviderCallbackKind.SMS,
        fields=_sms_fields("delivered"),
        raw_payload=b"rollback-callback",
        received_at=NOW,
    )
    sqlite_session.rollback()
    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)

    assert sqlite_session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 0
    assert persisted is not None
    assert persisted.state == ExternalEffectState.DISPATCHING
    assert persisted.provider_resource_id is None


def test_reconciliation_cli_is_off_by_default_and_structurally_cannot_send(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("CLINIC_RECALL_CALLBACK_RECONCILIATION_ENABLED", raising=False)
    monkeypatch.setattr(
        reconcile_module,
        "get_sessionmaker",
        lambda: pytest.fail("disabled reconciliation CLI opened the database"),
    )

    assert reconcile_main(["--clinic-id", "clinic-cli-disabled"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "applied": 0,
        "claimed": 0,
        "conflicts": 0,
        "enabled": False,
        "pending": 0,
        "unresolved_effects": 0,
    }
    source = inspect.getsource(reconcile_module)
    for forbidden in (
        "send_sms",
        "send_email",
        "initiate_call",
        "MessageSender",
        "CallInitiator",
        "TwilioSmsSender",
    ):
        assert forbidden not in source
