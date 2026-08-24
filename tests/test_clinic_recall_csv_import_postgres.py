"""Ordinary-role PostgreSQL proof for PR-08 controlled CSV import.

Runs only with ``CLINIC_RECALL_TEST_DSN`` pointing at a disposable database
owned by a NOSUPERUSER/NOBYPASSRLS role, so FORCE ROW LEVEL SECURITY applies
to every statement the suite issues.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from src.clinic_recall.db import clinic_scope
from src.clinic_recall.enums import (
    ImportBatchState,
    MatchStrategy,
    SourceSystem,
)
from src.clinic_recall.models import (
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
    RightsAliasTombstone,
)
from src.clinic_recall.rights import (
    RightsPolicy,
    SubjectFrozenError,
    SubjectKey,
    SubjectKeyring,
    assert_source_writable,
    request_patient_erasure,
)
from src.clinic_recall.sync import CsvSyncSource
from src.clinic_recall.sync.csv_consent import CsvImportPolicy
from src.clinic_recall.sync.csv_import import (
    CsvImportAttestation,
    CsvImportError,
    approve_csv_import,
    get_import_batch,
    preview_csv_import,
)
from src.clinic_recall.sync.csv_matching import create_patient_source_link

pytestmark = pytest.mark.postgres

FIXTURES = Path(__file__).parent / "fixtures" / "csv" / "pr08"
VALID_BYTES = (FIXTURES / "valid_multi.csv").read_bytes()

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
EXPORT_AT = datetime(2026, 7, 25, 18, 0, tzinfo=UTC)
CLINIC_A = "clinic-pr08-pg-a"
CLINIC_B = "clinic-pr08-pg-b"

KEY_V1 = SubjectKey(version="tests-pg-v1", secret=b"tests-postgres-pr08-key-01")
KEY_V2 = SubjectKey(version="tests-pg-v2", secret=b"tests-postgres-pr08-key-02")
KEYRING = SubjectKeyring(current=KEY_V1)
ROTATED = SubjectKeyring(current=KEY_V2, previous=(KEY_V1,))

RIGHTS_POLICY = RightsPolicy(
    version="tests-pg-rights-v1",
    approval_evidence_hash="c" * 64,
    request_due_after=timedelta(days=28),
)

POLICY = CsvImportPolicy(
    version="tests-pg-csv-policy-v1",
    statement_hash="a" * 64,
    attestation_versions=("attest-v1",),
    channels=("sms", "email", "call"),
    max_evidence_age=None,
    preview_ttl=timedelta(minutes=30),
    allowed_source_systems=(SourceSystem.CSV,),
)

ATTESTATION = CsvImportAttestation(
    source_system=SourceSystem.CSV,
    export_at=EXPORT_AT,
    attestation_version="attest-v1",
    attested_channels=("sms",),
    confirm_clinic_authority=True,
)

_PR08_TABLES = (
    "import_batch",
    "patient_source_link",
    "import_match_review",
    "rights_alias_tombstone",
)


def _reset_to_pr08_head(engine, monkeypatch) -> None:
    """Rebuild the schema and exercise the published-0020 -> 0021 create path."""
    # Repeated broad-suite resets recreate native enum OIDs. Dispose pooled
    # psycopg connections so auto-prepared statements never retain old OIDs.
    engine.dispose()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(sa.text("DROP SCHEMA public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))
    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        engine.url.render_as_string(hide_password=False),
    )
    config = Config("infra/postgres/alembic.ini")
    command.upgrade(config, "0020_availability_booking_state")
    # 0001's create_all made the PR-08 tables from live models; drop them to
    # emulate the exact published-0020 database, then run 0021's create path.
    with engine.begin() as connection:
        for table in _PR08_TABLES:
            connection.execute(sa.text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        for enum_name in (
            "match_strategy",
            "import_match_review_state",
            "source_link_state",
            "import_batch_state",
            "source_system",
        ):
            connection.execute(sa.text(f"DROP TYPE IF EXISTS {enum_name}"))
    command.upgrade(config, "0021_controlled_csv_import")
    engine.dispose()


def _seed_clinics(engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        for clinic_id in (CLINIC_A, CLINIC_B):
            with clinic_scope(session, clinic_id):
                session.add(Clinic(id=clinic_id, name=f"{clinic_id} Clinic"))
                session.flush()
        session.commit()


def _preview(session, clinic_id, data=VALID_BYTES):
    return preview_csv_import(
        session,
        clinic_id,
        materialization=CsvSyncSource.materialize(data),
        source_system=SourceSystem.CSV,
        export_at=EXPORT_AT,
        actor="staff:pg-test",
        now=NOW,
        policy=POLICY,
        upload_disposed_at=NOW,
    )


def _approve(session, clinic_id, batch_id, data=VALID_BYTES, *, keyring=KEYRING, now=LATER):
    return approve_csv_import(
        session,
        clinic_id,
        batch_id,
        materialization=CsvSyncSource.materialize(data),
        attestation=ATTESTATION,
        actor="staff:pg-test",
        now=now,
        policy=POLICY,
        keyring=keyring,
        upload_disposed_at=now,
    )


def test_postgres_0021_forces_rls_and_refuses_stateful_downgrade(
    clinic_recall_pg_engine, monkeypatch
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_pr08_head(engine, monkeypatch)

    with engine.connect() as connection:
        for table in _PR08_TABLES:
            enabled, forced = connection.execute(
                sa.text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = :table"
                ),
                {"table": table},
            ).one()
            assert enabled is True, table
            assert forced is True, table
            policy_count = connection.execute(
                sa.text("SELECT count(*) FROM pg_policies WHERE tablename = :table"),
                {"table": table},
            ).scalar_one()
            assert policy_count == 1, table
        audit_values = {
            row[0]
            for row in connection.execute(
                sa.text(
                    "SELECT enumlabel FROM pg_enum JOIN pg_type "
                    "ON pg_enum.enumtypid = pg_type.oid "
                    "WHERE pg_type.typname = 'audit_action'"
                )
            )
        }
        assert {"csv_import_preview", "csv_import_approve", "csv_import_match"} <= audit_values

    # A completed import refuses schema downgrade.
    _seed_clinics(engine)
    with Session(engine, expire_on_commit=False) as session:
        preview = _preview(session, CLINIC_A)
        session.commit()
        _approve(session, CLINIC_A, preview.batch.id)
        session.commit()
    config = Config("infra/postgres/alembic.ini")
    with pytest.raises(RuntimeError, match="roll back by disabling CSV import"):
        command.downgrade(config, "0020_availability_booking_state")


def test_postgres_rls_denies_cross_tenant_reads_writes_and_composite_fk(
    clinic_recall_pg_engine, monkeypatch
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_pr08_head(engine, monkeypatch)
    _seed_clinics(engine)

    with Session(engine, expire_on_commit=False) as session:
        preview = _preview(session, CLINIC_A)
        session.commit()
        _approve(session, CLINIC_A, preview.batch.id)
        session.commit()
        batch_id = preview.batch.id
        with clinic_scope(session, CLINIC_A):
            patient_a = session.execute(
                sa.select(Patient).where(Patient.source_ref == "PAT-PR08-001")
            ).scalar_one()
            cliniko_link = create_patient_source_link(
                session,
                CLINIC_A,
                patient_a.id,
                provider=SourceSystem.CLINIKO,
                source_ref="CLK-PG-1",
                strategy=MatchStrategy.OPERATOR_RESOLVED,
                evidence_hash="e" * 64,
                actor="operator:pg-test",
                now=LATER,
                keyring=KEYRING,
                import_batch_id=batch_id,
            )
            cliniko_link_id = cliniko_link.id
        session.commit()

    # Tenant B sees nothing and the service refuses the foreign batch.
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_B):
            for model in (ImportBatch, PatientSourceLink, ImportMatchReview):
                visible = session.execute(
                    sa.select(sa.func.count()).select_from(model)
                ).scalar_one()
                assert visible == 0, model.__tablename__
        assert get_import_batch(session, CLINIC_B, batch_id) is None
        with pytest.raises(CsvImportError) as denied:
            _approve(session, CLINIC_B, batch_id)
        session.rollback()
        assert denied.value.reason == "batch_not_found"

    # Direct SQL under tenant B's scope cannot write tenant A rows (WITH CHECK).
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_B):
            with pytest.raises(DBAPIError):
                session.execute(
                    sa.text(
                        "INSERT INTO import_batch ("
                        "id, clinic_id, state, file_sha256, validation_summary_sha256, "
                        "schema_version, source_system, export_at, preview_requested_at, "
                        "preview_actor, preview_expires_at, preview_upload_disposed_at"
                        ") VALUES ("
                        "'impb-cross', :clinic_a, 'preview_valid', :digest, :digest, "
                        "'wulo-csv-v1', 'csv', :now, :now, 'staff:pg', :later, :now"
                        ")"
                    ),
                    {
                        "clinic_a": CLINIC_A,
                        "digest": "d" * 64,
                        "now": NOW,
                        "later": NOW + timedelta(minutes=30),
                    },
                )
            session.rollback()

    # A review in tenant B cannot reference tenant A's source link.
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_B):
            session.add(
                Patient(
                    id="patient-pr08-pg-b",
                    clinic_id=CLINIC_B,
                    source_ref="PAT-PG-B",
                    name="Test Patient PG B",
                    consent_flags={},
                    opt_out_flags={},
                )
            )
            preview_b = _preview(session, CLINIC_B)
            session.flush()
            with pytest.raises((IntegrityError, DBAPIError)):
                session.execute(
                    sa.text(
                        "INSERT INTO import_match_review ("
                        "id, clinic_id, import_batch_id, patient_id, provider, "
                        "strategy, strategy_version, state, candidate_count, source_link_id, "
                        "resolved_by, resolved_at"
                        ") VALUES ("
                        "'imr-cross-link', :clinic_b, :batch_b, 'patient-pr08-pg-b', "
                        "'cliniko', 'operator_resolved', 'v1', 'linked', 1, :link_a, "
                        "'operator:pg', :now"
                        ")"
                    ),
                    {
                        "clinic_b": CLINIC_B,
                        "batch_b": preview_b.batch.id,
                        "link_a": cliniko_link_id,
                        "now": NOW,
                    },
                )
            session.rollback()

    # A composite FK cannot bind a link to another tenant's patient.
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            patient_a_id = session.execute(
                sa.select(Patient.id).where(Patient.source_ref == "PAT-PR08-001")
            ).scalar_one()
        with clinic_scope(session, CLINIC_B):
            with pytest.raises((IntegrityError, DBAPIError)):
                session.execute(
                    sa.text(
                        "INSERT INTO patient_source_link ("
                        "id, clinic_id, patient_id, provider, source_ref, state, "
                        "strategy, strategy_version, evidence_hash, resolved_by, resolved_at"
                        ") VALUES ("
                        "'pslink-cross', :clinic_b, :patient_a, 'cliniko', 'CLK-CROSS', "
                        "'active', 'operator_resolved', 'v1', :digest, 'operator:pg', :now"
                        ")"
                    ),
                    {
                        "clinic_b": CLINIC_B,
                        "patient_a": patient_a_id,
                        "digest": "e" * 64,
                        "now": NOW,
                    },
                )
            session.rollback()


def test_postgres_concurrent_identical_approvals_complete_once(
    clinic_recall_pg_engine, monkeypatch
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_pr08_head(engine, monkeypatch)
    _seed_clinics(engine)

    with Session(engine, expire_on_commit=False) as session:
        preview = _preview(session, CLINIC_A)
        session.commit()
        batch_id = preview.batch.id

    barrier = threading.Barrier(2)
    outcomes: list[tuple[bool, str | None]] = []
    lock = threading.Lock()

    def _worker() -> None:
        with Session(engine, expire_on_commit=False) as session:
            barrier.wait(timeout=10)
            try:
                result = _approve(session, CLINIC_A, batch_id)
                session.commit()
                with lock:
                    outcomes.append((result.replayed, None))
            except Exception as exc:  # pragma: no cover - failure evidence
                session.rollback()
                with lock:
                    outcomes.append((False, repr(exc)))

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(replayed for replayed, _ in outcomes) == [False, True], outcomes
    assert all(error is None for _, error in outcomes), outcomes

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            assert (
                session.execute(sa.select(sa.func.count()).select_from(Patient)).scalar_one() == 3
            )
            approve_audits = session.execute(
                sa.select(sa.func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "csv_import_approve")
            ).scalar_one()
            assert approve_audits == 1
            batch = session.get(ImportBatch, batch_id)
            assert batch.state == ImportBatchState.COMPLETED


def test_postgres_concurrent_previews_of_same_bytes_share_one_live_batch(
    clinic_recall_pg_engine, monkeypatch
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_pr08_head(engine, monkeypatch)
    _seed_clinics(engine)

    barrier = threading.Barrier(2)
    batch_ids: list[str] = []
    lock = threading.Lock()

    def _worker() -> None:
        with Session(engine, expire_on_commit=False) as session:
            barrier.wait(timeout=10)
            result = _preview(session, CLINIC_A)
            session.commit()
            with lock:
                batch_ids.append(result.batch.id)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(batch_ids) == 2
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            live = session.execute(
                sa.select(sa.func.count())
                .select_from(ImportBatch)
                .where(ImportBatch.state == ImportBatchState.PREVIEW_VALID)
            ).scalar_one()
            assert live == 1


def test_postgres_partial_import_fault_rolls_back_completely(
    clinic_recall_pg_engine, monkeypatch
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_pr08_head(engine, monkeypatch)
    _seed_clinics(engine)

    from src.clinic_recall.sync import csv_import as csv_import_module
    from src.clinic_recall.sync import upsert_source as real_upsert

    with Session(engine, expire_on_commit=False) as session:
        preview = _preview(session, CLINIC_A)
        session.commit()
        batch_id = preview.batch.id

        def _fail_after_upsert(inner_session, clinic_id, source, **kwargs):
            real_upsert(inner_session, clinic_id, source, **kwargs)
            raise RuntimeError("injected postgres mid-import fault")

        monkeypatch.setattr(csv_import_module, "upsert_source", _fail_after_upsert)
        with pytest.raises(RuntimeError, match="injected"):
            _approve(session, CLINIC_A, batch_id)
        session.rollback()

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            assert (
                session.execute(sa.select(sa.func.count()).select_from(Patient)).scalar_one() == 0
            )
            batch = session.get(ImportBatch, batch_id)
            assert batch.state == ImportBatchState.PREVIEW_VALID
            assert batch.completed_at is None


def test_postgres_alias_erasure_blocks_rehydration_across_key_rotation(
    clinic_recall_pg_engine, monkeypatch
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_pr08_head(engine, monkeypatch)
    _seed_clinics(engine)

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            session.add(
                Patient(
                    id="pat-pg-erase",
                    clinic_id=CLINIC_A,
                    source_ref="PAT-PR08-001",
                    name="Test Patient PG Erase",
                    consent_flags={},
                    opt_out_flags={},
                )
            )
            session.flush()
            create_patient_source_link(
                session,
                CLINIC_A,
                "pat-pg-erase",
                provider=SourceSystem.CLINIKO,
                source_ref="CLK-PG-ERASED",
                strategy=MatchStrategy.OPERATOR_RESOLVED,
                evidence_hash="e" * 64,
                actor="operator:pg-test",
                now=NOW,
                keyring=KEYRING,
            )
            request_patient_erasure(
                session,
                clinic_id=CLINIC_A,
                patient_id="pat-pg-erase",
                confirm_token="ERASE pat-pg-erase",
                request_identity="tests:pg-erase",
                actor_role="staff",
                actor_reference="staff:pg-test",
                keyring=KEYRING,
                policy=RIGHTS_POLICY,
                now=NOW,
            )
        session.commit()

    with Session(engine, expire_on_commit=False) as session:
        # Both identities are blocked, under current and rotated keys.
        for keyring in (KEYRING, ROTATED):
            for ref in ("PAT-PR08-001", "CLK-PG-ERASED"):
                with pytest.raises(SubjectFrozenError):
                    assert_source_writable(session, CLINIC_A, ref, keyring)
                session.rollback()
        # A CSV import touching the erased ref imports zero rows.
        preview = _preview(session, CLINIC_A)
        session.commit()
        with pytest.raises(CsvImportError) as excinfo:
            _approve(session, CLINIC_A, preview.batch.id, keyring=ROTATED)
        session.rollback()
        assert excinfo.value.reason == "subject_frozen"
        with clinic_scope(session, CLINIC_A):
            tombstones = session.execute(
                sa.select(sa.func.count()).select_from(RightsAliasTombstone)
            ).scalar_one()
            assert tombstones == 1


def test_postgres_import_grants_no_outreach_or_booking_authority(
    clinic_recall_pg_engine, monkeypatch
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_pr08_head(engine, monkeypatch)
    _seed_clinics(engine)

    with Session(engine, expire_on_commit=False) as session:
        preview = _preview(session, CLINIC_A)
        session.commit()
        _approve(session, CLINIC_A, preview.batch.id)
        session.commit()
        with clinic_scope(session, CLINIC_A):
            for model in (
                Campaign,
                OutreachJob,
                ExternalEffect,
                BookingAction,
                Interaction,
                PilotParticipant,
            ):
                count = session.execute(sa.select(sa.func.count()).select_from(model)).scalar_one()
                assert count == 0, model.__tablename__
