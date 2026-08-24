"""Focused contracts for PR-11 server-owned identity evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from src.clinic_recall.availability import (
    AvailabilitySlotInput,
    upsert_availability_slots,
)
from src.clinic_recall.booking import book_inbound_slot
from src.clinic_recall.enums import Channel
from src.clinic_recall.identity_evidence import (
    IdentityAction,
    IdentityEvidenceReason,
    IdentityEvidenceService,
    IdentityFactorRule,
    IdentityPolicy,
    IdentityPolicyMode,
    IdentityProviderField,
    IdentityTier,
)
from src.clinic_recall.models import (
    Appointment,
    BookingAction,
    Clinic,
    IdentityEvidence,
    IdentityFactorAttempt,
    Patient,
)

from tests.identity_evidence_support import grant_synthetic_t2

NOW = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)


def _canonical_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _canonical_date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _synthetic_policy() -> IdentityPolicy:
    return IdentityPolicy(
        version="synthetic-policy-v1",
        mode=IdentityPolicyMode.SYNTHETIC_TEST,
        approval_reference="test-only-pr11-policy",
        factors=(
            IdentityFactorRule(
                factor_type="full_name",
                independence_group="knowledge-name",
                provider_field=IdentityProviderField.FULL_NAME,
                canonicalizer=_canonical_name,
                prompt="Please provide the fictional full name for this test.",
            ),
            IdentityFactorRule(
                factor_type="date_of_birth",
                independence_group="knowledge-dob",
                provider_field=IdentityProviderField.DATE_OF_BIRTH,
                canonicalizer=_canonical_date,
                prompt="Please provide the fictional date of birth for this test.",
            ),
        ),
        expires_after=timedelta(minutes=5),
        max_attempts=3,
        t1_match_count=1,
        t2_match_count=2,
        date_of_birth_factor_type="date_of_birth",
        mismatch_text="The fictional details could not be verified.",
        uncertain_text="The fictional details were not clear enough to verify.",
        handoff_text="A fictional clinic team member will follow up.",
    )


def _seed_identity_candidate(session) -> None:
    session.add(Clinic(id="clinic-pr11", name="Fictional Recall Clinic"))
    session.add(
        Patient(
            id="patient-pr11-avery",
            clinic_id="clinic-pr11",
            source_ref="910000001",
            name="Avery Example",
            phone="+447700900701",
            consent_flags={"sms": True, "call": True},
            opt_out_flags={},
        )
    )
    session.flush()


def _service(
    policy: IdentityPolicy | None,
    *,
    clock=lambda: NOW,
) -> IdentityEvidenceService:
    identifiers = iter(("evidence-pr11-1", "attempt-pr11-1", "attempt-pr11-2"))
    challenges = iter(("challenge-pr11-1", "challenge-pr11-2", "challenge-pr11-3"))
    return IdentityEvidenceService(
        policy=policy,
        clock=clock,
        identifier_factory=lambda: next(identifiers),
        challenge_factory=lambda: next(challenges),
    )


def test_route_name_and_dob_promote_only_through_t0_t1_t2(sqlite_session) -> None:
    _seed_identity_candidate(sqlite_session)
    service = _service(_synthetic_policy())

    started = service.begin(
        sqlite_session,
        clinic_id="clinic-pr11",
        session_id="session-pr11-sms-1",
        route_id="route-pr11-sms-1",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        route_possession=True,
    )

    assert started.tier == IdentityTier.T0
    assert started.reason == IdentityEvidenceReason.ROUTE_ONLY
    assert started.evidence_id == "evidence-pr11-1"
    assert started.challenge_token == "challenge-pr11-1"
    assert service.authorize(
        sqlite_session,
        clinic_id="clinic-pr11",
        evidence_id=started.evidence_id,
        session_id="session-pr11-sms-1",
        route_id="route-pr11-sms-1",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        action=IdentityAction.PATIENT_APPOINTMENT_READ,
    ).allowed is False

    name_match = service.record_factor_result(
        sqlite_session,
        clinic_id="clinic-pr11",
        evidence_id=started.evidence_id,
        session_id="session-pr11-sms-1",
        route_id="route-pr11-sms-1",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        challenge_token=started.challenge_token,
        factor_type="full_name",
        matched=True,
        uncertain=False,
    )

    assert name_match.tier == IdentityTier.T1
    assert name_match.challenge_token == "challenge-pr11-2"
    assert service.authorize(
        sqlite_session,
        clinic_id="clinic-pr11",
        evidence_id=started.evidence_id,
        session_id="session-pr11-sms-1",
        route_id="route-pr11-sms-1",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        action=IdentityAction.GENERIC_BOOKING_REQUEST,
    ).allowed is True
    assert service.authorize(
        sqlite_session,
        clinic_id="clinic-pr11",
        evidence_id=started.evidence_id,
        session_id="session-pr11-sms-1",
        route_id="route-pr11-sms-1",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        action=IdentityAction.BOOK_SLOT,
    ).allowed is False

    dob_match = service.record_factor_result(
        sqlite_session,
        clinic_id="clinic-pr11",
        evidence_id=started.evidence_id,
        session_id="session-pr11-sms-1",
        route_id="route-pr11-sms-1",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        challenge_token=name_match.challenge_token,
        factor_type="date_of_birth",
        matched=True,
        uncertain=False,
    )

    assert dob_match.tier == IdentityTier.T2
    assert dob_match.challenge_token is None
    assert service.authorize(
        sqlite_session,
        clinic_id="clinic-pr11",
        evidence_id=started.evidence_id,
        session_id="session-pr11-sms-1",
        route_id="route-pr11-sms-1",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        action=IdentityAction.BOOK_SLOT,
    ).allowed is True


def test_missing_policy_is_t0_only_and_creates_no_evidence(sqlite_session) -> None:
    _seed_identity_candidate(sqlite_session)
    service = _service(None)

    decision = service.begin(
        sqlite_session,
        clinic_id="clinic-pr11",
        session_id="session-pr11-sms-1",
        route_id="route-pr11-sms-1",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        route_possession=True,
    )

    assert decision.tier == IdentityTier.T0
    assert decision.reason == IdentityEvidenceReason.MISSING_POLICY
    assert decision.evidence_id is None
    assert sqlite_session.query(IdentityEvidence).count() == 0


def test_replayed_challenge_revokes_current_evidence_to_t0(sqlite_session) -> None:
    _seed_identity_candidate(sqlite_session)
    service = _service(_synthetic_policy())
    started = service.begin(
        sqlite_session,
        clinic_id="clinic-pr11",
        session_id="session-pr11-sms-1",
        route_id="route-pr11-sms-1",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        route_possession=True,
    )
    promoted = service.record_factor_result(
        sqlite_session,
        clinic_id="clinic-pr11",
        evidence_id=started.evidence_id,
        session_id="session-pr11-sms-1",
        route_id="route-pr11-sms-1",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        challenge_token=started.challenge_token,
        factor_type="full_name",
        matched=True,
        uncertain=False,
    )
    assert promoted.tier == IdentityTier.T1

    replayed = service.record_factor_result(
        sqlite_session,
        clinic_id="clinic-pr11",
        evidence_id=started.evidence_id,
        session_id="session-pr11-sms-1",
        route_id="route-pr11-sms-1",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        challenge_token=started.challenge_token,
        factor_type="date_of_birth",
        matched=True,
        uncertain=False,
    )

    assert replayed.tier == IdentityTier.T0
    assert replayed.reason == IdentityEvidenceReason.REPLAYED
    assert service.authorize(
        sqlite_session,
        clinic_id="clinic-pr11",
        evidence_id=started.evidence_id,
        session_id="session-pr11-sms-1",
        route_id="route-pr11-sms-1",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        action=IdentityAction.GENERIC_BOOKING_REQUEST,
    ).allowed is False


def test_mismatch_and_uncertain_input_revoke_without_promotion(sqlite_session) -> None:
    for suffix, matched, uncertain, expected_reason in (
        (
            "mismatch",
            False,
            False,
            IdentityEvidenceReason.MISMATCH,
        ),
        (
            "uncertain",
            True,
            True,
            IdentityEvidenceReason.UNCERTAIN,
        ),
    ):
        sqlite_session.rollback()
        sqlite_session.query(IdentityEvidence).delete()
        sqlite_session.query(Patient).delete()
        sqlite_session.query(Clinic).delete()
        sqlite_session.flush()
        _seed_identity_candidate(sqlite_session)
        service = _service(_synthetic_policy())
        started = service.begin(
            sqlite_session,
            clinic_id="clinic-pr11",
            session_id=f"session-pr11-{suffix}",
            route_id=f"route-pr11-{suffix}",
            channel=Channel.SMS,
            patient_id="patient-pr11-avery",
            route_possession=True,
        )

        decision = service.record_factor_result(
            sqlite_session,
            clinic_id="clinic-pr11",
            evidence_id=started.evidence_id,
            session_id=f"session-pr11-{suffix}",
            route_id=f"route-pr11-{suffix}",
            channel=Channel.SMS,
            patient_id="patient-pr11-avery",
            challenge_token=started.challenge_token,
            factor_type="full_name",
            matched=matched,
            uncertain=uncertain,
        )

        assert decision.tier == IdentityTier.T0
        assert decision.reason == expected_reason


def test_expiry_and_cross_channel_reuse_revoke_to_t0(sqlite_session) -> None:
    _seed_identity_candidate(sqlite_session)
    current = [NOW]
    service = _service(_synthetic_policy(), clock=lambda: current[0])
    started = service.begin(
        sqlite_session,
        clinic_id="clinic-pr11",
        session_id="session-pr11-expiry",
        route_id="route-pr11-expiry",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        route_possession=True,
    )
    current[0] = NOW + timedelta(minutes=6)

    expired = service.authorize(
        sqlite_session,
        clinic_id="clinic-pr11",
        evidence_id=started.evidence_id,
        session_id="session-pr11-expiry",
        route_id="route-pr11-expiry",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        action=IdentityAction.GENERIC_BOOKING_REQUEST,
    )

    assert expired.tier == IdentityTier.T0
    assert expired.reason == IdentityEvidenceReason.EXPIRED

    sqlite_session.rollback()
    sqlite_session.query(IdentityEvidence).delete()
    sqlite_session.query(Patient).delete()
    sqlite_session.query(Clinic).delete()
    sqlite_session.flush()
    _seed_identity_candidate(sqlite_session)
    service = _service(_synthetic_policy())
    started = service.begin(
        sqlite_session,
        clinic_id="clinic-pr11",
        session_id="session-pr11-channel",
        route_id="route-pr11-channel",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        route_possession=True,
    )
    reused = service.authorize(
        sqlite_session,
        clinic_id="clinic-pr11",
        evidence_id=started.evidence_id,
        session_id="session-pr11-channel",
        route_id="route-pr11-channel",
        channel=Channel.CALL,
        patient_id="patient-pr11-avery",
        action=IdentityAction.GENERIC_BOOKING_REQUEST,
    )

    assert reused.tier == IdentityTier.T0
    assert reused.reason == IdentityEvidenceReason.BINDING_MISMATCH


def test_stale_policy_version_revokes_and_schema_has_no_raw_factor_columns(
    sqlite_session,
) -> None:
    _seed_identity_candidate(sqlite_session)
    initial_service = _service(_synthetic_policy())
    started = initial_service.begin(
        sqlite_session,
        clinic_id="clinic-pr11",
        session_id="session-pr11-policy",
        route_id="route-pr11-policy",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        route_possession=True,
    )
    changed = replace(_synthetic_policy(), version="synthetic-policy-v2")
    current_service = _service(changed)

    decision = current_service.authorize(
        sqlite_session,
        clinic_id="clinic-pr11",
        evidence_id=started.evidence_id,
        session_id="session-pr11-policy",
        route_id="route-pr11-policy",
        channel=Channel.SMS,
        patient_id="patient-pr11-avery",
        action=IdentityAction.GENERIC_BOOKING_REQUEST,
    )

    assert decision.tier == IdentityTier.T0
    assert decision.reason == IdentityEvidenceReason.STALE_POLICY
    columns = set(IdentityEvidence.__table__.c) | set(IdentityFactorAttempt.__table__.c)
    assert {
        "answer",
        "raw_answer",
        "factor_value",
        "name",
        "date_of_birth",
        "phone",
    }.isdisjoint(columns)


def test_booking_requires_t2_and_binds_action_to_current_evidence(sqlite_session) -> None:
    _seed_identity_candidate(sqlite_session)
    slot_id = upsert_availability_slots(
        sqlite_session,
        "clinic-pr11",
        [
            AvailabilitySlotInput(
                source_ref="fictional-pr11-slot",
                source_provider="cliniko",
                business_id="fictional-business",
                appointment_type_id="fictional-appointment-type",
                clinician_id="fictional-clinician",
                start_at=NOW + timedelta(days=1),
                end_at=NOW + timedelta(days=1, minutes=30),
                fetched_at=NOW,
                expires_at=NOW + timedelta(minutes=30),
            )
        ],
        now=NOW,
    )[0].slot_id

    denied = book_inbound_slot(
        sqlite_session,
        "clinic-pr11",
        patient_id="patient-pr11-avery",
        slot_id=slot_id,
        now=NOW,
        action_type="book",
    )

    assert denied.success is False
    assert denied.error == "identity_t2_required"
    assert sqlite_session.scalar(select(func.count()).select_from(Appointment)) == 0
    assert sqlite_session.scalar(select(func.count()).select_from(BookingAction)) == 0

    service, context = grant_synthetic_t2(
        sqlite_session,
        clinic_id="clinic-pr11",
        patient_id="patient-pr11-avery",
        channel=Channel.SMS,
        now=NOW,
        suffix="booking",
    )
    allowed = book_inbound_slot(
        sqlite_session,
        "clinic-pr11",
        patient_id="patient-pr11-avery",
        slot_id=slot_id,
        now=NOW,
        action_type="book",
        identity_service=service,
        identity_context=context,
    )

    assert allowed.success is True
    action = sqlite_session.execute(select(BookingAction)).scalar_one()
    assert action.identity_evidence_id == context.evidence_id
    assert action.identity_policy_version == "synthetic-test-policy-v1"
    assert action.identity_evidence_revision >= 2
    assert service.authorize_bound_action(
        sqlite_session,
        clinic_id="clinic-pr11",
        evidence_id=action.identity_evidence_id,
        evidence_policy_version=action.identity_policy_version,
        evidence_revision=action.identity_evidence_revision,
        patient_id="patient-pr11-avery",
        channel=Channel.SMS,
        action=IdentityAction.BOOK_SLOT,
    ).allowed is True

    service.revoke(
        sqlite_session,
        clinic_id="clinic-pr11",
        evidence_id=context.evidence_id,
        reason=IdentityEvidenceReason.REVOKED,
    )
    assert service.authorize_bound_action(
        sqlite_session,
        clinic_id="clinic-pr11",
        evidence_id=action.identity_evidence_id,
        evidence_policy_version=action.identity_policy_version,
        evidence_revision=action.identity_evidence_revision,
        patient_id="patient-pr11-avery",
        channel=Channel.SMS,
        action=IdentityAction.BOOK_SLOT,
    ).allowed is False