"""Focused contracts for the first durable Clinic Recall SMS effect."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from src.clinic_recall.availability import AvailabilitySlotInput, upsert_availability_slots
from src.clinic_recall.booking import book_slot
from src.clinic_recall.durable.callbacks import generate_effect_token
from src.clinic_recall.durable.cliniko_booking_state import preflight_zero_match_hash
from src.clinic_recall.durable.config import durable_sms_enabled
from src.clinic_recall.durable.effects import (
    claim_effects,
    mark_dispatching,
    mark_retryable_failure,
)
from src.clinic_recall.durable.enqueue import (
    enqueue_booking_confirmation_effect,
    enqueue_sms_effect,
)
from src.clinic_recall.durable.worker import (
    _bootstrap_runtime_configuration,
)
from src.clinic_recall.durable.worker import (
    main as worker_main,
)
from src.clinic_recall.durable.worker import (
    run_once as _run_once,
)
from src.clinic_recall.enums import (
    BookingWriteBackState,
    Channel,
    ExternalEffectState,
    ExternalEffectType,
    SkipReason,
)
from src.clinic_recall.identity_evidence import IdentityEvidenceService
from src.clinic_recall.messaging.sender import (
    AcsSmsSender,
    FakeMessageSender,
    ProviderOutcomeUnknownError,
    SendResult,
)
from src.clinic_recall.models import (
    Appointment,
    BookingAction,
    Campaign,
    Clinic,
    ExternalEffect,
    HandoffReceipt,
    Interaction,
    OutreachJob,
    Patient,
)
from src.clinic_recall.pilot_controls import PilotGateDecision
from src.clinic_recall.sync.cliniko_booking import (
    ExpectedAppointmentSignature,
    ObservedAppointment,
)

from tests.identity_evidence_support import (
    grant_synthetic_t2,
    synthetic_identity_policy,
)

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _allow_pilot(*_args) -> PilotGateDecision:
    return PilotGateDecision(True, "allowed")


def run_once(*args, **kwargs):
    kwargs.setdefault("programme_gate", _allow_pilot)
    kwargs.setdefault("identity_service", _identity_service())
    return _run_once(*args, **kwargs)


def _identity_service() -> IdentityEvidenceService:
    return IdentityEvidenceService(
        policy=synthetic_identity_policy(),
        clock=lambda: NOW,
        identifier_factory=lambda: "unused-durable-sms-identity",
        challenge_factory=lambda: "unused-durable-sms-challenge",
    )


def _seed_clinic(session: Session) -> str:
    clinic_id = "clinic-durable-sms"
    session.add(Clinic(id=clinic_id, name="Durable SMS Clinic"))
    session.commit()
    return clinic_id


def _seed_sms_job(session: Session, *, opted_out: bool = False) -> str:
    clinic_id = _seed_clinic(session)
    session.add(
        Patient(
            id="patient-durable-sms",
            clinic_id=clinic_id,
            source_ref="patient-ref-001",
            name="Synthetic Patient",
            phone="+447700900001",
            consent_flags={"sms": True},
            opt_out_flags={"sms": opted_out},
        )
    )
    session.add(
        Appointment(
            id="appointment-durable-sms",
            clinic_id=clinic_id,
            patient_id="patient-durable-sms",
            source_ref="appointment-ref-001",
            status="missed",
            start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
        )
    )
    session.add(
        Campaign(
            id="campaign-durable-sms",
            clinic_id=clinic_id,
            type="recovery",
            status="active",
        )
    )
    session.add(
        OutreachJob(
            id="job-durable-sms",
            clinic_id=clinic_id,
            campaign_id="campaign-durable-sms",
            patient_id="patient-durable-sms",
            appointment_id="appointment-durable-sms",
            channel="sms",
            state="queued",
        )
    )
    session.commit()
    return clinic_id


def _seed_verified_confirmation(session: Session) -> tuple[str, ExternalEffect]:
    clinic_id = _seed_sms_job(session)
    patient = session.get(Patient, "patient-durable-sms")
    assert patient is not None
    patient.source_ref = "900700001"
    patient.consent_flags = {"sms": True, "call": True}
    slot = upsert_availability_slots(
        session,
        clinic_id,
        [
            AvailabilitySlotInput(
                source_ref="cliniko:v1:" + "8" * 64,
                source_provider="cliniko",
                business_id="920700001",
                clinician_id="930700001",
                appointment_type_id="940700001",
                start_at=NOW + timedelta(days=2),
                end_at=NOW + timedelta(days=2, minutes=30),
                fetched_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
            )
        ],
        now=NOW,
    )[0]
    identity_service, identity_context = grant_synthetic_t2(
        session,
        clinic_id=clinic_id,
        patient_id="patient-durable-sms",
        channel=Channel.SMS,
        now=NOW,
        suffix="durable-sms-confirmation",
    )
    booking = book_slot(
        session,
        clinic_id,
        patient_id="patient-durable-sms",
        outreach_job_id="job-durable-sms",
        slot_id=slot.slot_id,
        now=NOW,
        write_back_enabled=True,
        identity_service=identity_service,
        identity_context=identity_context,
    )
    action = session.get(BookingAction, booking.booking_action_id)
    assert action is not None
    booking_effect = session.execute(
        select(ExternalEffect).where(
            ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
        )
    ).scalar_one()
    action.write_back_state = BookingWriteBackState.VERIFIED
    action.written_back = True
    action.external_appointment_ref = "950700001"
    action.provider_attempted_at = NOW
    action.read_back_verified_at = NOW
    booking_effect.state = ExternalEffectState.SUCCEEDED
    booking_effect.provider_resource_id = "950700001"
    booking_effect.provider_status = "verified"
    booking_effect.dispatch_started_at = NOW
    booking_effect.preflight_evidence_hash = preflight_zero_match_hash(
        booking_effect.request_hash
    )
    observed = ObservedAppointment(
        provider_id="950700001",
        signature=ExpectedAppointmentSignature(
            patient_id=patient.source_ref,
            business_id=slot.business_id or "",
            practitioner_id=slot.clinician_id or "",
            appointment_type_id=slot.appointment_type_id or "",
            starts_at=slot.start_at,
            ends_at=slot.end_at,
        ),
        active=True,
        updated_at=NOW,
    )
    completion_evidence_hash = observed.completion_hash(booking_effect.request_hash)
    booking_effect.completion_evidence_hash = completion_evidence_hash
    booking_effect.completed_at = NOW
    confirmation_effect, created = enqueue_booking_confirmation_effect(
        session,
        clinic_id=clinic_id,
        outreach_job_id="job-durable-sms",
        booking_action_id=action.id,
        completion_evidence_hash=completion_evidence_hash,
        available_at=NOW,
    )
    assert created is True
    session.commit()
    return clinic_id, confirmation_effect


class _AmbiguousSmsSender:
    name = "ambiguous-test"

    def __init__(self) -> None:
        self.calls = 0

    def send_sms(
        self,
        *,
        to: str,
        body: str,
        tag: str | None = None,
        status_callback_url: str | None = None,
    ):
        self.calls += 1
        raise TimeoutError(f"unknown outcome for Synthetic Patient at {to}: {body}")

    def send_email(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        html_body: str | None = None,
    ):
        raise AssertionError("email is outside the durable SMS slice")


class _OutcomeUnknownService:
    async def send_sms(self, **_kwargs):
        return {
            "success": False,
            "error": "provider_outcome_unknown",
            "outcome_unknown": True,
            "sent_messages": [],
            "failed_messages": [],
        }


class _RateLimitedSmsSender:
    name = "rate-limited-test"

    def __init__(self) -> None:
        self.calls = 0

    def send_sms(self, **_kwargs) -> SendResult:
        self.calls += 1
        return SendResult(
            successful=False,
            provider=self.name,
            http_status_code=429,
            error="Synthetic Patient +447700900001 retry after raw provider detail",
        )

    def send_email(self, **_kwargs) -> SendResult:
        raise AssertionError("email is outside the durable SMS slice")


def test_transaction_rollback_creates_no_effect(sqlite_session: Session) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    effect, created = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-internal-001",
        idempotency_key="recall-sms:job-internal-001",
        available_at=NOW,
    )
    assert created is True
    assert effect.state == ExternalEffectState.PENDING
    sqlite_session.rollback()
    assert sqlite_session.scalar(select(func.count()).select_from(ExternalEffect)) == 0


def test_duplicate_logical_enqueue_returns_one_effect(sqlite_session: Session) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    first, first_created = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-internal-001",
        idempotency_key="recall-sms:job-internal-001",
        available_at=NOW,
    )
    second, second_created = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-internal-001",
        idempotency_key="recall-sms:job-internal-001",
        available_at=NOW,
    )
    assert first.effect_type == ExternalEffectType.SMS
    assert first.id == second.id
    assert first_created is True
    assert second_created is False
    assert sqlite_session.scalar(select(func.count()).select_from(ExternalEffect)) == 1


def test_enqueue_normalizes_available_at_to_utc(sqlite_session: Session) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    local_time = datetime(2026, 7, 18, 14, 0, tzinfo=timezone(timedelta(hours=2)))

    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-internal-utc",
        idempotency_key="recall-sms:job-internal-utc",
        available_at=local_time,
    )

    assert effect.available_at == local_time.astimezone(UTC)
    assert effect.available_at.utcoffset() == timedelta(0)


def test_idempotency_key_cannot_be_rebound(sqlite_session: Session) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-internal-001",
        idempotency_key="recall-sms:logical-request",
        available_at=NOW,
    )
    with pytest.raises(ValueError, match="different request"):
        enqueue_sms_effect(
            sqlite_session,
            clinic_id=clinic_id,
            outreach_job_id="job-internal-002",
            idempotency_key="recall-sms:logical-request",
            available_at=NOW,
        )


def test_committed_pending_effect_survives_process_loss(sqlite_session: Session) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-internal-001",
        idempotency_key="recall-sms:job-internal-001",
        available_at=NOW,
    )
    effect_id = effect.id
    sqlite_session.commit()
    with Session(sqlite_session.bind, expire_on_commit=False) as fresh_session:
        claimed = claim_effects(
            fresh_session,
            clinic_id=clinic_id,
            worker_id="worker-fresh",
            now=NOW,
            lease_for=timedelta(minutes=5),
        )
    assert [item.id for item in claimed] == [effect_id]


def test_active_lease_is_not_claimed_twice(sqlite_session: Session) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-internal-001",
        idempotency_key="recall-sms:job-internal-001",
        available_at=NOW,
    )
    sqlite_session.commit()
    first = claim_effects(
        sqlite_session,
        clinic_id=clinic_id,
        worker_id="worker-a",
        now=NOW,
        lease_for=timedelta(minutes=5),
    )
    sqlite_session.commit()
    second = claim_effects(
        sqlite_session,
        clinic_id=clinic_id,
        worker_id="worker-b",
        now=NOW + timedelta(minutes=1),
        lease_for=timedelta(minutes=5),
    )
    assert len(first) == 1
    assert second == []


def test_wrong_worker_cannot_mark_effect_dispatching(sqlite_session: Session) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-internal-001",
        idempotency_key="recall-sms:job-internal-001",
        available_at=NOW,
    )
    sqlite_session.commit()
    claim_effects(
        sqlite_session,
        clinic_id=clinic_id,
        worker_id="worker-a",
        now=NOW,
        lease_for=timedelta(minutes=5),
    )
    with pytest.raises(ValueError, match="another worker"):
        mark_dispatching(
            sqlite_session,
            clinic_id=clinic_id,
            effect_id=effect.id,
            worker_id="worker-b",
            now=NOW,
        )


def test_expired_lease_can_be_reclaimed(sqlite_session: Session) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-internal-001",
        idempotency_key="recall-sms:job-internal-001",
        available_at=NOW,
    )
    sqlite_session.commit()
    claim_effects(
        sqlite_session,
        clinic_id=clinic_id,
        worker_id="worker-a",
        now=NOW,
        lease_for=timedelta(minutes=5),
    )
    sqlite_session.commit()
    reclaimed = claim_effects(
        sqlite_session,
        clinic_id=clinic_id,
        worker_id="worker-b",
        now=NOW + timedelta(minutes=6),
        lease_for=timedelta(minutes=5),
    )
    assert [item.id for item in reclaimed] == [effect.id]
    assert reclaimed[0].lease_owner == "worker-b"


def test_expired_dispatching_effect_requires_reconciliation(sqlite_session: Session) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-internal-001",
        idempotency_key="recall-sms:job-internal-001",
        available_at=NOW,
    )
    sqlite_session.commit()
    claim_effects(
        sqlite_session,
        clinic_id=clinic_id,
        worker_id="worker-a",
        now=NOW,
        lease_for=timedelta(minutes=5),
    )
    mark_dispatching(
        sqlite_session,
        clinic_id=clinic_id,
        effect_id=effect.id,
        worker_id="worker-a",
        now=NOW,
    )
    sqlite_session.commit()
    claimed = claim_effects(
        sqlite_session,
        clinic_id=clinic_id,
        worker_id="worker-b",
        now=NOW + timedelta(minutes=6),
        lease_for=timedelta(minutes=5),
    )
    sqlite_session.commit()
    sqlite_session.refresh(effect)
    assert claimed == []
    assert effect.state == ExternalEffectState.RECONCILE_REQUIRED
    assert effect.last_error_code == "dispatch_lease_expired"


def test_run_once_is_off_by_default(sqlite_session: Session) -> None:
    clinic_id = _seed_sms_job(sqlite_session)
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-durable-sms",
        idempotency_key="recall-sms:job-durable-sms",
        available_at=NOW,
    )
    sqlite_session.commit()
    sender = FakeMessageSender()
    result = run_once(
        sessionmaker(bind=sqlite_session.bind, expire_on_commit=False),
        clinic_id=clinic_id,
        worker_id="worker-disabled",
        sender=sender,
        now=NOW,
    )
    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert result.enabled is False
    assert result.claimed == 0
    assert sender.sms_messages == []
    assert persisted is not None
    assert persisted.state == ExternalEffectState.PENDING


def test_durable_sms_environment_switch_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("CLINIC_RECALL_DURABLE_SMS_ENABLED", raising=False)
    assert durable_sms_enabled() is False
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_SMS_ENABLED", "true")
    assert durable_sms_enabled() is True
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_SMS_ENABLED", "unexpected")
    assert durable_sms_enabled() is False


def test_disabled_worker_cli_does_not_open_database(monkeypatch, capsys) -> None:
    monkeypatch.delenv("CLINIC_RECALL_DURABLE_SMS_ENABLED", raising=False)
    monkeypatch.setattr(
        "src.clinic_recall.durable.worker.get_sessionmaker",
        lambda: (_ for _ in ()).throw(AssertionError("database should remain unopened")),
    )
    assert worker_main(["--clinic-id", "clinic-internal-test"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "canceled": 0,
        "claimed": 0,
        "dead_lettered": 0,
        "enabled": False,
        "handoffs_queued": 0,
        "reconcile_required": 0,
        "rejected": 0,
        "retried": 0,
        "succeeded": 0,
    }


def test_worker_runtime_configuration_fails_closed_when_appconfig_load_fails(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AZURE_APPCONFIG_ENDPOINT", raising=False)
    _bootstrap_runtime_configuration(lambda: False)
    monkeypatch.setenv(
        "AZURE_APPCONFIG_ENDPOINT",
        "https://synthetic-test.azconfig.io",
    )
    with pytest.raises(RuntimeError, match="App Configuration"):
        _bootstrap_runtime_configuration(lambda: False)
    _bootstrap_runtime_configuration(lambda: True)


def test_run_once_rechecks_opt_out_before_provider_dispatch(sqlite_session: Session) -> None:
    clinic_id = _seed_sms_job(sqlite_session, opted_out=True)
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-durable-sms",
        idempotency_key="recall-sms:job-durable-sms",
        available_at=NOW,
    )
    sqlite_session.commit()
    sender = FakeMessageSender()
    result = run_once(
        sessionmaker(bind=sqlite_session.bind, expire_on_commit=False),
        clinic_id=clinic_id,
        worker_id="worker-opt-out",
        sender=sender,
        now=NOW,
        enabled=True,
    )
    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert result.canceled == 1
    assert sender.sms_messages == []
    assert persisted is not None
    assert persisted.state == ExternalEffectState.CANCELED
    assert persisted.last_error_code == "opted_out"


def test_run_once_cancels_when_programme_gate_returns_none(sqlite_session: Session) -> None:
    clinic_id = _seed_sms_job(sqlite_session)
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-durable-sms",
        idempotency_key="recall-sms:missing-gate-decision",
        available_at=NOW,
    )
    sqlite_session.commit()
    sender = FakeMessageSender()

    result = run_once(
        sessionmaker(bind=sqlite_session.bind, expire_on_commit=False),
        clinic_id=clinic_id,
        worker_id="worker-missing-gate-decision",
        sender=sender,
        programme_gate=lambda *_args: None,
        now=NOW,
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert result.canceled == 1
    assert sender.sms_messages == []
    assert persisted is not None
    assert persisted.state == ExternalEffectState.CANCELED
    assert persisted.last_error_code == "programme_gate_invalid"


def test_definitive_provider_rejection_is_terminal(sqlite_session: Session) -> None:
    clinic_id = _seed_sms_job(sqlite_session)
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-durable-sms",
        idempotency_key="recall-sms:job-durable-sms",
        available_at=NOW,
    )
    sqlite_session.commit()
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    sender = FakeMessageSender(sms_success=False)
    first = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-rejected",
        sender=sender,
        now=NOW,
        enabled=True,
    )
    second = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-after-rejection",
        sender=sender,
        now=NOW + timedelta(minutes=10),
        enabled=True,
    )
    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert first.rejected == 1
    assert second.claimed == 0
    assert len(sender.sms_messages) == 1
    assert persisted is not None
    assert persisted.state == ExternalEffectState.REJECTED
    assert persisted.last_error_code == "provider_rejected"


def test_retryable_rate_limit_persists_first_backoff_without_raw_error(
    sqlite_session: Session,
) -> None:
    clinic_id = _seed_sms_job(sqlite_session)
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-durable-sms",
        idempotency_key="recall-sms:job-durable-sms",
        available_at=NOW,
    )
    sqlite_session.commit()
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    sender = _RateLimitedSmsSender()

    first = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-rate-limited",
        sender=sender,
        now=NOW,
        enabled=True,
    )
    before_due = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-rate-limited-early",
        sender=sender,
        now=NOW + timedelta(seconds=59),
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    job = sqlite_session.get(OutreachJob, "job-durable-sms")
    assert first.retried == 1
    assert before_due.claimed == 0
    assert sender.calls == 1
    assert persisted is not None
    assert persisted.state == ExternalEffectState.PENDING
    assert persisted.attempt_count == 1
    assert persisted.available_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=1)
    assert persisted.last_error_class == "ProviderTransientFailure"
    assert persisted.last_error_code == "provider_rate_limited"
    assert job.state == "queued"
    assert sqlite_session.scalar(select(func.count()).select_from(Interaction)) == 0
    serialized = json.dumps(
        {
            "payload": persisted.payload,
            "last_error_class": persisted.last_error_class,
            "last_error_code": persisted.last_error_code,
        },
        sort_keys=True,
    )
    assert "Synthetic Patient" not in serialized
    assert "+447700900001" not in serialized


def test_transient_pre_dispatch_gate_retries_without_starting_voice_clock(
    sqlite_session: Session,
) -> None:
    clinic_id = _seed_sms_job(sqlite_session)
    patient = sqlite_session.get(Patient, "patient-durable-sms")
    patient.contact_prefs = {"quiet_start_hour": 8, "quiet_end_hour": 20}
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-durable-sms",
        idempotency_key="recall-sms:job-durable-sms",
        available_at=NOW,
    )
    sqlite_session.commit()
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    sender = FakeMessageSender()

    blocked = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-quiet-hours",
        sender=sender,
        now=NOW,
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert blocked.retried == 1
    assert blocked.canceled == 0
    assert sender.sms_messages == []
    assert persisted is not None
    assert persisted.state == ExternalEffectState.PENDING
    assert persisted.dispatch_started_at is None
    assert persisted.available_at.replace(tzinfo=UTC) == NOW + timedelta(hours=1)
    assert persisted.last_error_class == "PreDispatchTransient"
    assert persisted.last_error_code == SkipReason.QUIET_HOURS.value

    patient = sqlite_session.get(Patient, "patient-durable-sms")
    patient.contact_prefs = {}
    sqlite_session.commit()
    sent = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-after-quiet-hours",
        sender=sender,
        now=NOW + timedelta(hours=1),
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert sent.succeeded == 1
    assert len(sender.sms_messages) == 1
    assert persisted is not None
    assert persisted.dispatch_started_at.replace(tzinfo=UTC) == NOW + timedelta(hours=1)


def test_later_retry_backoff_grows_and_preserves_first_dispatch_start(
    sqlite_session: Session,
) -> None:
    clinic_id = _seed_sms_job(sqlite_session)
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-durable-sms",
        idempotency_key="recall-sms:job-durable-sms",
        available_at=NOW,
    )
    sqlite_session.commit()
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    sender = _RateLimitedSmsSender()

    first = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-retry-one",
        sender=sender,
        now=NOW,
        enabled=True,
    )
    second = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-retry-two",
        sender=sender,
        now=NOW + timedelta(minutes=1),
        enabled=True,
    )
    before_second_due = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-retry-two-early",
        sender=sender,
        now=NOW + timedelta(minutes=2, seconds=59),
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert first.retried == 1
    assert second.retried == 1
    assert before_second_due.claimed == 0
    assert sender.calls == 2
    assert persisted is not None
    assert persisted.attempt_count == 2
    assert persisted.available_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=3)
    assert persisted.dispatch_started_at.replace(tzinfo=UTC) == NOW


def test_retry_backoff_is_capped_at_one_hour(sqlite_session: Session) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-internal-001",
        idempotency_key="recall-sms:job-internal-001",
        available_at=NOW,
        max_attempts=10,
    )
    effect.state = ExternalEffectState.DISPATCHING
    effect.lease_owner = "worker-cap"
    effect.attempt_count = 8
    sqlite_session.flush()

    transitioned, handoff_created = mark_retryable_failure(
        sqlite_session,
        clinic_id=clinic_id,
        effect_id=effect.id,
        worker_id="worker-cap",
        now=NOW,
        reason_code="provider_rate_limited",
    )

    assert handoff_created is False
    assert transitioned.state == ExternalEffectState.PENDING
    assert transitioned.available_at == NOW + timedelta(hours=1)


def test_retry_exhaustion_dead_letters_once_with_minimized_handoff(
    sqlite_session: Session,
) -> None:
    from src.clinic_recall.models import ExternalEffectHandoff

    clinic_id = _seed_sms_job(sqlite_session)
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-durable-sms",
        idempotency_key="recall-sms:job-durable-sms",
        available_at=NOW,
        max_attempts=2,
    )
    sqlite_session.commit()
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    sender = _RateLimitedSmsSender()

    first = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-exhaust-one",
        sender=sender,
        now=NOW,
        enabled=True,
    )
    exhausted = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-exhaust-two",
        sender=sender,
        now=NOW + timedelta(minutes=1),
        enabled=True,
    )
    repeated = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-exhaust-repeated",
        sender=sender,
        now=NOW + timedelta(hours=2),
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    handoffs = sqlite_session.execute(select(ExternalEffectHandoff)).scalars().all()
    receipts = sqlite_session.execute(select(HandoffReceipt)).scalars().all()
    notification_effects = list(
        sqlite_session.scalars(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.HANDOFF_NOTIFICATION
            )
        )
    )
    assert first.retried == 1
    assert exhausted.dead_lettered == 1
    assert exhausted.handoffs_queued == 1
    assert repeated.claimed == 0
    assert sender.calls == 2
    assert persisted is not None
    assert persisted.state == ExternalEffectState.DEAD_LETTER
    assert persisted.attempt_count == 2
    assert len(handoffs) == 1
    assert handoffs[0].external_effect_id == effect.id
    assert handoffs[0].status == "queued"
    assert handoffs[0].reason_code == "retry_exhausted"
    assert len(receipts) == 1
    assert receipts[0].external_effect_handoff_id == handoffs[0].id
    assert len(notification_effects) == 1
    serialized = json.dumps(
        {
            "effect_id": handoffs[0].external_effect_id,
            "status": handoffs[0].status,
            "reason_code": handoffs[0].reason_code,
        },
        sort_keys=True,
    )
    assert "Synthetic Patient" not in serialized
    assert "+447700900001" not in serialized
    assert "raw provider detail" not in serialized


def test_run_once_dispatches_one_sms_and_persists_provider_evidence(
    sqlite_session: Session,
) -> None:
    clinic_id = _seed_sms_job(sqlite_session)
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-durable-sms",
        idempotency_key="recall-sms:job-durable-sms",
        available_at=NOW,
    )
    sqlite_session.commit()
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    sender = FakeMessageSender()
    result = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-success",
        sender=sender,
        now=NOW,
        enabled=True,
    )
    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert result.claimed == 1
    assert result.succeeded == 1
    assert len(sender.sms_messages) == 1
    assert persisted is not None
    assert persisted.state == ExternalEffectState.SUCCEEDED
    assert persisted.provider_resource_id == "fake-sms-1"
    assert persisted.provider_status == "accepted"
    assert len(persisted.completion_evidence_hash or "") == 64
    assert sqlite_session.scalar(select(func.count()).select_from(Interaction)) == 1
    second = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-second",
        sender=sender,
        now=NOW + timedelta(minutes=10),
        enabled=True,
    )
    assert second.claimed == 0
    assert len(sender.sms_messages) == 1


def test_verified_booking_confirmation_effect_dispatches_confirmation_once(
    sqlite_session: Session,
    monkeypatch,
) -> None:
    clinic_id, effect = _seed_verified_confirmation(sqlite_session)
    monkeypatch.setenv(
        "TWILIO_SMS_STATUS_CALLBACK_URL",
        "https://clinic.example.test/api/v1/sms/twilio",
    )
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    sender = FakeMessageSender()

    first = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-booking-confirmation",
        sender=sender,
        now=NOW,
        enabled=True,
        booking_confirmation_enabled=True,
    )
    second = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-booking-confirmation-repeat",
        sender=sender,
        now=NOW + timedelta(minutes=1),
        enabled=True,
        booking_confirmation_enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert first.succeeded == 1
    assert second.claimed == 0
    assert len(sender.sms_messages) == 1
    assert "appointment is booked" in sender.sms_messages[0].body.lower()
    assert sender.sms_messages[0].status_callback_url is not None
    assert effect.callback_token in sender.sms_messages[0].status_callback_url
    assert persisted is not None
    assert persisted.state == ExternalEffectState.SUCCEEDED
    assert persisted.provider_resource_id == "fake-sms-1"


def test_booking_confirmation_dispatch_switch_off_cancels_without_send(
    sqlite_session: Session,
) -> None:
    clinic_id, effect = _seed_verified_confirmation(sqlite_session)
    sender = FakeMessageSender()

    result = run_once(
        sessionmaker(bind=sqlite_session.bind, expire_on_commit=False),
        clinic_id=clinic_id,
        worker_id="worker-booking-confirmation-disabled",
        sender=sender,
        now=NOW,
        enabled=True,
        booking_confirmation_enabled=False,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert result.canceled == 1
    assert sender.sms_messages == []
    assert persisted is not None
    assert persisted.state == ExternalEffectState.CANCELED
    assert persisted.last_error_code == "booking_confirmation_disabled"


def test_malformed_booking_confirmation_intent_never_falls_back_to_recall(
    sqlite_session: Session,
) -> None:
    clinic_id, effect = _seed_verified_confirmation(sqlite_session)
    effect.payload = {
        "intent": "booking_confirmation",
        "outreach_job_id": effect.aggregate_id,
    }
    sqlite_session.commit()
    sender = FakeMessageSender()

    result = run_once(
        sessionmaker(bind=sqlite_session.bind, expire_on_commit=False),
        clinic_id=clinic_id,
        worker_id="worker-booking-confirmation-malformed",
        sender=sender,
        now=NOW,
        enabled=True,
        booking_confirmation_enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert result.canceled == 1
    assert sender.sms_messages == []
    assert persisted is not None
    assert persisted.state == ExternalEffectState.CANCELED
    assert persisted.last_error_code == "invalid_sms_effect_contract"


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("booking_action_id", "booking-action-untrusted"),
        ("idempotency_key", "booking-confirmation:changed:v1"),
        ("request_hash", "0" * 64),
    ],
)
def test_booking_confirmation_effect_must_match_exact_verified_action_and_evidence(
    sqlite_session: Session,
    field_name: str,
    field_value: str,
) -> None:
    clinic_id, effect = _seed_verified_confirmation(sqlite_session)
    if field_name == "booking_action_id":
        effect.payload = {
            **effect.payload,
            "booking_action_id": field_value,
        }
    else:
        setattr(effect, field_name, field_value)
    sqlite_session.commit()
    sender = FakeMessageSender()

    result = run_once(
        sessionmaker(bind=sqlite_session.bind, expire_on_commit=False),
        clinic_id=clinic_id,
        worker_id="worker-booking-confirmation-binding",
        sender=sender,
        now=NOW,
        enabled=True,
        booking_confirmation_enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert result.canceled == 1
    assert sender.sms_messages == []
    assert persisted is not None
    assert persisted.state == ExternalEffectState.CANCELED
    assert persisted.last_error_code == "booking_confirmation_authority_invalid"


def test_booking_confirmation_rejects_tampered_completion_evidence(
    sqlite_session: Session,
) -> None:
    clinic_id, _effect = _seed_verified_confirmation(sqlite_session)
    booking_effect = sqlite_session.execute(
        select(ExternalEffect).where(
            ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
        )
    ).scalar_one()
    booking_effect.completion_evidence_hash = "0" * 64
    sqlite_session.commit()
    sender = FakeMessageSender()

    result = run_once(
        sessionmaker(bind=sqlite_session.bind, expire_on_commit=False),
        clinic_id=clinic_id,
        worker_id="worker-tampered-booking-evidence",
        sender=sender,
        now=NOW,
        enabled=True,
        booking_confirmation_enabled=True,
    )

    sqlite_session.expire_all()
    confirmation_effect = sqlite_session.get(ExternalEffect, _effect.id)
    assert result.canceled == 1
    assert sender.sms_messages == []
    assert confirmation_effect is not None
    assert confirmation_effect.last_error_code == (
        "booking_confirmation_authority_invalid"
    )


def test_sms_worker_claims_only_sms_when_future_effect_types_are_due(
    sqlite_session: Session,
) -> None:
    clinic_id = _seed_sms_job(sqlite_session)
    sms_effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-durable-sms",
        idempotency_key="recall-sms:job-durable-sms",
        available_at=NOW,
    )
    call_effect = ExternalEffect(
        id="effect-durable-call",
        clinic_id=clinic_id,
        aggregate_type="outreach_job",
        aggregate_id="job-durable-sms",
        effect_type=ExternalEffectType.CALL,
        idempotency_key="recall-call:job-durable-sms",
        callback_token=generate_effect_token(clinic_id),
        payload_version=1,
        payload={"intent": "recall_fallback", "outreach_job_id": "job-durable-sms"},
        request_hash="c" * 64,
        state=ExternalEffectState.PENDING,
        available_at=NOW,
        max_attempts=1,
    )
    sqlite_session.add(call_effect)
    recording_effect = ExternalEffect(
        id="effect-durable-recording",
        clinic_id=clinic_id,
        aggregate_type="outreach_job",
        aggregate_id="job-durable-sms",
        effect_type=ExternalEffectType.RECORDING,
        idempotency_key="recall-recording:job-durable-sms",
        callback_token=generate_effect_token(clinic_id),
        payload_version=1,
        payload={"intent": "recording", "outreach_job_id": "job-durable-sms"},
        request_hash="r" * 64,
        state=ExternalEffectState.PENDING,
        available_at=NOW,
        max_attempts=1,
    )
    sqlite_session.add(recording_effect)
    sqlite_session.commit()
    sender = FakeMessageSender()

    result = run_once(
        sessionmaker(bind=sqlite_session.bind, expire_on_commit=False),
        clinic_id=clinic_id,
        worker_id="worker-sms-only",
        sender=sender,
        now=NOW,
        enabled=True,
    )

    sqlite_session.expire_all()
    assert result.claimed == 1
    assert result.succeeded == 1
    assert len(sender.sms_messages) == 1
    assert sqlite_session.get(ExternalEffect, sms_effect.id).state == ExternalEffectState.SUCCEEDED
    persisted_call = sqlite_session.get(ExternalEffect, call_effect.id)
    assert persisted_call is not None
    assert persisted_call.state == ExternalEffectState.PENDING
    assert persisted_call.attempt_count == 0
    persisted_recording = sqlite_session.get(ExternalEffect, recording_effect.id)
    assert persisted_recording is not None
    assert persisted_recording.state == ExternalEffectState.PENDING
    assert persisted_recording.attempt_count == 0


def test_run_once_passes_persisted_effect_token_in_callback_url(
    sqlite_session: Session,
    monkeypatch,
) -> None:
    from urllib.parse import parse_qs, urlsplit

    clinic_id = _seed_sms_job(sqlite_session)
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-durable-sms",
        idempotency_key="recall-sms:job-durable-sms",
        available_at=NOW,
    )
    sqlite_session.commit()
    monkeypatch.setenv(
        "TWILIO_SMS_STATUS_CALLBACK_URL",
        "https://clinic.example.test/api/v1/sms/twilio",
    )
    sender = FakeMessageSender()

    result = run_once(
        sessionmaker(bind=sqlite_session.bind, expire_on_commit=False),
        clinic_id=clinic_id,
        worker_id="worker-callback-url",
        sender=sender,
        now=NOW,
        enabled=True,
    )

    assert result.succeeded == 1
    assert len(sender.sms_messages) == 1
    callback_url = sender.sms_messages[0].status_callback_url
    assert callback_url is not None
    assert parse_qs(urlsplit(callback_url).query) == {"effect_token": [effect.callback_token]}


def test_ambiguous_provider_exception_is_minimized_and_never_retried(
    sqlite_session: Session,
) -> None:
    clinic_id = _seed_sms_job(sqlite_session)
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-durable-sms",
        idempotency_key="recall-sms:job-durable-sms",
        available_at=NOW,
    )
    sqlite_session.commit()
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    sender = _AmbiguousSmsSender()
    first = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-timeout",
        sender=sender,
        now=NOW,
        enabled=True,
    )
    second = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-after-timeout",
        sender=sender,
        now=NOW + timedelta(minutes=10),
        enabled=True,
    )
    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert first.reconcile_required == 1
    assert second.claimed == 0
    assert sender.calls == 1
    assert persisted is not None
    assert persisted.state == ExternalEffectState.RECONCILE_REQUIRED
    assert persisted.last_error_class == "ProviderDispatchError"
    assert persisted.last_error_code == "provider_outcome_unknown"
    serialized = json.dumps(
        {
            "payload": persisted.payload,
            "last_error_class": persisted.last_error_class,
            "last_error_code": persisted.last_error_code,
        },
        sort_keys=True,
    )
    assert set(persisted.payload) == {"intent", "outreach_job_id"}
    assert "Synthetic Patient" not in serialized
    assert "+447700900001" not in serialized
    assert "Reply STOP" not in serialized


def test_acs_sender_propagates_unknown_provider_outcome() -> None:
    sender = AcsSmsSender(service=_OutcomeUnknownService())

    with pytest.raises(ProviderOutcomeUnknownError, match="outcome is unknown"):
        sender.send_sms(
            to="+447700900001",
            body="Synthetic Clinic Recall acceptance message",
            tag="synthetic-job",
        )
