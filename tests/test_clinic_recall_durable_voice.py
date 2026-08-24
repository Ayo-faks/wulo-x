"""Focused contracts for durable Clinic Recall CALL dispatch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from src.clinic_recall.durable import call_worker as call_worker_module
from src.clinic_recall.durable import config as durable_config
from src.clinic_recall.durable.call_worker import run_once as run_call_once
from src.clinic_recall.durable.callbacks import (
    generate_effect_token,
    receive_twilio_callback,
    reconcile_once,
)
from src.clinic_recall.durable.effects import claim_effects, mark_dispatching
from src.clinic_recall.durable.enqueue import enqueue_call_effect, enqueue_sms_effect
from src.clinic_recall.enums import (
    BookingActionStatus,
    BookingActionType,
    CallRecordingStatus,
    CampaignStatus,
    CampaignType,
    Channel,
    EscalationPriority,
    EscalationReason,
    EscalationStatus,
    ExternalEffectState,
    ExternalEffectType,
    InteractionDirection,
    OutreachState,
    ProviderCallbackKind,
    ProviderCallbackState,
    RecordingConsentState,
)
from src.clinic_recall.models import (
    Appointment,
    Base,
    BookingAction,
    CallRecord,
    Campaign,
    Clinic,
    Escalation,
    ExternalEffect,
    Interaction,
    OutreachJob,
    Patient,
    ProviderCallbackReceipt,
)
from src.clinic_recall.voice_worker import (
    CallInitiationDisposition,
    CallInitiationReason,
    CallInitiationResult,
    TwilioCallInitiator,
)

NOW = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
CALL_SID = "CA" + "4" * 32


class _ObservingCallInitiator:
    name = "synthetic-observer"

    def __init__(self, session_factory: sessionmaker[Session], effect_id: str) -> None:
        self._session_factory = session_factory
        self._effect_id = effect_id
        self.calls = 0

    def initiate_call(
        self,
        *,
        target_number: str,
        context: dict[str, object],
    ) -> CallInitiationResult:
        self.calls += 1
        with self._session_factory() as fresh_session:
            persisted = fresh_session.get(ExternalEffect, self._effect_id)
            ledger = fresh_session.execute(
                select(CallRecord).where(CallRecord.external_effect_id == self._effect_id)
            ).scalar_one()
            assert persisted is not None
            assert persisted.state == ExternalEffectState.DISPATCHING
            assert persisted.attempt_count == 1
            assert ledger.provider_call_id is None
            assert ledger.consent_state == RecordingConsentState.NOT_ASKED
            assert ledger.recording_status == CallRecordingStatus.NONE
        assert target_number == "+447700900101"
        assert context["effect_token"]
        assert context["record_call"] is False
        return CallInitiationResult(
            successful=True,
            call_id=CALL_SID,
            provider=self.name,
        )


class _BatchObservingCallInitiator:
    name = "synthetic-batch-observer"

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        effect_ids: set[str],
    ) -> None:
        self._session_factory = session_factory
        self._effect_ids = effect_ids
        self.calls = 0

    def initiate_call(
        self,
        *,
        target_number: str,
        context: dict[str, object],
    ) -> CallInitiationResult:
        del target_number, context
        if self.calls == 0:
            with self._session_factory() as fresh_session:
                effects = list(
                    fresh_session.scalars(
                        select(ExternalEffect).where(ExternalEffect.id.in_(self._effect_ids))
                    )
                )
                assert {effect.id for effect in effects} == self._effect_ids
                assert all(
                    effect.state == ExternalEffectState.DISPATCHING and effect.attempt_count == 1
                    for effect in effects
                )
        self.calls += 1
        return CallInitiationResult(
            successful=True,
            call_id="CA" + str(self.calls) * 32,
            provider=self.name,
        )


def _allow_programme(_session, _clinic_id, _job, _now) -> bool:
    return True


class _NeverCallInitiator:
    name = "never-call"

    def __init__(self) -> None:
        self.calls = 0

    def initiate_call(self, **_kwargs) -> CallInitiationResult:
        self.calls += 1
        raise AssertionError("blocked or disabled CALL work must not reach a provider")


class _StaticResultInitiator:
    name = "synthetic-static-result"

    def __init__(self, result: CallInitiationResult) -> None:
        self.result = result
        self.calls = 0

    def initiate_call(self, **_kwargs) -> CallInitiationResult:
        self.calls += 1
        return self.result


class _RaisingCallInitiator:
    name = "synthetic-raising-result"

    def __init__(self, exception: Exception) -> None:
        self.exception = exception
        self.calls = 0

    def initiate_call(self, **_kwargs) -> CallInitiationResult:
        self.calls += 1
        raise self.exception


def _seed_blocked_candidate(
    session: Session,
    *,
    case: str,
) -> tuple[str, ExternalEffect]:
    clinic_id = "clinic-durable-call-blocked"
    patient_id = "patient-durable-call-blocked"
    job_id = "job-durable-call-blocked"
    campaign_status = (
        CampaignStatus.PAUSED if case == "inactive_campaign" else CampaignStatus.ACTIVE
    )
    job_state = {
        "completed_thread": OutreachState.COMPLETED,
        "escalation": OutreachState.ESCALATED,
    }.get(case, OutreachState.NO_REPLY)
    phone = (
        None
        if case == "missing_phone"
        else "invalid-phone" if case == "invalid_phone" else "+447700900102"
    )
    consent_flags = (
        {}
        if case == "missing_consent"
        else (
            {"call": False, "sms": True} if case == "false_consent" else {"call": True, "sms": True}
        )
    )
    opt_out_flags = {"call": case == "permanent_opt_out"}
    contact_prefs = (
        {"quiet_start_hour": 10, "quiet_end_hour": 12} if case == "quiet_hours" else None
    )
    contact_hours = {"start_hour": 12, "end_hour": 13} if case == "outside_contact_hours" else None
    daily_caps = 1 if case == "daily_cap" else 20

    session.add(
        Clinic(
            id=clinic_id,
            name="Blocked Durable Call Clinic",
            timezone="Europe/London",
            contact_hours=contact_hours,
            daily_caps=daily_caps,
        )
    )
    session.add(
        Patient(
            id=patient_id,
            clinic_id=clinic_id,
            source_ref=patient_id,
            name="Synthetic Blocked Call Patient",
            phone=phone,
            consent_flags=consent_flags,
            opt_out_flags=opt_out_flags,
            contact_prefs=contact_prefs,
        )
    )
    session.add(
        Appointment(
            id="appointment-durable-call-blocked",
            clinic_id=clinic_id,
            patient_id=patient_id,
            source_ref="appointment-durable-call-blocked",
            status="missed",
            start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
        )
    )
    session.add(
        Campaign(
            id="campaign-durable-call-blocked",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=campaign_status,
        )
    )
    session.add(
        OutreachJob(
            id=job_id,
            clinic_id=clinic_id,
            campaign_id="campaign-durable-call-blocked",
            patient_id=patient_id,
            appointment_id="appointment-durable-call-blocked",
            channel=Channel.SMS,
            state=job_state,
        )
    )
    session.flush()

    if case == "related_completed_thread":
        session.add(
            OutreachJob(
                id="job-durable-call-related-complete",
                clinic_id=clinic_id,
                campaign_id="campaign-durable-call-blocked",
                patient_id=patient_id,
                appointment_id="appointment-durable-call-blocked",
                channel=Channel.SMS,
                state=OutreachState.COMPLETED,
            )
        )
    if case in {"inbound_reply", "existing_outbound_call"}:
        session.add(
            Interaction(
                id=f"interaction-{case}",
                clinic_id=clinic_id,
                outreach_job_id=job_id,
                channel=(Channel.CALL if case == "existing_outbound_call" else Channel.SMS),
                direction=(
                    InteractionDirection.OUTBOUND
                    if case == "existing_outbound_call"
                    else InteractionDirection.INBOUND
                ),
                occurred_at=NOW,
            )
        )
    if case == "completed_booking_row":
        session.add(
            BookingAction(
                id="booking-action-durable-call-completed",
                clinic_id=clinic_id,
                appointment_id="appointment-durable-call-blocked",
                outreach_job_id=job_id,
                type=BookingActionType.BOOK,
                status=BookingActionStatus.COMPLETED,
                written_back=False,
            )
        )
    if case == "active_escalation_row":
        session.add(
            Escalation(
                id="escalation-durable-call-active",
                clinic_id=clinic_id,
                patient_id=patient_id,
                outreach_job_id=job_id,
                reason=EscalationReason.AMBIGUOUS,
                priority=EscalationPriority.NORMAL,
                status=EscalationStatus.OPEN,
            )
        )
    if case in {"frequency_cap", "daily_cap"}:
        contact_count = 3 if case == "frequency_cap" else 1
        session.add_all(
            [
                Interaction(
                    id=f"interaction-{case}-{index}",
                    clinic_id=clinic_id,
                    outreach_job_id=job_id,
                    channel=Channel.SMS,
                    direction=InteractionDirection.OUTBOUND,
                    occurred_at=NOW,
                )
                for index in range(contact_count)
            ]
        )
    session.flush()
    effect, _ = enqueue_call_effect(
        session,
        clinic_id=clinic_id,
        outreach_job_id=job_id,
        idempotency_key=f"cadence:call:{job_id}",
        available_at=NOW,
    )
    session.commit()
    return clinic_id, effect


def test_disabled_call_worker_opens_no_database_or_provider() -> None:
    def fail_session_factory() -> Session:
        raise AssertionError("disabled worker must not open the database")

    initiator = _NeverCallInitiator()
    result = run_call_once(
        fail_session_factory,
        clinic_id="clinic-disabled-call",
        worker_id="worker-disabled-call",
        initiator=initiator,
        programme_gate=None,
        now=NOW,
        enabled=False,
    )

    assert result.as_summary() == {
        "enabled": False,
        "claimed": 0,
        "provider_accepted": 0,
        "rejected": 0,
        "canceled": 0,
        "reconcile_required": 0,
    }
    assert initiator.calls == 0


@pytest.mark.parametrize(
    ("case", "reason_code"),
    [
        ("inactive_campaign", "campaign_not_active"),
        ("inbound_reply", "inbound_reply"),
        ("existing_outbound_call", "outbound_call_exists"),
        ("completed_thread", "outreach_completed"),
        ("related_completed_thread", "outreach_thread_stopped"),
        ("escalation", "outreach_escalated"),
        ("completed_booking_row", "booking_completed"),
        ("active_escalation_row", "active_escalation"),
        ("permanent_opt_out", "opted_out"),
        ("missing_consent", "no_consent"),
        ("false_consent", "no_consent"),
        ("invalid_phone", "not_contactable"),
        ("missing_phone", "not_contactable"),
        ("frequency_cap", "frequency_cap"),
        ("daily_cap", "daily_cap"),
        ("quiet_hours", "quiet_hours"),
        ("outside_contact_hours", "outside_contact_hours"),
        ("programme_gate_false", "programme_gate_unbound"),
    ],
)
def test_fresh_dispatch_gate_cancels_without_provider_and_is_idempotent(
    sqlite_session: Session,
    case: str,
    reason_code: str,
) -> None:
    clinic_id, effect = _seed_blocked_candidate(sqlite_session, case=case)
    session_factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    initiator = _NeverCallInitiator()
    programme_gate = (
        (lambda _session, _clinic_id, _job, _now: False)
        if case == "programme_gate_false"
        else _allow_programme
    )

    first = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id=f"worker-{case}",
        initiator=initiator,
        programme_gate=programme_gate,
        now=NOW,
        enabled=True,
    )
    second = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id=f"worker-{case}-second",
        initiator=initiator,
        programme_gate=programme_gate,
        now=NOW,
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert first.claimed == 1
    assert first.canceled == 1
    assert first.provider_accepted == 0
    assert first.rejected == 0
    assert first.reconcile_required == 0
    assert second.claimed == 0
    assert second.canceled == 0
    assert initiator.calls == 0
    assert persisted is not None
    assert persisted.state == ExternalEffectState.CANCELED
    assert persisted.provider_status == "not_dispatched"
    assert persisted.last_error_code == reason_code
    assert sqlite_session.scalar(select(func.count()).select_from(Interaction)) == (
        3
        if case == "frequency_cap"
        else (
            1
            if case
            in {
                "daily_cap",
                "inbound_reply",
                "existing_outbound_call",
            }
            else 0
        )
    )


@pytest.mark.parametrize(
    "tamper",
    ["aggregate_type", "aggregate_id", "payload", "payload_version", "max_attempts"],
)
def test_tampered_call_effect_contract_cancels_without_provider_io(
    sqlite_session: Session,
    tamper: str,
) -> None:
    clinic_id, effect = _seed_blocked_candidate(sqlite_session, case="ready")
    if tamper == "aggregate_type":
        effect.aggregate_type = "patient"
    elif tamper == "aggregate_id":
        effect.aggregate_id = "job-unbound"
    elif tamper == "payload":
        effect.payload = {
            "intent": "recall_fallback",
            "outreach_job_id": "job-unbound",
        }
    elif tamper == "payload_version":
        effect.payload_version = 2
    else:
        effect.max_attempts = 2
    sqlite_session.commit()
    session_factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    initiator = _NeverCallInitiator()

    result = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id=f"worker-tampered-{tamper}",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW,
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert result.claimed == 1
    assert result.canceled == 1
    assert result.reconcile_required == 0
    assert initiator.calls == 0
    assert persisted is not None
    assert persisted.state == ExternalEffectState.CANCELED
    assert persisted.last_error_code == "invalid_effect_contract"
    assert sqlite_session.scalar(select(func.count()).select_from(Interaction)) == 0


def test_unbound_programme_gate_does_not_open_database_or_construct_provider() -> None:
    def fail_session_factory() -> Session:
        raise AssertionError("unbound programme seam must not open the database")

    initiator = _NeverCallInitiator()
    result = run_call_once(
        fail_session_factory,
        clinic_id="clinic-unbound-programme",
        worker_id="worker-unbound-programme",
        initiator=initiator,
        programme_gate=None,
        now=NOW,
        enabled=True,
    )

    assert result == call_worker_module.CallRunOnceResult(enabled=False)
    assert initiator.calls == 0


def test_unrestricted_provider_failure_is_ambiguous_and_never_replayed(
    sqlite_session: Session,
) -> None:
    clinic_id, effect = _seed_blocked_candidate(sqlite_session, case="ready")
    session_factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    raw_detail = "Synthetic Patient +447700900102 raw provider rejection body"
    initiator = _StaticResultInitiator(
        CallInitiationResult(
            successful=False,
            provider="legacy-unrestricted",
            error=raw_detail,
        )
    )

    first = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id="worker-unrestricted-failure",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW,
        enabled=True,
    )
    second = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id="worker-unrestricted-failure-second",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW,
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert first.claimed == 1
    assert first.provider_accepted == 0
    assert first.rejected == 0
    assert first.reconcile_required == 1
    assert second.claimed == 0
    assert initiator.calls == 1
    assert persisted is not None
    assert persisted.state == ExternalEffectState.RECONCILE_REQUIRED
    assert persisted.last_error_code == "provider_outcome_unknown"
    assert raw_detail not in repr(persisted.__dict__)
    assert raw_detail not in str(first.as_summary())


def test_explicit_closed_provider_rejection_is_terminal_and_never_retried(
    sqlite_session: Session,
) -> None:
    clinic_id, effect = _seed_blocked_candidate(sqlite_session, case="ready")
    session_factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    initiator = _StaticResultInitiator(
        CallInitiationResult(
            successful=False,
            provider="synthetic-closed-rejection",
            disposition=CallInitiationDisposition.REJECTED,
            reason_code=CallInitiationReason.PROVIDER_VALIDATION_REJECTED,
        )
    )

    first = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id="worker-closed-rejection",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW,
        enabled=True,
    )
    second = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id="worker-closed-rejection-second",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW,
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert first.claimed == 1
    assert first.provider_accepted == 0
    assert first.rejected == 1
    assert first.reconcile_required == 0
    assert second.claimed == 0
    assert initiator.calls == 1
    assert persisted is not None
    assert persisted.state == ExternalEffectState.REJECTED
    assert persisted.provider_status == "rejected"
    assert persisted.last_error_code == "provider_validation_rejected"


def test_closed_not_dispatched_result_is_canceled_not_provider_rejected(
    sqlite_session: Session,
) -> None:
    clinic_id, effect = _seed_blocked_candidate(sqlite_session, case="ready")
    session_factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    initiator = _StaticResultInitiator(
        CallInitiationResult(
            successful=False,
            provider="synthetic-local-preflight",
            disposition=CallInitiationDisposition.NOT_DISPATCHED,
            reason_code=CallInitiationReason.INVALID_CONFIGURATION,
        )
    )

    result = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id="worker-local-preflight",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW,
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert result.claimed == 1
    assert result.canceled == 1
    assert result.rejected == 0
    assert result.reconcile_required == 0
    assert initiator.calls == 1
    assert persisted is not None
    assert persisted.state == ExternalEffectState.CANCELED
    assert persisted.provider_status == "not_dispatched"
    assert persisted.last_error_code == "invalid_configuration"


@pytest.mark.parametrize(
    ("result", "reason_code"),
    [
        (
            CallInitiationResult(successful=True, provider="synthetic", call_id=None),
            "missing_call_sid",
        ),
        (
            CallInitiationResult(
                successful=True,
                provider="synthetic",
                call_id="not-a-call-sid",
            ),
            "missing_call_sid",
        ),
        (
            CallInitiationResult(
                successful=False,
                provider="synthetic",
                disposition=CallInitiationDisposition.AMBIGUOUS,
                reason_code=CallInitiationReason.TRANSPORT_ERROR,
            ),
            "transport_error",
        ),
        (
            CallInitiationResult(
                successful=False,
                provider="synthetic",
                disposition=CallInitiationDisposition.AMBIGUOUS,
                reason_code=CallInitiationReason.MALFORMED_RESPONSE,
            ),
            "malformed_response",
        ),
    ],
)
def test_ambiguous_call_results_are_quarantined_without_replay(
    sqlite_session: Session,
    result: CallInitiationResult,
    reason_code: str,
) -> None:
    clinic_id, effect = _seed_blocked_candidate(sqlite_session, case="ready")
    session_factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    initiator = _StaticResultInitiator(result)

    first = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id=f"worker-ambiguous-{reason_code}",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW,
        enabled=True,
    )
    second = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id=f"worker-ambiguous-{reason_code}-second",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW,
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert first.claimed == 1
    assert first.reconcile_required == 1
    assert first.provider_accepted == 0
    assert first.rejected == 0
    assert second.claimed == 0
    assert initiator.calls == 1
    assert persisted is not None
    assert persisted.state == ExternalEffectState.RECONCILE_REQUIRED
    assert persisted.last_error_code == reason_code


@pytest.mark.parametrize(
    "exception",
    [
        httpx.ReadTimeout("synthetic timeout"),
        httpx.ConnectError("synthetic connection loss"),
        RuntimeError("synthetic unexpected provider failure"),
    ],
)
def test_exception_after_dispatching_is_ambiguous_and_never_replayed(
    sqlite_session: Session,
    exception: Exception,
) -> None:
    clinic_id, effect = _seed_blocked_candidate(sqlite_session, case="ready")
    session_factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    initiator = _RaisingCallInitiator(exception)

    first = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id="worker-provider-exception",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW,
        enabled=True,
    )
    second = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id="worker-provider-exception-second",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW,
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert first.reconcile_required == 1
    assert first.rejected == 0
    assert second.claimed == 0
    assert initiator.calls == 1
    assert persisted is not None
    assert persisted.state == ExternalEffectState.RECONCILE_REQUIRED
    assert persisted.last_error_code == "provider_outcome_unknown"
    assert str(exception) not in repr(persisted.__dict__)


def test_expired_dispatching_call_is_quarantined_without_provider_replay(
    sqlite_session: Session,
) -> None:
    clinic_id, effect = _seed_blocked_candidate(sqlite_session, case="ready")
    claimed = claim_effects(
        sqlite_session,
        clinic_id=clinic_id,
        worker_id="worker-expired-dispatch",
        now=NOW,
        lease_for=timedelta(minutes=1),
        effect_types=(ExternalEffectType.CALL,),
    )
    assert [item.id for item in claimed] == [effect.id]
    mark_dispatching(
        sqlite_session,
        clinic_id=clinic_id,
        effect_id=effect.id,
        worker_id="worker-expired-dispatch",
        now=NOW,
    )
    sqlite_session.commit()
    session_factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    initiator = _NeverCallInitiator()

    result = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id="worker-after-expiry",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW + timedelta(minutes=2),
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert result.claimed == 0
    assert initiator.calls == 0
    assert persisted is not None
    assert persisted.state == ExternalEffectState.RECONCILE_REQUIRED
    assert persisted.last_error_code == "dispatch_lease_expired"


@pytest.mark.parametrize(
    ("status_code", "response_content", "disposition", "reason_code"),
    [
        (
            201,
            b'{"sid":"CA44444444444444444444444444444444"}',
            CallInitiationDisposition.ACCEPTED,
            CallInitiationReason.PROVIDER_ACCEPTED,
        ),
        (
            201,
            b"{}",
            CallInitiationDisposition.AMBIGUOUS,
            CallInitiationReason.MISSING_CALL_SID,
        ),
        (
            201,
            b"not-json",
            CallInitiationDisposition.AMBIGUOUS,
            CallInitiationReason.MALFORMED_RESPONSE,
        ),
        (
            400,
            b'{"code":21211,"message":"raw target-number rejection"}',
            CallInitiationDisposition.REJECTED,
            CallInitiationReason.PROVIDER_VALIDATION_REJECTED,
        ),
        (
            401,
            b'{"message":"raw credential rejection"}',
            CallInitiationDisposition.REJECTED,
            CallInitiationReason.PROVIDER_AUTH_REJECTED,
        ),
        (
            500,
            b'{"message":"raw provider server response"}',
            CallInitiationDisposition.AMBIGUOUS,
            CallInitiationReason.PROVIDER_SERVER_ERROR,
        ),
    ],
)
def test_twilio_adapter_returns_only_closed_create_outcomes(
    status_code: int,
    response_content: bytes,
    disposition: CallInitiationDisposition,
    reason_code: CallInitiationReason,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            content=response_content,
            headers={"Content-Type": "application/json"},
        )

    initiator = TwilioCallInitiator(
        account_sid="AC123",
        auth_token="synthetic-secret",
        from_number="+447700900200",
        twiml_url="https://clinic.example.test/api/v1/voice/twilio/twiml",
        transport=httpx.MockTransport(handler),
    )
    result = initiator.initiate_call(
        target_number="+447700900201",
        context={"scenario": "rebooking", "record_call": False},
    )

    assert result.disposition == disposition
    assert result.reason_code == reason_code
    assert result.successful is (disposition == CallInitiationDisposition.ACCEPTED)
    assert "raw" not in (result.error or "")


@pytest.mark.parametrize("exception_type", [httpx.ReadTimeout, httpx.ConnectError])
def test_twilio_adapter_classifies_transport_loss_as_ambiguous(
    exception_type: type[httpx.HTTPError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception_type("raw transport detail", request=request)

    initiator = TwilioCallInitiator(
        account_sid="AC123",
        auth_token="synthetic-secret",
        from_number="+447700900200",
        twiml_url="https://clinic.example.test/api/v1/voice/twilio/twiml",
        transport=httpx.MockTransport(handler),
    )
    result = initiator.initiate_call(
        target_number="+447700900201",
        context={"scenario": "rebooking", "record_call": False},
    )

    assert result.successful is False
    assert result.disposition == CallInitiationDisposition.AMBIGUOUS
    assert result.reason_code == CallInitiationReason.TRANSPORT_ERROR
    assert "raw" not in (result.error or "")


def test_durable_twilio_create_uses_url_twiML_and_synchronous_amd_only() -> None:
    effect_token = generate_effect_token("clinic-durable-twilio-form")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/2010-04-01/Accounts/AC123/Calls.json"
        assert request.headers.get("Authorization", "").startswith("Basic ")
        body = parse_qs(request.content.decode("utf-8"))
        assert body["From"] == ["+447700900200"]
        assert body["To"] == ["+447700900201"]
        assert body["MachineDetection"] == ["Enable"]
        assert "Twiml" not in body
        assert "AsyncAmd" not in body
        assert "AsyncAmdStatusCallback" not in body
        assert "AsyncAmdStatusCallbackMethod" not in body
        assert "Record" not in body
        assert "RecordingChannels" not in body
        assert "RecordingStatusCallback" not in body
        assert "RecordingStatusCallbackEvent" not in body
        assert "voicemail" not in request.content.decode("utf-8").lower()
        twiml_query = parse_qs(urlsplit(body["Url"][0]).query)
        status_query = parse_qs(urlsplit(body["StatusCallback"][0]).query)
        assert set(twiml_query) == {"source", "effect_token"}
        assert twiml_query["effect_token"] == [effect_token]
        assert status_query["effect_token"] == [effect_token]
        assert twiml_query["source"] == ["clinic_recall_voice_worker"]
        for minimized_key in (
            "clinic_id",
            "patient_id",
            "outreach_job_id",
            "record_call",
            "scenario",
            "session_id",
        ):
            assert minimized_key not in twiml_query
        assert body["StatusCallbackEvent"] == [
            "initiated",
            "ringing",
            "answered",
            "completed",
        ]
        return httpx.Response(201, json={"sid": CALL_SID, "status": "queued"})

    initiator = TwilioCallInitiator(
        account_sid="AC123",
        auth_token="synthetic-secret",
        from_number="+447700900200",
        twiml_url="https://clinic.example.test/api/v1/voice/twilio/twiml",
        status_callback_url="https://clinic.example.test/api/v1/voice/twilio/call-status",
        transport=httpx.MockTransport(handler),
    )
    result = initiator.initiate_call(
        target_number="+447700900201",
        context={
            "source": "clinic_recall_voice_worker",
            "scenario": "rebooking",
            "clinic_id": "clinic-durable-twilio-form",
            "patient_id": "patient-durable-twilio-form",
            "outreach_job_id": "job-durable-twilio-form",
            "record_call": False,
            "effect_token": effect_token,
        },
    )

    assert result.successful is True
    assert result.disposition == CallInitiationDisposition.ACCEPTED
    assert result.call_id == CALL_SID


@pytest.mark.parametrize(
    "context_override",
    [
        {"record_call": True},
        {"recording_effect_token": "placeholder"},
    ],
)
def test_durable_twilio_rejects_recording_inputs_before_provider_io(
    context_override: dict[str, object],
) -> None:
    effect_token = generate_effect_token("clinic-durable-no-recording")
    if context_override.get("recording_effect_token"):
        context_override["recording_effect_token"] = generate_effect_token(
            "clinic-durable-no-recording"
        )

    def fail_handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("durable recording policy failure must precede provider I/O")

    initiator = TwilioCallInitiator(
        account_sid="AC123",
        auth_token="synthetic-secret",
        from_number="+447700900200",
        twiml_url="https://clinic.example.test/api/v1/voice/twilio/twiml",
        status_callback_url="https://clinic.example.test/api/v1/voice/twilio/call-status",
        transport=httpx.MockTransport(fail_handler),
    )
    context: dict[str, object] = {
        "source": "clinic_recall_voice_worker",
        "record_call": False,
        "effect_token": effect_token,
    }
    context.update(context_override)

    result = initiator.initiate_call(
        target_number="+447700900201",
        context=context,
    )

    assert result.successful is False
    assert result.disposition == CallInitiationDisposition.NOT_DISPATCHED
    assert result.reason_code == CallInitiationReason.DURABLE_POLICY_REJECTED


def test_durable_twilio_rejects_inline_twiml_before_provider_io() -> None:
    effect_token = generate_effect_token("clinic-durable-no-inline")

    def fail_handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("inline TwiML rejection must precede provider I/O")

    initiator = TwilioCallInitiator(
        account_sid="AC123",
        auth_token="synthetic-secret",
        from_number="+447700900200",
        media_stream_url="wss://clinic.example.test/api/v1/twilio/stream",
        status_callback_url="https://clinic.example.test/api/v1/voice/twilio/call-status",
        inline_twiml=True,
        transport=httpx.MockTransport(fail_handler),
    )
    result = initiator.initiate_call(
        target_number="+447700900201",
        context={
            "source": "clinic_recall_voice_worker",
            "record_call": False,
            "effect_token": effect_token,
        },
    )

    assert result.successful is False
    assert result.disposition == CallInitiationDisposition.NOT_DISPATCHED
    assert result.reason_code == CallInitiationReason.DURABLE_POLICY_REJECTED


def test_durable_twilio_requires_effect_token_before_provider_io() -> None:
    def fail_handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("missing effect token must fail before provider I/O")

    initiator = TwilioCallInitiator(
        account_sid="AC123",
        auth_token="synthetic-secret",
        from_number="+447700900200",
        twiml_url="https://clinic.example.test/api/v1/voice/twilio/twiml",
        status_callback_url="https://clinic.example.test/api/v1/voice/twilio/call-status",
        transport=httpx.MockTransport(fail_handler),
    )
    result = initiator.initiate_call(
        target_number="+447700900201",
        context={
            "source": "clinic_recall_voice_worker",
            "record_call": False,
        },
    )

    assert result.successful is False
    assert result.disposition == CallInitiationDisposition.NOT_DISPATCHED
    assert result.reason_code == CallInitiationReason.INVALID_EFFECT_TOKEN


def test_durable_twilio_requires_status_callback_before_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TWILIO_VOICE_STATUS_CALLBACK_URL", raising=False)
    monkeypatch.delenv("TWILIO_WEBHOOK_BASE_URL", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    effect_token = generate_effect_token("clinic-durable-status-required")

    def fail_handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("missing status callback must fail before provider I/O")

    initiator = TwilioCallInitiator(
        account_sid="AC123",
        auth_token="synthetic-secret",
        from_number="+447700900200",
        twiml_url="https://clinic.example.test/api/v1/voice/twilio/twiml",
        transport=httpx.MockTransport(fail_handler),
    )
    result = initiator.initiate_call(
        target_number="+447700900201",
        context={
            "source": "clinic_recall_voice_worker",
            "record_call": False,
            "effect_token": effect_token,
        },
    )

    assert result.successful is False
    assert result.disposition == CallInitiationDisposition.NOT_DISPATCHED
    assert result.reason_code == CallInitiationReason.INVALID_CONFIGURATION


def test_early_amd_callback_and_provider_response_converge_without_second_call(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'early-amd.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as seed_session:
        clinic_id, effect = _seed_blocked_candidate(seed_session, case="ready")

    class _EarlyAmdInitiator:
        name = "synthetic-early-amd"

        def __init__(self) -> None:
            self.calls = 0

        def initiate_call(self, **_kwargs) -> CallInitiationResult:
            self.calls += 1
            with session_factory.begin() as callback_session:
                receive_twilio_callback(
                    callback_session,
                    effect_token=effect.callback_token,
                    callback_kind=ProviderCallbackKind.AMD,
                    fields={"CallSid": CALL_SID, "AnsweredBy": "human"},
                    raw_payload=b"early-amd-before-provider-response",
                    received_at=NOW,
                    apply_immediately=False,
                )
            return CallInitiationResult(
                successful=True,
                call_id=CALL_SID,
                provider=self.name,
                disposition=CallInitiationDisposition.ACCEPTED,
                reason_code=CallInitiationReason.PROVIDER_ACCEPTED,
            )

    initiator = _EarlyAmdInitiator()
    first = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id="worker-early-amd",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW,
        enabled=True,
    )
    reconciliation = reconcile_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id="reconciler-early-amd",
        now=NOW + timedelta(seconds=1),
        enabled=True,
    )
    second = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id="worker-early-amd-second",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW + timedelta(seconds=2),
        enabled=True,
    )

    with session_factory() as session:
        persisted = session.get(ExternalEffect, effect.id)
        receipt = session.execute(select(ProviderCallbackReceipt)).scalar_one()
        job = session.get(OutreachJob, "job-durable-call-blocked")
        assert persisted is not None
        assert persisted.state == ExternalEffectState.SUCCEEDED
        assert persisted.provider_resource_id == CALL_SID
        assert persisted.provider_status == "human_confirmed"
        assert persisted.attempt_count == 1
        assert receipt.state == ProviderCallbackState.APPLIED
        assert job is not None and job.state == OutreachState.NO_REPLY
        assert session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 1
    assert first.provider_accepted == 1
    assert reconciliation.applied == 1
    assert second.claimed == 0
    assert initiator.calls == 1
    engine.dispose()


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (None, False),
        ("", False),
        ("false", False),
        ("unexpected", False),
        ("2", False),
        ("true", True),
        (" TRUE ", True),
        ("1", True),
        ("yes", True),
        ("on", True),
    ],
)
def test_durable_call_runtime_gate_is_explicit_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str | None,
    expected: bool,
) -> None:
    if raw_value is None:
        monkeypatch.delenv("CLINIC_RECALL_DURABLE_CALL_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CLINIC_RECALL_DURABLE_CALL_ENABLED", raw_value)

    assert durable_config.durable_call_enabled() is expected


def test_disabled_call_cli_bootstraps_then_avoids_database_and_provider(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        call_worker_module,
        "_bootstrap_runtime_configuration",
        lambda: events.append("bootstrap"),
        raising=False,
    )
    monkeypatch.setattr(
        call_worker_module,
        "durable_call_enabled",
        lambda: events.append("gate") or False,
        raising=False,
    )
    monkeypatch.setattr(
        call_worker_module,
        "get_sessionmaker",
        lambda: pytest.fail("disabled CLI opened the database"),
        raising=False,
    )
    monkeypatch.setattr(
        call_worker_module,
        "build_call_initiator",
        lambda *_args: pytest.fail("disabled CLI constructed a provider"),
        raising=False,
    )

    exit_code = call_worker_module.main(["--clinic-id", "clinic-disabled-call-cli"])

    assert exit_code == 0
    assert events == ["bootstrap", "gate"]
    assert capsys.readouterr().out.strip() == (
        '{"canceled": 0, "claimed": 0, "enabled": false, '
        '"provider_accepted": 0, "reconcile_required": 0, "rejected": 0}'
    )


@pytest.mark.parametrize("provider_mode", ["auto", "acs", "art", "art_http", ""])
def test_call_cli_fails_closed_on_non_twilio_provider_mode_before_clients(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider_mode: str,
) -> None:
    monkeypatch.setattr(
        call_worker_module,
        "_bootstrap_runtime_configuration",
        lambda: None,
        raising=False,
    )
    monkeypatch.setattr(
        call_worker_module,
        "durable_call_enabled",
        lambda: True,
        raising=False,
    )
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_CALL_PROVIDER", provider_mode)
    monkeypatch.setattr(
        call_worker_module,
        "get_sessionmaker",
        lambda: pytest.fail("invalid provider mode opened the database"),
        raising=False,
    )
    monkeypatch.setattr(
        call_worker_module,
        "build_call_initiator",
        lambda *_args: pytest.fail("invalid provider mode constructed a client"),
        raising=False,
    )

    exit_code = call_worker_module.main(["--clinic-id", "clinic-invalid-provider-mode"])

    assert exit_code == 2
    assert '"enabled": false' in capsys.readouterr().out


def test_enabled_call_cli_hydrates_configuration_before_clients(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []

    def bootstrap() -> None:
        events.append("bootstrap")
        monkeypatch.setenv("CLINIC_RECALL_DURABLE_CALL_ENABLED", "true")
        monkeypatch.setenv("CLINIC_RECALL_DURABLE_CALL_PROVIDER", "twilio")

    def session_factory_builder():
        events.append("database")
        return object()

    def initiator_builder(provider: str):
        assert provider == "twilio"
        events.append("provider")
        return object()

    def fake_run_once(*_args, **kwargs):
        assert kwargs["programme_gate"] is _allow_programme
        events.append("run")
        return call_worker_module.CallRunOnceResult(enabled=True)

    monkeypatch.setattr(call_worker_module, "_bootstrap_runtime_configuration", bootstrap)
    monkeypatch.setattr(
        call_worker_module,
        "_runtime_programme_gate",
        lambda _now: _allow_programme,
    )
    monkeypatch.setattr(call_worker_module, "get_sessionmaker", session_factory_builder)
    monkeypatch.setattr(call_worker_module, "build_call_initiator", initiator_builder)
    monkeypatch.setattr(call_worker_module, "run_once", fake_run_once)

    exit_code = call_worker_module.main(["--clinic-id", "clinic-enabled-call-cli"])

    assert exit_code == 0
    assert events == ["bootstrap", "database", "provider", "run"]
    assert '"enabled": true' in capsys.readouterr().out


def test_enabled_call_cli_with_unbound_programme_gate_opens_no_clients(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(call_worker_module, "_bootstrap_runtime_configuration", lambda: None)
    monkeypatch.setattr(call_worker_module, "durable_call_enabled", lambda: True)
    monkeypatch.setattr(
        call_worker_module,
        "durable_call_provider_is_twilio",
        lambda: True,
    )
    monkeypatch.setattr(
        call_worker_module,
        "_runtime_programme_gate",
        lambda _now: None,
    )
    monkeypatch.setattr(
        call_worker_module,
        "get_sessionmaker",
        lambda: pytest.fail("unbound programme seam opened the database"),
    )
    monkeypatch.setattr(
        call_worker_module,
        "build_call_initiator",
        lambda *_args: pytest.fail("unbound programme seam constructed a provider"),
    )

    exit_code = call_worker_module.main(["--clinic-id", "clinic-unbound-programme-cli"])

    assert exit_code == 2
    assert '"enabled": false' in capsys.readouterr().out


def test_call_worker_commits_dispatching_before_provider_and_never_replays(
    sqlite_session: Session,
) -> None:
    clinic_id = "clinic-durable-call"
    job_id = "job-durable-call"
    sqlite_session.add(
        Clinic(
            id=clinic_id,
            name="Durable Call Clinic",
            timezone="Europe/London",
            daily_caps=20,
        )
    )
    sqlite_session.add(
        Patient(
            id="patient-durable-call",
            clinic_id=clinic_id,
            source_ref="patient-durable-call",
            name="Synthetic Durable Call Patient",
            phone="+447700900101",
            consent_flags={"call": True, "sms": True},
            opt_out_flags={},
        )
    )
    sqlite_session.add(
        Appointment(
            id="appointment-durable-call",
            clinic_id=clinic_id,
            patient_id="patient-durable-call",
            source_ref="appointment-durable-call",
            status="missed",
            start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
        )
    )
    sqlite_session.add(
        Campaign(
            id="campaign-durable-call",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    sqlite_session.add(
        OutreachJob(
            id=job_id,
            clinic_id=clinic_id,
            campaign_id="campaign-durable-call",
            patient_id="patient-durable-call",
            appointment_id="appointment-durable-call",
            channel=Channel.SMS,
            state=OutreachState.NO_REPLY,
        )
    )
    sqlite_session.flush()
    call_effect, _ = enqueue_call_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id=job_id,
        idempotency_key=f"cadence:call:{job_id}",
        available_at=NOW,
    )
    sms_effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id=job_id,
        idempotency_key=f"synthetic:sms:{job_id}",
        available_at=NOW,
    )
    recording_effect = ExternalEffect(
        id="effect-durable-call-recording",
        clinic_id=clinic_id,
        aggregate_type="outreach_job",
        aggregate_id=job_id,
        effect_type=ExternalEffectType.RECORDING,
        idempotency_key=f"synthetic:recording:{job_id}",
        callback_token=generate_effect_token(clinic_id),
        payload_version=1,
        payload={"intent": "synthetic_recording_fixture"},
        request_hash="f" * 64,
        state=ExternalEffectState.PENDING,
        available_at=NOW,
        max_attempts=1,
    )
    sqlite_session.add(recording_effect)
    sqlite_session.commit()

    session_factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    initiator = _ObservingCallInitiator(session_factory, call_effect.id)

    first = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id="worker-durable-call",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW,
        enabled=True,
    )
    second = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id="worker-durable-call-second",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW,
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted_call = sqlite_session.get(ExternalEffect, call_effect.id)
    persisted_sms = sqlite_session.get(ExternalEffect, sms_effect.id)
    persisted_recording = sqlite_session.get(ExternalEffect, recording_effect.id)
    ledger = sqlite_session.execute(
        select(CallRecord).where(CallRecord.external_effect_id == call_effect.id)
    ).scalar_one()

    assert first.claimed == 1
    assert first.provider_accepted == 1
    assert first.rejected == 0
    assert first.canceled == 0
    assert first.reconcile_required == 0
    assert second.claimed == 0
    assert second.provider_accepted == 0
    assert initiator.calls == 1
    assert persisted_call is not None
    assert persisted_call.state == ExternalEffectState.SUCCEEDED
    assert persisted_call.provider_status == "accepted"
    assert persisted_call.provider_resource_id == CALL_SID
    assert persisted_call.attempt_count == 1
    assert ledger.provider_call_id == CALL_SID
    assert ledger.patient_id == "patient-durable-call"
    assert ledger.direction == InteractionDirection.OUTBOUND
    assert ledger.consent_state == RecordingConsentState.NOT_ASKED
    assert ledger.recording_status == CallRecordingStatus.NONE
    assert persisted_sms is not None
    assert persisted_sms.state == ExternalEffectState.PENDING
    assert persisted_recording is not None
    assert persisted_recording.state == ExternalEffectState.PENDING
    assert list(
        sqlite_session.scalars(
            select(ExternalEffect).where(ExternalEffect.effect_type == ExternalEffectType.CALL)
        )
    ) == [persisted_call]


def test_call_worker_commits_entire_batch_dispatching_before_first_provider_io(
    sqlite_session: Session,
) -> None:
    clinic_id = "clinic-durable-call-batch"
    sqlite_session.add(
        Clinic(
            id=clinic_id,
            name="Durable Call Batch Clinic",
            timezone="Europe/London",
            daily_caps=20,
        )
    )
    sqlite_session.add(
        Campaign(
            id="campaign-durable-call-batch",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    effect_ids: set[str] = set()
    for index in (1, 2):
        patient_id = f"patient-durable-call-batch-{index}"
        appointment_id = f"appointment-durable-call-batch-{index}"
        job_id = f"job-durable-call-batch-{index}"
        sqlite_session.add(
            Patient(
                id=patient_id,
                clinic_id=clinic_id,
                source_ref=patient_id,
                name=f"Synthetic Batch Patient {index}",
                phone=f"+44770090010{index}",
                consent_flags={"call": True, "sms": True},
                opt_out_flags={},
            )
        )
        sqlite_session.add(
            Appointment(
                id=appointment_id,
                clinic_id=clinic_id,
                patient_id=patient_id,
                source_ref=appointment_id,
                status="missed",
                start_at=datetime(2026, 7, 10, 9, index, tzinfo=UTC),
            )
        )
        sqlite_session.add(
            OutreachJob(
                id=job_id,
                clinic_id=clinic_id,
                campaign_id="campaign-durable-call-batch",
                patient_id=patient_id,
                appointment_id=appointment_id,
                channel=Channel.SMS,
                state=OutreachState.NO_REPLY,
            )
        )
        sqlite_session.flush()
        effect, _ = enqueue_call_effect(
            sqlite_session,
            clinic_id=clinic_id,
            outreach_job_id=job_id,
            idempotency_key=f"cadence:call:{job_id}",
            available_at=NOW,
        )
        effect_ids.add(effect.id)
    sqlite_session.commit()

    session_factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    initiator = _BatchObservingCallInitiator(session_factory, effect_ids)

    result = run_call_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id="worker-durable-call-batch",
        initiator=initiator,
        programme_gate=_allow_programme,
        now=NOW,
        enabled=True,
        limit=2,
    )

    assert result.claimed == 2
    assert result.provider_accepted == 2
    assert result.reconcile_required == 0
    assert initiator.calls == 2
