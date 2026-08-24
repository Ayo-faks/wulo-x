"""Tests for the CSV sync adapter and idempotent upsert (chunk 1b)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from src.clinic_recall.db import current_clinic_id
from src.clinic_recall.enums import AppointmentStatus, AuditAction
from src.clinic_recall.models import Appointment, AuditLog, Clinic, Patient, RightsRequest
from src.clinic_recall.rights import (
    RightsPolicy,
    SubjectFrozenError,
    SubjectKey,
    SubjectKeyring,
    request_patient_erasure,
)
from src.clinic_recall.sync import (
    CsvSyncError,
    CsvSyncSource,
    NormalizedAppointment,
    NormalizedPatient,
    SyncIntegrityError,
    upsert_source,
)

VALID_CSV = (
    "appointment_source_ref,patient_source_ref,patient_name,patient_phone,"
    "patient_email,status,start_at,value,consent_sms,consent_email,consent_call,"
    "opt_out_sms,opt_out_email,opt_out_call\n"
    "A1,P1,Alice,+447700900001,alice@example.com,missed,2026-06-20T09:00:00+00:00,"
    "50.00,yes,no,yes,no,no,no\n"
    "A2,P1,Alice,+447700900001,alice@example.com,scheduled,2026-07-01T09:00:00+00:00,"
    ",1,0,1,0,0,0\n"
    "A3,P2,Bob,,bob@example.com,cancelled,2026-06-21T10:00:00Z,,true,true,false,"
    "false,false,false\n"
)


def _add_clinic(session, clinic_id="clinic-x"):
    session.add(Clinic(id=clinic_id, name="Clinic X"))
    session.flush()
    return clinic_id


# --------------------------------------------------------------------------- #
# CSV parsing / validation
# --------------------------------------------------------------------------- #
def test_csv_parses_patients_and_appointments():
    source = CsvSyncSource.from_text(VALID_CSV)
    patients = source.fetch_patients()
    appointments = source.fetch_appointments()
    assert {p.source_ref for p in patients} == {"P1", "P2"}  # P1 deduped
    assert len(appointments) == 3
    assert appointments[0].status == AppointmentStatus.MISSED
    assert appointments[0].start_at == datetime(2026, 6, 20, 9, 0, tzinfo=UTC)


def test_csv_missing_required_column_is_rejected():
    no_status = VALID_CSV.replace(",status,", ",").replace(",missed,", ",")
    with pytest.raises(CsvSyncError, match="missing required columns"):
        CsvSyncSource.from_text(no_status)


def test_csv_naive_datetime_is_rejected():
    naive = VALID_CSV.replace("2026-06-20T09:00:00+00:00", "2026-06-20T09:00:00")
    with pytest.raises(CsvSyncError, match="row 2"):
        CsvSyncSource.from_text(naive)


def test_csv_neutralises_formula_injection_in_name():
    row = (
        "appointment_source_ref,patient_source_ref,patient_name,status,start_at\n"
        "A9,P9,=cmd|'/c calc'!A1,missed,2026-06-20T09:00:00Z\n"
    )
    source = CsvSyncSource.from_text(row)
    assert source.fetch_patients()[0].name.startswith("'=")


def test_csv_invalid_phone_becomes_none():
    row = (
        "appointment_source_ref,patient_source_ref,patient_name,patient_phone,status,start_at\n"
        "A8,P8,Carol,not-a-phone,missed,2026-06-20T09:00:00Z\n"
    )
    assert CsvSyncSource.from_text(row).fetch_patients()[0].phone is None


def test_csv_bad_boolean_is_rejected():
    row = (
        "appointment_source_ref,patient_source_ref,patient_name,status,start_at,consent_sms\n"
        "A7,P7,Dave,missed,2026-06-20T09:00:00Z,maybe\n"
    )
    with pytest.raises(CsvSyncError, match="invalid boolean"):
        CsvSyncSource.from_text(row)


# --------------------------------------------------------------------------- #
# Idempotent upsert
# --------------------------------------------------------------------------- #
def test_upsert_is_idempotent(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    source = CsvSyncSource.from_text(VALID_CSV)

    first = upsert_source(sqlite_session, clinic_id, source)
    assert (first.patients_inserted, first.appointments_inserted) == (2, 3)
    assert (first.patients_updated, first.appointments_updated) == (0, 0)

    second = upsert_source(sqlite_session, clinic_id, source)
    assert (second.patients_inserted, second.appointments_inserted) == (0, 0)
    assert (second.patients_updated, second.appointments_updated) == (2, 3)

    # No duplicate rows after two runs.
    assert sqlite_session.execute(select(func.count()).select_from(Patient)).scalar() == 2
    assert sqlite_session.execute(select(func.count()).select_from(Appointment)).scalar() == 3


def test_upsert_writes_audit_record(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    upsert_source(sqlite_session, clinic_id, CsvSyncSource.from_text(VALID_CSV))
    audits = (
        sqlite_session.execute(select(AuditLog).where(AuditLog.action == AuditAction.SYNC_UPSERT))
        .scalars()
        .all()
    )
    assert len(audits) == 1
    assert audits[0].clinic_id == clinic_id
    assert audits[0].payload_hash  # a hash was recorded


def test_upsert_updates_changed_fields(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    upsert_source(sqlite_session, clinic_id, CsvSyncSource.from_text(VALID_CSV))

    # Re-sync with Bob's status changed from cancelled to completed.
    changed = VALID_CSV.replace(
        "A3,P2,Bob,,bob@example.com,cancelled", "A3,P2,Bob,,bob@example.com,completed"
    )
    upsert_source(sqlite_session, clinic_id, CsvSyncSource.from_text(changed))
    appt = sqlite_session.execute(
        select(Appointment).where(Appointment.source_ref == "A3")
    ).scalar_one()
    assert appt.status == AppointmentStatus.COMPLETED


def test_upsert_preserves_unset_consent_authority_and_allows_explicit_clear(
    sqlite_session,
):
    clinic_id = _add_clinic(sqlite_session)

    class _PatientSource:
        name = "authority-test"

        def __init__(self, patient: NormalizedPatient) -> None:
            self.patient = patient

        def fetch_patients(self):
            return (self.patient,)

        def fetch_appointments(self):
            return ()

    upsert_source(
        sqlite_session,
        clinic_id,
        _PatientSource(
            NormalizedPatient(
                source_ref="P-AUTHORITY",
                name="Original",
                consent_flags={"sms": True},
                opt_out_flags={"sms": False},
            )
        ),
    )
    upsert_source(
        sqlite_session,
        clinic_id,
        _PatientSource(
            NormalizedPatient(
                source_ref="P-AUTHORITY",
                name="Cliniko Update",
                consent_flags=None,
                opt_out_flags=None,
            )
        ),
    )
    patient = sqlite_session.execute(
        select(Patient).where(Patient.source_ref == "P-AUTHORITY")
    ).scalar_one()
    assert patient.name == "Cliniko Update"
    assert patient.consent_flags == {"sms": True}
    assert patient.opt_out_flags == {"sms": False}

    upsert_source(
        sqlite_session,
        clinic_id,
        _PatientSource(
            NormalizedPatient(
                source_ref="P-AUTHORITY",
                name="Authoritative Clear",
                consent_flags={},
                opt_out_flags={},
            )
        ),
    )
    assert patient.consent_flags == {}
    assert patient.opt_out_flags == {}

    upsert_source(
        sqlite_session,
        clinic_id,
        _PatientSource(
            NormalizedPatient(
                source_ref="P-FAIL-CLOSED",
                name="New Unauthoritative Patient",
                consent_flags=None,
                opt_out_flags=None,
            )
        ),
    )
    new_patient = sqlite_session.execute(
        select(Patient).where(Patient.source_ref == "P-FAIL-CLOSED")
    ).scalar_one()
    assert new_patient.consent_flags == {}
    assert new_patient.opt_out_flags == {}


def test_upsert_materializes_all_source_data_before_clinic_scope(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    calls: list[str] = []

    def assert_outside_scope() -> None:
        with pytest.raises(LookupError, match="No clinic scope"):
            current_clinic_id()

    class _OrderingSource:
        name = "ordering-test"

        def fetch_patients(self):
            assert_outside_scope()
            calls.append("patients")
            return (NormalizedPatient(source_ref="P-ORDER", name="Ordering"),)

        def fetch_appointments(self):
            assert_outside_scope()
            calls.append("appointments")
            return ()

    upsert_source(sqlite_session, clinic_id, _OrderingSource())

    assert calls == ["patients", "appointments"]


def test_upsert_rejects_appointment_with_unknown_patient(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)

    class _OrphanSource:
        name = "fake"

        def fetch_patients(self):
            return [NormalizedPatient(source_ref="known", name="Known")]

        def fetch_appointments(self):
            return [
                NormalizedAppointment(
                    source_ref="X1",
                    patient_source_ref="ghost",
                    status=AppointmentStatus.MISSED,
                    start_at=datetime(2026, 6, 20, 9, 0, tzinfo=UTC),
                )
            ]

    with pytest.raises(SyncIntegrityError, match="unknown patient"):
        upsert_source(sqlite_session, clinic_id, _OrphanSource())


def test_upsert_rejects_permanent_erasure_tombstone_after_patient_removal(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    source = CsvSyncSource.from_text(
        "appointment_source_ref,patient_source_ref,patient_name,status,start_at\n"
        "A-RIGHTS,P-RIGHTS,Erased Person,missed,2026-06-20T09:00:00Z\n"
    )
    keyring = SubjectKeyring(
        current=SubjectKey(
            version="tests-sync-v1",
            secret=b"tests-only-sync-rights-key",
        )
    )
    policy = RightsPolicy(
        version="tests-sync-policy-v1",
        approval_evidence_hash="a" * 64,
        request_due_after=timedelta(days=28),
    )
    upsert_source(sqlite_session, clinic_id, source, keyring=keyring)
    patient = sqlite_session.execute(
        select(Patient).where(Patient.source_ref == "P-RIGHTS")
    ).scalar_one()
    request_patient_erasure(
        sqlite_session,
        clinic_id=clinic_id,
        patient_id=patient.id,
        confirm_token=f"ERASE {patient.id}",
        request_identity="tests-sync-erasure",
        actor_role="dpo",
        actor_reference="tests-sync-operator",
        keyring=keyring,
        policy=policy,
        now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )
    request = sqlite_session.execute(select(RightsRequest)).scalar_one()
    request.patient_id = None
    sqlite_session.execute(delete(Appointment).where(Appointment.patient_id == patient.id))
    sqlite_session.execute(delete(Patient).where(Patient.id == patient.id))
    sqlite_session.flush()

    with pytest.raises(SubjectFrozenError, match="subject_frozen"):
        upsert_source(
            sqlite_session,
            clinic_id,
            source,
            keyring=SubjectKeyring(
                current=SubjectKey(
                    version="tests-sync-v2",
                    secret=b"tests-only-rotated-sync-key",
                ),
                previous=(keyring.current,),
            ),
        )

    assert sqlite_session.execute(select(func.count()).select_from(Patient)).scalar() == 0
    assert sqlite_session.execute(select(func.count()).select_from(Appointment)).scalar() == 0
