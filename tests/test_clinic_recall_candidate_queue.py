"""Tests for candidate queue generation (chunk 1d: FR-05 + FR-06 -> queue)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from src.clinic_recall.candidate_queue import generate_candidate_queue
from src.clinic_recall.enums import AuditAction, CampaignStatus, OutreachState, ReasonCode
from src.clinic_recall.models import AuditLog, Campaign, Clinic, OutreachJob, Patient
from src.clinic_recall.pilot_controls import PilotGateDecision
from src.clinic_recall.rights import (
    RightsPolicy,
    SubjectKey,
    SubjectKeyring,
    request_patient_erasure,
)
from src.clinic_recall.sync import CsvSyncSource, upsert_source

NOW = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)  # 13:00 Europe/London (in-hours)

_HEADER = (
    "appointment_source_ref,patient_source_ref,patient_name,patient_phone,"
    "patient_email,status,start_at,value,consent_sms,consent_email,consent_call,"
    "opt_out_sms,opt_out_email,opt_out_call\n"
)

# One appointment per reason code, plus one per eligibility skip reason.
SCENARIO_CSV = _HEADER + "\n".join(
    [
        # Detected + eligible -> queued (one per reason code).
        "M1,P1,Missed,+447700900001,,missed,2026-06-20T09:00:00Z,,yes,no,no,no,no,no",
        "C1,P2,Cancelled,+447700900002,,cancelled,2026-06-21T10:00:00Z,,yes,no,no,no,no,no",
        "U1,P3,Upcoming,+447700900003,,scheduled,2026-06-27T12:00:00Z,,yes,no,no,no,no,no",
        "D1,P4,DueRecurring,+447700900004,,scheduled,2026-07-06T12:00:00Z,,yes,no,no,no,no,no",
        "O1,P5,Overdue,+447700900005,,completed,2026-05-17T12:00:00Z,,yes,no,no,no,no,no",
        # Detected but no reason -> ignored.
        "N1,P6,NoReason,+447700900006,,scheduled,2026-07-26T12:00:00Z,,yes,no,no,no,no,no",
        # Detected (missed) but skipped by eligibility.
        "S1,P7,NoConsent,+447700900007,,missed,2026-06-20T09:00:00Z,,no,no,no,no,no,no",
        "S2,P8,OptedOut,+447700900008,,missed,2026-06-20T09:00:00Z,,yes,no,no,yes,no,no",
        "S3,P9,NoPhone,,,missed,2026-06-20T09:00:00Z,,yes,no,no,no,no,no",
    ]
)


def _seed_clinic(session, clinic_id="clinic-cq", daily_caps=200):
    session.add(Clinic(id=clinic_id, name="CQ", timezone="Europe/London", daily_caps=daily_caps))
    session.flush()
    return clinic_id


def _allow_pilot(*_args) -> PilotGateDecision:
    return PilotGateDecision(True, "allowed")


def test_full_scenario_detection_eligibility_and_queue(sqlite_session):
    clinic_id = _seed_clinic(sqlite_session)
    upsert_source(sqlite_session, clinic_id, CsvSyncSource.from_text(SCENARIO_CSV))

    result = generate_candidate_queue(
        sqlite_session, clinic_id, NOW, pilot_gate=_allow_pilot
    )

    assert dict(result.detected) == {
        "missed": 4,  # P1, P7, P8, P9
        "cancelled": 1,
        "upcoming_reminder": 1,
        "due_recurring": 1,
        "overdue_followup": 1,
    }
    assert result.detected_total == 8
    assert result.queued == 5
    assert dict(result.skipped) == {"no_consent": 1, "opted_out": 1, "not_contactable": 1}

    jobs = sqlite_session.execute(select(OutreachJob)).scalars().all()
    assert len(jobs) == 5
    assert all(job.state == OutreachState.QUEUED for job in jobs)
    assert {job.reason_code for job in jobs} == {
        ReasonCode.MISSED,
        ReasonCode.CANCELLED,
        ReasonCode.UPCOMING_REMINDER,
        ReasonCode.DUE_RECURRING,
        ReasonCode.OVERDUE_FOLLOWUP,
    }


def test_queue_uses_standing_detection_campaign(sqlite_session):
    clinic_id = _seed_clinic(sqlite_session)
    upsert_source(sqlite_session, clinic_id, CsvSyncSource.from_text(SCENARIO_CSV))
    generate_candidate_queue(sqlite_session, clinic_id, NOW, pilot_gate=_allow_pilot)

    campaigns = sqlite_session.execute(select(Campaign)).scalars().all()
    assert len(campaigns) == 1
    assert campaigns[0].id == f"campaign-detection-{clinic_id}"
    assert campaigns[0].status == CampaignStatus.DRAFT
    jobs = sqlite_session.execute(select(OutreachJob)).scalars().all()
    assert all(job.campaign_id == campaigns[0].id for job in jobs)


def test_generation_refresh_returns_detection_campaign_to_draft(sqlite_session):
    clinic_id = _seed_clinic(sqlite_session)
    upsert_source(sqlite_session, clinic_id, CsvSyncSource.from_text(SCENARIO_CSV))
    generate_candidate_queue(sqlite_session, clinic_id, NOW, pilot_gate=_allow_pilot)
    campaign = sqlite_session.get(Campaign, f"campaign-detection-{clinic_id}")
    campaign.status = CampaignStatus.ACTIVE

    generate_candidate_queue(sqlite_session, clinic_id, NOW, pilot_gate=_allow_pilot)

    assert campaign.status == CampaignStatus.DRAFT


def test_generation_is_idempotent(sqlite_session):
    clinic_id = _seed_clinic(sqlite_session)
    upsert_source(sqlite_session, clinic_id, CsvSyncSource.from_text(SCENARIO_CSV))

    generate_candidate_queue(sqlite_session, clinic_id, NOW, pilot_gate=_allow_pilot)
    second = generate_candidate_queue(
        sqlite_session, clinic_id, NOW, pilot_gate=_allow_pilot
    )

    assert second.queued == 0
    assert second.already_queued == 5
    # Still exactly five jobs after a second run.
    assert sqlite_session.execute(select(func.count()).select_from(OutreachJob)).scalar() == 5


def test_enqueue_is_audited(sqlite_session):
    clinic_id = _seed_clinic(sqlite_session)
    upsert_source(sqlite_session, clinic_id, CsvSyncSource.from_text(SCENARIO_CSV))
    generate_candidate_queue(sqlite_session, clinic_id, NOW, pilot_gate=_allow_pilot)

    enqueue_audits = sqlite_session.execute(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == AuditAction.ENQUEUE_OUTREACH)
    ).scalar()
    skip_audits = sqlite_session.execute(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == AuditAction.SKIP_CANDIDATE)
    ).scalar()
    assert enqueue_audits == 5
    assert skip_audits == 3


def test_frozen_patient_is_never_added_to_candidate_queue(sqlite_session):
    clinic_id = _seed_clinic(sqlite_session)
    source = CsvSyncSource.from_text(
        _HEADER
        + "FROZEN,P-FROZEN,Frozen,+447700900099,,missed,2026-06-20T09:00:00Z,,"
        "yes,no,no,no,no,no\n"
    )
    upsert_source(sqlite_session, clinic_id, source)
    patient = sqlite_session.execute(
        select(Patient).where(Patient.source_ref == "P-FROZEN")
    ).scalar_one()
    request_patient_erasure(
        sqlite_session,
        clinic_id=clinic_id,
        patient_id=patient.id,
        confirm_token=f"ERASE {patient.id}",
        request_identity="tests-candidate-freeze",
        actor_role="dpo",
        actor_reference="tests-candidate-operator",
        keyring=SubjectKeyring(
            current=SubjectKey(
                version="tests-candidate-v1",
                secret=b"tests-candidate-rights-key",
            )
        ),
        policy=RightsPolicy(
            version="tests-candidate-policy-v1",
            approval_evidence_hash="a" * 64,
            request_due_after=timedelta(days=28),
        ),
        now=NOW,
    )

    result = generate_candidate_queue(
        sqlite_session,
        clinic_id,
        NOW,
        pilot_gate=_allow_pilot,
    )

    assert result.queued == 0
    assert result.skipped["subject_frozen"] == 1
    assert sqlite_session.execute(select(func.count()).select_from(OutreachJob)).scalar() == 0


def test_per_patient_frequency_cap_enforced_within_run(sqlite_session):
    clinic_id = _seed_clinic(sqlite_session)
    # One patient with four missed appointments; default weekly cap is 3.
    rows = _HEADER + "\n".join(
        f"F{i},PF,Frequent,+447700900010,,missed,2026-06-2{i}T09:00:00Z,,yes,no,no,no,no,no"
        for i in range(1, 5)
    )
    upsert_source(sqlite_session, clinic_id, CsvSyncSource.from_text(rows))

    result = generate_candidate_queue(
        sqlite_session, clinic_id, NOW, pilot_gate=_allow_pilot
    )
    assert result.detected["missed"] == 4
    assert result.queued == 3
    assert result.skipped["frequency_cap"] == 1


def test_daily_clinic_cap_enforced_within_run(sqlite_session):
    clinic_id = _seed_clinic(sqlite_session, daily_caps=2)
    upsert_source(sqlite_session, clinic_id, CsvSyncSource.from_text(SCENARIO_CSV))

    result = generate_candidate_queue(
        sqlite_session, clinic_id, NOW, pilot_gate=_allow_pilot
    )
    assert result.queued == 2
    assert result.skipped["daily_cap"] >= 1
