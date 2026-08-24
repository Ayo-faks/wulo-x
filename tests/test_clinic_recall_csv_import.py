"""PR-08 controlled CSV import: preview, approval, consent, and provenance tests.

Every fixture identity is synthetic (see ``tests/fixtures/csv/pr08/manifest.json``).
The import contract under test:

    authorized preview (metadata only)
      -> explicit same-bytes re-upload approval
      -> one atomic tenant-scoped import
      -> no raw retention, no outreach/booking side effects
      -> presence-aware fail-closed consent, monotonic opt-out
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from src.clinic_recall.enums import (
    AuditAction,
    ImportBatchState,
    MatchStrategy,
    SourceSystem,
)
from src.clinic_recall.models import (
    Appointment,
    AuditLog,
    BookingAction,
    Campaign,
    Clinic,
    ExternalEffect,
    ImportBatch,
    ImportMatchReview,
    Interaction,
    OutreachJob,
    Patient,
    PatientSourceLink,
    PilotParticipant,
)
from src.clinic_recall.rights import (
    RightsPolicy,
    SubjectKey,
    SubjectKeyring,
    request_patient_erasure,
)
from src.clinic_recall.sync import CsvSyncSource, upsert_source
from src.clinic_recall.sync.csv_consent import CsvImportPolicy
from src.clinic_recall.sync.csv_import import (
    CsvImportAttestation,
    CsvImportError,
    approve_csv_import,
    preview_csv_import,
)
from src.clinic_recall.sync.csv_matching import create_patient_source_link

FIXTURES = Path(__file__).parent / "fixtures" / "csv" / "pr08"

VALID_BYTES = (FIXTURES / "valid_multi.csv").read_bytes()
CHANGED_BYTES = (FIXTURES / "changed_variant.csv").read_bytes()

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
EXPORT_AT = datetime(2026, 7, 25, 18, 0, tzinfo=UTC)

KEYRING = SubjectKeyring(
    current=SubjectKey(version="tests-v1", secret=b"tests-only-secret-material-01")
)

RIGHTS_POLICY = RightsPolicy(
    version="tests-rights-v1",
    approval_evidence_hash="c" * 64,
    request_due_after=timedelta(days=28),
)

# Raw fixture values that must never appear in errors, provenance, or audit rows.
RAW_MARKERS = ("PAT-PR08", "APPT-PR08", "Alpha", "+44770090", "clinic-test.invalid")


def _policy(**overrides) -> CsvImportPolicy:
    base = dict(
        version="test-csv-policy-v1",
        statement_hash="a" * 64,
        attestation_versions=("attest-v1",),
        channels=("sms", "email", "call"),
        max_evidence_age=None,  # no controller-approved age policy -> grant nothing
        preview_ttl=timedelta(minutes=30),
        allowed_source_systems=(SourceSystem.CSV,),
    )
    base.update(overrides)
    return CsvImportPolicy(**base)


def _attestation(**overrides) -> CsvImportAttestation:
    base = dict(
        source_system=SourceSystem.CSV,
        export_at=EXPORT_AT,
        attestation_version="attest-v1",
        attested_channels=("sms",),
        confirm_clinic_authority=True,
    )
    base.update(overrides)
    return CsvImportAttestation(**base)


def _add_clinic(session, clinic_id="clinic-pr08-a"):
    session.add(Clinic(id=clinic_id, name="PR08 Test Clinic"))
    session.flush()
    return clinic_id


def _preview(session, clinic_id, data=VALID_BYTES, *, policy=None, now=NOW):
    return preview_csv_import(
        session,
        clinic_id,
        materialization=CsvSyncSource.materialize(data),
        source_system=SourceSystem.CSV,
        export_at=EXPORT_AT,
        actor="staff:test-alice",
        now=now,
        policy=policy or _policy(),
        upload_disposed_at=now,
    )


def _approve(session, clinic_id, batch_id, data, *, policy=None, now=LATER, **kw):
    return approve_csv_import(
        session,
        clinic_id,
        batch_id,
        materialization=CsvSyncSource.materialize(data),
        attestation=kw.pop("attestation", _attestation()),
        actor="staff:test-alice",
        now=now,
        policy=policy or _policy(),
        keyring=KEYRING,
        upload_disposed_at=now,
        **kw,
    )


def _count(session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def _assert_no_import_side_effects(session):
    for model in (
        Patient,
        Appointment,
        PatientSourceLink,
        ImportMatchReview,
        Campaign,
        OutreachJob,
        ExternalEffect,
        BookingAction,
        Interaction,
        PilotParticipant,
    ):
        assert _count(session, model) == 0, f"unexpected {model.__name__} rows"


# --------------------------------------------------------------------------- #
# The falsifier: changed bytes at approval import nothing
# --------------------------------------------------------------------------- #
def test_changed_bytes_at_approval_imports_nothing(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    preview = _preview(sqlite_session, clinic_id)
    sqlite_session.commit()
    assert preview.batch.state == ImportBatchState.PREVIEW_VALID

    with pytest.raises(CsvImportError) as excinfo:
        _approve(sqlite_session, clinic_id, preview.batch.id, CHANGED_BYTES)
    sqlite_session.rollback()

    # One bounded reason; no raw fixture value leaks through the error.
    assert excinfo.value.reason == "file_hash_mismatch"
    for marker in RAW_MARKERS:
        assert marker not in str(excinfo.value)

    # Zero patient/appointment/link/review/campaign/job/effect/booking rows.
    _assert_no_import_side_effects(sqlite_session)

    # The preview stays valid for a same-file retry; nothing completed.
    batch = sqlite_session.get(ImportBatch, preview.batch.id)
    assert batch.state == ImportBatchState.PREVIEW_VALID
    assert batch.completed_at is None
    assert batch.approved_at is None

    # No consent or opt-out state exists to weaken; no audit beyond preview.
    approve_audits = [
        row
        for row in sqlite_session.execute(select(AuditLog)).scalars()
        if row.action == AuditAction.CSV_IMPORT_APPROVE
    ]
    assert approve_audits == []


# --------------------------------------------------------------------------- #
# Preview is metadata-only
# --------------------------------------------------------------------------- #
def test_preview_creates_metadata_only(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    preview = _preview(sqlite_session, clinic_id)
    sqlite_session.commit()

    _assert_no_import_side_effects(sqlite_session)
    batch = sqlite_session.get(ImportBatch, preview.batch.id)
    assert batch.state == ImportBatchState.PREVIEW_VALID
    assert batch.total_rows == 4
    assert batch.valid_row_count == 4
    assert batch.invalid_row_count == 0
    assert batch.patient_count == 3
    assert batch.appointment_count == 4
    assert len(batch.file_sha256) == 64
    assert batch.preview_upload_disposed_at is not None

    # No raw values or filename-like content anywhere in the persisted row.
    persisted = repr(vars(batch))
    for marker in RAW_MARKERS:
        assert marker not in persisted
    assert "valid_multi" not in persisted


def test_preview_of_invalid_file_is_not_importable(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    data = (FIXTURES / "conflicting_patient_facts.csv").read_bytes()
    preview = _preview(sqlite_session, clinic_id, data)
    sqlite_session.commit()

    assert preview.batch.state == ImportBatchState.PREVIEW_INVALID
    assert preview.batch.invalid_row_count > 0
    assert preview.errors  # bounded safe errors returned, not persisted
    with pytest.raises(CsvImportError) as excinfo:
        _approve(sqlite_session, clinic_id, preview.batch.id, data)
    sqlite_session.rollback()
    assert excinfo.value.reason == "not_importable"
    _assert_no_import_side_effects(sqlite_session)


# --------------------------------------------------------------------------- #
# Same-file approval: the golden path
# --------------------------------------------------------------------------- #
def test_same_file_approval_imports_atomically(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    preview = _preview(sqlite_session, clinic_id)
    sqlite_session.commit()

    result = _approve(sqlite_session, clinic_id, preview.batch.id, VALID_BYTES)
    sqlite_session.commit()

    assert result.replayed is False
    batch = sqlite_session.get(ImportBatch, preview.batch.id)
    assert batch.state == ImportBatchState.COMPLETED
    assert batch.completed_at is not None
    assert batch.approval_upload_disposed_at is not None
    assert batch.patients_inserted == 3
    assert batch.appointments_inserted == 4
    assert _count(sqlite_session, Patient) == 3
    assert _count(sqlite_session, Appointment) == 4
    assert _count(sqlite_session, PatientSourceLink) == 3
    links = list(sqlite_session.execute(select(PatientSourceLink)).scalars())
    assert {link.provider for link in links} == {SourceSystem.CSV}
    assert {link.import_batch_id for link in links} == {preview.batch.id}
    # Import grants no outreach/booking/campaign authority.
    for model in (
        Campaign,
        OutreachJob,
        ExternalEffect,
        BookingAction,
        Interaction,
        PilotParticipant,
    ):
        assert _count(sqlite_session, model) == 0


def test_completed_replay_is_idempotent(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    preview = _preview(sqlite_session, clinic_id)
    sqlite_session.commit()
    _approve(sqlite_session, clinic_id, preview.batch.id, VALID_BYTES)
    sqlite_session.commit()

    audit_before = _count(sqlite_session, AuditLog)
    replay = _approve(sqlite_session, clinic_id, preview.batch.id, VALID_BYTES)
    sqlite_session.commit()

    assert replay.replayed is True
    assert _count(sqlite_session, Patient) == 3
    assert _count(sqlite_session, Appointment) == 4
    assert _count(sqlite_session, AuditLog) == audit_before  # no new audit rows


def test_completed_replay_rejects_changed_approval_metadata(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    preview = _preview(sqlite_session, clinic_id)
    sqlite_session.commit()
    _approve(sqlite_session, clinic_id, preview.batch.id, VALID_BYTES)
    sqlite_session.commit()

    with pytest.raises(CsvImportError) as excinfo:
        _approve(
            sqlite_session,
            clinic_id,
            preview.batch.id,
            VALID_BYTES,
            attestation=_attestation(export_at=EXPORT_AT + timedelta(hours=1)),
        )
    sqlite_session.rollback()

    assert excinfo.value.reason == "source_metadata_mismatch"
    assert _count(sqlite_session, Patient) == 3
    assert _count(sqlite_session, Appointment) == 4
    assert _count(sqlite_session, PatientSourceLink) == 3


def test_expired_preview_cannot_be_approved(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    preview = _preview(sqlite_session, clinic_id)
    sqlite_session.commit()

    with pytest.raises(CsvImportError) as excinfo:
        _approve(
            sqlite_session,
            clinic_id,
            preview.batch.id,
            VALID_BYTES,
            now=NOW + timedelta(hours=2),
        )
    sqlite_session.rollback()
    assert excinfo.value.reason == "preview_expired"
    _assert_no_import_side_effects(sqlite_session)


# --------------------------------------------------------------------------- #
# Erasure fails closed at approval
# --------------------------------------------------------------------------- #
def test_same_file_erased_subject_fails_closed(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    # An earlier patient existed under PAT-PR08-001 and was erased.
    session = sqlite_session
    session.add(
        Patient(
            id="pat-early",
            clinic_id=clinic_id,
            source_ref="PAT-PR08-001",
            name="Test Patient Early",
            consent_flags={},
            opt_out_flags={},
        )
    )
    session.flush()
    request_patient_erasure(
        session,
        clinic_id=clinic_id,
        patient_id="pat-early",
        confirm_token="ERASE pat-early",
        request_identity="tests:erase-1",
        actor_role="staff",
        actor_reference="staff:test-alice",
        keyring=KEYRING,
        policy=RIGHTS_POLICY,
        now=NOW - timedelta(days=1),
    )
    session.commit()

    preview = _preview(session, clinic_id)
    session.commit()

    with pytest.raises(CsvImportError) as excinfo:
        _approve(session, clinic_id, preview.batch.id, VALID_BYTES)
    session.rollback()
    assert excinfo.value.reason == "subject_frozen"
    # Nothing imported: the frozen patient row is untouched (PR-10 deletes it
    # later through the durable rights worker) and no other rows were created.
    assert _count(session, Appointment) == 0
    assert _count(session, Patient) == 1
    frozen = session.execute(
        select(Patient).where(Patient.source_ref == "PAT-PR08-001")
    ).scalar_one()
    assert frozen.name == "Test Patient Early"  # CSV values did not land
    assert frozen.phone is None
    batch = session.get(ImportBatch, preview.batch.id)
    assert batch.state == ImportBatchState.PREVIEW_VALID
    assert batch.completed_at is None


# --------------------------------------------------------------------------- #
# Consent and opt-out authority
# --------------------------------------------------------------------------- #
def test_positive_consent_stays_unknown_without_approved_age_policy(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    preview = _preview(sqlite_session, clinic_id)
    sqlite_session.commit()
    _approve(sqlite_session, clinic_id, preview.batch.id, VALID_BYTES)
    sqlite_session.commit()

    alpha = sqlite_session.execute(
        select(Patient).where(Patient.source_ref == "PAT-PR08-001")
    ).scalar_one()
    # File says consent_sms=yes, but no approved evidence-age policy exists.
    assert alpha.consent_flags.get("sms") is not True
    beta = sqlite_session.execute(
        select(Patient).where(Patient.source_ref == "PAT-PR08-002")
    ).scalar_one()
    assert beta.consent_flags.get("sms") is not True
    assert beta.consent_flags.get("email") is not True
    # Opt-out remains authoritative: Gamma opted out of sms.
    gamma = sqlite_session.execute(
        select(Patient).where(Patient.source_ref == "PAT-PR08-003")
    ).scalar_one()
    assert gamma.opt_out_flags.get("sms") is True


def test_attested_policy_grants_only_attested_channels(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    policy = _policy(max_evidence_age=timedelta(days=365))
    preview = _preview(sqlite_session, clinic_id, policy=policy)
    sqlite_session.commit()
    _approve(
        sqlite_session,
        clinic_id,
        preview.batch.id,
        VALID_BYTES,
        policy=policy,
        attestation=_attestation(attested_channels=("sms",)),
    )
    sqlite_session.commit()

    alpha = sqlite_session.execute(
        select(Patient).where(Patient.source_ref == "PAT-PR08-001")
    ).scalar_one()
    assert alpha.consent_flags.get("sms") is True  # attested + policy-current
    beta = sqlite_session.execute(
        select(Patient).where(Patient.source_ref == "PAT-PR08-002")
    ).scalar_one()
    # consent_email=true in file, but email was not attested.
    assert beta.consent_flags.get("email") is not True


def test_absent_consent_columns_preserve_existing_consent(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    sqlite_session.add(
        Patient(
            id="pat-existing",
            clinic_id=clinic_id,
            source_ref="PAT-PR08-002",
            name="Test Patient Beta",
            consent_flags={"sms": True},
            opt_out_flags={},
        )
    )
    sqlite_session.commit()

    preview = _preview(sqlite_session, clinic_id, CHANGED_BYTES)
    sqlite_session.commit()
    _approve(sqlite_session, clinic_id, preview.batch.id, CHANGED_BYTES)
    sqlite_session.commit()

    beta = sqlite_session.execute(
        select(Patient).where(Patient.source_ref == "PAT-PR08-002")
    ).scalar_one()
    # The changed variant has no consent columns at all: existing evidence survives.
    assert beta.consent_flags.get("sms") is True
    # New patients from the same file get no positive consent.
    gamma = sqlite_session.execute(
        select(Patient).where(Patient.source_ref == "PAT-PR08-003")
    ).scalar_one()
    assert gamma.consent_flags.get("sms") is not True


def test_missing_contact_columns_preserve_existing_contact_data(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    sqlite_session.add(
        Patient(
            id="pat-contact-existing",
            clinic_id=clinic_id,
            source_ref="PAT-CONTACT-1",
            name="Test Patient Contact Old",
            phone="+447700900701",
            email="contact-old@clinic-test.invalid",
            consent_flags={},
            opt_out_flags={},
        )
    )
    sqlite_session.commit()
    data = (
        b"appointment_source_ref,patient_source_ref,patient_name,status,start_at\n"
        b"APPT-CONTACT-1,PAT-CONTACT-1,Test Patient Contact New,missed,"
        b"2026-06-20T09:00:00+00:00\n"
    )

    preview = _preview(sqlite_session, clinic_id, data)
    sqlite_session.commit()
    _approve(sqlite_session, clinic_id, preview.batch.id, data)
    sqlite_session.commit()

    patient = sqlite_session.get(Patient, "pat-contact-existing")
    assert patient.name == "Test Patient Contact New"
    assert patient.phone == "+447700900701"
    assert patient.email == "contact-old@clinic-test.invalid"


def test_import_reuses_active_source_link_without_rewriting_primary_ref(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    sqlite_session.add(
        Patient(
            id="pat-linked-existing",
            clinic_id=clinic_id,
            source_ref="CLINIKO-PRIMARY-777",
            name="Test Patient Linked Old",
            consent_flags={},
            opt_out_flags={},
        )
    )
    sqlite_session.flush()
    create_patient_source_link(
        sqlite_session,
        clinic_id,
        "pat-linked-existing",
        provider=SourceSystem.CSV,
        source_ref="PAT-LINKED-CSV-777",
        strategy=MatchStrategy.OPERATOR_RESOLVED,
        evidence_hash="e" * 64,
        actor="operator:test",
        now=NOW,
        keyring=KEYRING,
    )
    sqlite_session.commit()
    data = (
        b"appointment_source_ref,patient_source_ref,patient_name,status,start_at\n"
        b"APPT-LINKED-1,PAT-LINKED-CSV-777,Test Patient Linked New,missed,"
        b"2026-06-20T09:00:00+00:00\n"
    )

    preview = _preview(sqlite_session, clinic_id, data)
    sqlite_session.commit()
    _approve(sqlite_session, clinic_id, preview.batch.id, data)
    sqlite_session.commit()

    assert _count(sqlite_session, Patient) == 1
    patient = sqlite_session.get(Patient, "pat-linked-existing")
    assert patient.source_ref == "CLINIKO-PRIMARY-777"
    assert patient.name == "Test Patient Linked New"
    appointment = sqlite_session.execute(select(Appointment)).scalar_one()
    assert appointment.patient_id == patient.id
    assert _count(sqlite_session, PatientSourceLink) == 1


def test_csv_false_never_clears_existing_opt_out(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    sqlite_session.add(
        Patient(
            id="pat-optout",
            clinic_id=clinic_id,
            source_ref="PAT-PR08-003",
            name="Test Patient Gamma",
            consent_flags={},
            opt_out_flags={"sms": True, "email": True},
        )
    )
    sqlite_session.commit()

    # changed_variant sets opt_out_sms=false and leaves opt_out_email empty for Gamma.
    preview = _preview(sqlite_session, clinic_id, CHANGED_BYTES)
    sqlite_session.commit()
    _approve(sqlite_session, clinic_id, preview.batch.id, CHANGED_BYTES)
    sqlite_session.commit()

    gamma = sqlite_session.execute(
        select(Patient).where(Patient.source_ref == "PAT-PR08-003")
    ).scalar_one()
    assert gamma.opt_out_flags.get("sms") is True  # false in CSV cannot clear
    assert gamma.opt_out_flags.get("email") is True  # missing in CSV cannot clear


def test_optout_true_wins_over_consent_true(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    data = (FIXTURES / "optout_conflict.csv").read_bytes()
    policy = _policy(max_evidence_age=timedelta(days=365))
    preview = _preview(sqlite_session, clinic_id, data, policy=policy)
    sqlite_session.commit()
    _approve(sqlite_session, clinic_id, preview.batch.id, data, policy=policy)
    sqlite_session.commit()

    patient = sqlite_session.execute(
        select(Patient).where(Patient.source_ref == "PAT-PR08-C01")
    ).scalar_one()
    assert patient.opt_out_flags.get("sms") is True
    assert patient.consent_flags.get("sms") is not True


# --------------------------------------------------------------------------- #
# Atomicity
# --------------------------------------------------------------------------- #
def test_transaction_rollback_leaves_no_partial_import(sqlite_session, monkeypatch):
    clinic_id = _add_clinic(sqlite_session)
    preview = _preview(sqlite_session, clinic_id)
    sqlite_session.commit()

    from src.clinic_recall.sync import csv_import as csv_import_module

    def _fail_after_upsert(session, clinic_id_, source, **kwargs):
        upsert_source(session, clinic_id_, source, **kwargs)
        raise RuntimeError("injected mid-import fault")

    monkeypatch.setattr(csv_import_module, "upsert_source", _fail_after_upsert)
    with pytest.raises(RuntimeError, match="injected"):
        _approve(sqlite_session, clinic_id, preview.batch.id, VALID_BYTES)
    sqlite_session.rollback()

    _assert_no_import_side_effects(sqlite_session)
    batch = sqlite_session.get(ImportBatch, preview.batch.id)
    assert batch.state == ImportBatchState.PREVIEW_VALID
    assert batch.completed_at is None


# --------------------------------------------------------------------------- #
# Attestation and metadata binding
# --------------------------------------------------------------------------- #
def test_approval_requires_current_structured_attestation(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    preview = _preview(sqlite_session, clinic_id)
    sqlite_session.commit()

    for bad in (
        _attestation(attestation_version="attest-v0"),
        _attestation(confirm_clinic_authority=False),
        _attestation(source_system=SourceSystem.CLINIKO),
        _attestation(export_at=EXPORT_AT + timedelta(hours=1)),
        _attestation(attested_channels=("fax",)),
    ):
        with pytest.raises(CsvImportError) as excinfo:
            _approve(sqlite_session, clinic_id, preview.batch.id, VALID_BYTES, attestation=bad)
        sqlite_session.rollback()
        assert excinfo.value.reason in {
            "attestation_invalid",
            "source_metadata_mismatch",
        }
    _assert_no_import_side_effects(sqlite_session)


def test_policy_change_between_preview_and_approval_fails_closed(sqlite_session):
    clinic_id = _add_clinic(sqlite_session)
    preview = _preview(sqlite_session, clinic_id, policy=_policy())
    sqlite_session.commit()

    changed_policy = _policy(
        version="test-csv-policy-v2",
        statement_hash="b" * 64,
    )
    with pytest.raises(CsvImportError) as excinfo:
        _approve(
            sqlite_session,
            clinic_id,
            preview.batch.id,
            VALID_BYTES,
            policy=changed_policy,
        )
    sqlite_session.rollback()

    assert excinfo.value.reason == "policy_mismatch"
    _assert_no_import_side_effects(sqlite_session)


def test_cross_tenant_batch_is_not_found(sqlite_session):
    clinic_a = _add_clinic(sqlite_session, "clinic-pr08-a")
    clinic_b = _add_clinic(sqlite_session, "clinic-pr08-b")
    preview = _preview(sqlite_session, clinic_a)
    sqlite_session.commit()

    with pytest.raises(CsvImportError) as excinfo:
        _approve(sqlite_session, clinic_b, preview.batch.id, VALID_BYTES)
    sqlite_session.rollback()
    assert excinfo.value.reason == "batch_not_found"
    for marker in RAW_MARKERS:
        assert marker not in str(excinfo.value)
    _assert_no_import_side_effects(sqlite_session)
