"""Ordinary-role PostgreSQL proofs for PR-06 availability and local claims."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from src.clinic_recall.availability import AvailabilitySlotInput, upsert_availability_slots
from src.clinic_recall.booking import (
    book_inbound_slot as _book_inbound_slot,
)
from src.clinic_recall.booking import (
    book_slot as _book_slot,
)
from src.clinic_recall.db import clinic_scope
from src.clinic_recall.enums import (
    AppointmentStatus,
    AuditAction,
    CampaignStatus,
    CampaignType,
    Channel,
    OutreachState,
)
from src.clinic_recall.models import (
    Appointment,
    AuditLog,
    Base,
    BookingAction,
    Campaign,
    Clinic,
    OutreachJob,
    Patient,
)
from src.clinic_recall.rights import (
    RightsPolicy,
    SubjectKey,
    SubjectKeyring,
    request_patient_erasure,
)
from src.clinic_recall.rls import apply_rls_policies

from tests.identity_evidence_support import grant_synthetic_t2

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
_IDENTITY_SUFFIX = count(1)
MIGRATION = "0020_availability_booking_state"
AVAILABILITY_COLUMNS = {
    "source_provider",
    "business_id",
    "appointment_type_id",
    "fetched_at",
    "expires_at",
}
BOOKING_COLUMNS = {
    "write_back_state",
    "external_appointment_ref",
    "request_hash",
    "provider_attempted_at",
    "read_back_verified_at",
    "conflict_reason",
}


def _with_synthetic_t2(session, clinic_id: str, kwargs: dict, channel: Channel):
    service, context = grant_synthetic_t2(
        session,
        clinic_id=clinic_id,
        patient_id=str(kwargs["patient_id"]),
        channel=channel,
        now=kwargs["now"],
        suffix=f"availability-postgres-{next(_IDENTITY_SUFFIX)}",
    )
    return {
        **kwargs,
        "identity_service": service,
        "identity_context": context,
    }


def book_slot(session, clinic_id: str, **kwargs):
    return _book_slot(
        session,
        clinic_id,
        **_with_synthetic_t2(session, clinic_id, kwargs, Channel.CALL),
    )


def book_inbound_slot(session, clinic_id: str, **kwargs):
    return _book_inbound_slot(
        session,
        clinic_id,
        **_with_synthetic_t2(session, clinic_id, kwargs, Channel.SMS),
    )


def _reset_schema(engine) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(sa.text("DROP SCHEMA public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))


def _create_current_schema(engine) -> None:
    _reset_schema(engine)
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        apply_rls_policies(connection)


def _strip_0020_shape(engine) -> None:
    statements = (
        "ALTER TABLE booking_action DROP CONSTRAINT IF EXISTS ck_booking_action_verified_write_back",
        "ALTER TABLE booking_action DROP CONSTRAINT IF EXISTS ck_booking_action_conflict_reason_length",
        "ALTER TABLE booking_action DROP CONSTRAINT IF EXISTS ck_booking_action_request_hash_length",
        "ALTER TABLE availability_slot DROP CONSTRAINT IF EXISTS ck_availability_slot_authoritative_observation",
        "DROP INDEX IF EXISTS ix_availability_slot_clinic_fresh",
        "ALTER TABLE booking_action DROP COLUMN IF EXISTS conflict_reason",
        "ALTER TABLE booking_action DROP COLUMN IF EXISTS read_back_verified_at",
        "ALTER TABLE booking_action DROP COLUMN IF EXISTS provider_attempted_at",
        "ALTER TABLE booking_action DROP COLUMN IF EXISTS request_hash",
        "ALTER TABLE booking_action DROP COLUMN IF EXISTS external_appointment_ref",
        "ALTER TABLE booking_action DROP COLUMN IF EXISTS write_back_state",
        "ALTER TABLE availability_slot DROP COLUMN IF EXISTS expires_at",
        "ALTER TABLE availability_slot DROP COLUMN IF EXISTS fetched_at",
        "ALTER TABLE availability_slot DROP COLUMN IF EXISTS appointment_type_id",
        "ALTER TABLE availability_slot DROP COLUMN IF EXISTS business_id",
        "ALTER TABLE availability_slot DROP COLUMN IF EXISTS source_provider",
        "DROP TYPE IF EXISTS booking_write_back_state",
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(sa.text(statement))


def _seed_legacy_booking(engine, *, written_back: bool) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text("SELECT set_config('app.clinic_id', 'clinic-legacy', true)")
        )
        connection.execute(
            sa.text("INSERT INTO clinic (id, name) VALUES ('clinic-legacy', 'Legacy')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO patient (id, clinic_id, source_ref, name, "
                "consent_flags, opt_out_flags) VALUES "
                "('patient-legacy', 'clinic-legacy', 'P-legacy', 'Synthetic', "
                "'{}'::json, '{}'::json)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO appointment (id, clinic_id, patient_id, source_ref, "
                "status, start_at) VALUES "
                "('appointment-legacy', 'clinic-legacy', 'patient-legacy', "
                "'A-legacy', 'scheduled', '2026-08-01T09:00:00Z')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO availability_slot (id, clinic_id, source_ref, "
                "clinician_id, start_at, end_at, details) VALUES "
                "('slot-legacy', 'clinic-legacy', 'legacy-source', 'legacy-clinician', "
                "'2026-08-01T09:00:00Z', '2026-08-01T09:30:00Z', '{}'::json)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO booking_action (id, clinic_id, appointment_id, "
                "availability_slot_id, type, status, written_back) VALUES "
                "('booking-legacy', 'clinic-legacy', 'appointment-legacy', "
                "'slot-legacy', 'book', 'completed', :written_back)"
            ),
            {"written_back": written_back},
        )


def _seed_claim_clinic(
    engine,
    clinic_id: str,
    *,
    patient_count: int,
    with_slot: bool = True,
) -> str | None:
    with Session(engine, expire_on_commit=False) as session:
        session.add(Clinic(id=clinic_id, name=f"{clinic_id} Synthetic Clinic"))
        session.flush()
        with clinic_scope(session, clinic_id):
            session.add(
                Campaign(
                    id=f"campaign-{clinic_id}",
                    clinic_id=clinic_id,
                    type=CampaignType.RECOVERY,
                    status=CampaignStatus.ACTIVE,
                )
            )
            session.flush()
            for index in range(patient_count):
                patient_id = f"patient-{clinic_id}-{index}"
                appointment_id = f"appointment-{clinic_id}-{index}"
                session.add(
                    Patient(
                        id=patient_id,
                        clinic_id=clinic_id,
                        source_ref=f"P-{clinic_id}-{index}",
                        name=f"Synthetic Patient {index}",
                        consent_flags={"call": True, "sms": True},
                        opt_out_flags={},
                    )
                )
                session.flush()
                session.add(
                    Appointment(
                        id=appointment_id,
                        clinic_id=clinic_id,
                        patient_id=patient_id,
                        source_ref=f"A-{clinic_id}-{index}",
                        status=AppointmentStatus.MISSED,
                        start_at=NOW - timedelta(days=7),
                    )
                )
                session.flush()
                session.add(
                    OutreachJob(
                        id=f"job-{clinic_id}-{index}",
                        clinic_id=clinic_id,
                        campaign_id=f"campaign-{clinic_id}",
                        patient_id=patient_id,
                        appointment_id=appointment_id,
                        channel=Channel.CALL,
                        state=OutreachState.NO_REPLY,
                    )
                )
                session.flush()
            slot_id = None
            if with_slot:
                slot_id = upsert_availability_slots(
                    session,
                    clinic_id,
                    [
                        AvailabilitySlotInput(
                            source_ref=f"cliniko:v1:{clinic_id}",
                            source_provider="cliniko",
                            business_id="920600001",
                            clinician_id="930600001",
                            appointment_type_id="940600001",
                            start_at=NOW + timedelta(days=1),
                            end_at=NOW + timedelta(days=1, minutes=30),
                            fetched_at=NOW,
                            expires_at=NOW + timedelta(minutes=10),
                        )
                    ],
                    now=NOW,
                )[0].slot_id
        session.commit()
        return slot_id


def test_postgres_0019_to_0020_round_trip_forced_rls_and_direct_guards(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_schema(engine)
    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        engine.url.render_as_string(hide_password=False),
    )
    config = Config("infra/postgres/alembic.ini")
    command.upgrade(config, "0019_rights_retention_purge")
    _strip_0020_shape(engine)
    _seed_legacy_booking(engine, written_back=False)

    command.upgrade(config, MIGRATION)
    inspector = sa.inspect(engine)
    assert AVAILABILITY_COLUMNS <= {
        column["name"] for column in inspector.get_columns("availability_slot")
    }
    assert BOOKING_COLUMNS <= {
        column["name"] for column in inspector.get_columns("booking_action")
    }
    with engine.begin() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname IN ('availability_slot', 'booking_action') "
                "ORDER BY relname"
            )
        ).all()
        connection.execute(
            sa.text("SELECT set_config('app.clinic_id', 'clinic-legacy', true)")
        )
        legacy = connection.execute(
            sa.text(
                "SELECT write_back_state, written_back, request_hash, "
                "read_back_verified_at FROM booking_action"
            )
        ).one()
    assert rows == [
        ("availability_slot", True, True),
        ("booking_action", True, True),
    ]
    assert tuple(legacy) == ("not_attempted", False, None, None)

    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                sa.text("SELECT set_config('app.clinic_id', 'clinic-legacy', true)")
            )
            connection.execute(
                sa.text("UPDATE booking_action SET written_back = true")
            )

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE alembic_version SET version_num = "
                "'0019_rights_retention_purge'"
            )
        )
    command.upgrade(config, MIGRATION)
    command.downgrade(config, "0019_rights_retention_purge")
    inspector = sa.inspect(engine)
    assert AVAILABILITY_COLUMNS.isdisjoint(
        {column["name"] for column in inspector.get_columns("availability_slot")}
    )
    assert BOOKING_COLUMNS.isdisjoint(
        {column["name"] for column in inspector.get_columns("booking_action")}
    )
    command.upgrade(config, MIGRATION)


def test_postgres_0020_fails_closed_on_rls_hidden_legacy_written_back(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_schema(engine)
    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        engine.url.render_as_string(hide_password=False),
    )
    config = Config("infra/postgres/alembic.ini")
    command.upgrade(config, "0019_rights_retention_purge")
    _strip_0020_shape(engine)
    _seed_legacy_booking(engine, written_back=True)

    with pytest.raises(RuntimeError, match="legacy written_back rows require review"):
        command.upgrade(config, MIGRATION)

    with engine.connect() as connection:
        version = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert version == "0019_rights_retention_purge"


def test_postgres_0020_downgrade_rejects_provider_state_hidden_by_rls(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_schema(engine)
    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        engine.url.render_as_string(hide_password=False),
    )
    config = Config("infra/postgres/alembic.ini")
    command.upgrade(config, MIGRATION)
    _seed_legacy_booking(engine, written_back=False)
    with engine.begin() as connection:
        connection.execute(
            sa.text("SELECT set_config('app.clinic_id', 'clinic-legacy', true)")
        )
        connection.execute(
            sa.text(
                "UPDATE booking_action SET write_back_state = 'verified', "
                "written_back = true, external_appointment_ref = 'synthetic-ref', "
                "provider_attempted_at = :now, read_back_verified_at = :now"
            ),
            {"now": NOW},
        )

    with pytest.raises(RuntimeError, match="disable provider write-back state"):
        command.downgrade(config, "0019_rights_retention_purge")

    with engine.connect() as connection:
        version = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert version == MIGRATION


def test_postgres_concurrent_claim_is_exactly_once_idempotent_and_tenant_scoped(
    clinic_recall_pg_engine,
) -> None:
    engine = clinic_recall_pg_engine
    _create_current_schema(engine)
    clinic_id = "clinic-pr06-race"
    slot_id = _seed_claim_clinic(engine, clinic_id, patient_count=2)
    assert slot_id is not None
    _seed_claim_clinic(engine, "clinic-pr06-other", patient_count=1, with_slot=False)
    start = threading.Barrier(2)

    def claim(index: int):
        with Session(engine, expire_on_commit=False) as session:
            start.wait()
            result = book_slot(
                session,
                clinic_id,
                patient_id=f"patient-{clinic_id}-{index}",
                outreach_job_id=f"job-{clinic_id}-{index}",
                slot_id=slot_id,
                now=NOW,
            )
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (0, 1)))

    winners = [index for index, result in enumerate(results) if result.success]
    losers = [result for result in results if not result.success]
    assert len(winners) == 1
    assert [result.error for result in losers] == ["slot_already_booked"]
    winner = winners[0]

    with Session(engine, expire_on_commit=False) as session:
        repeated = book_slot(
            session,
            clinic_id,
            patient_id=f"patient-{clinic_id}-{winner}",
            outreach_job_id=f"job-{clinic_id}-{winner}",
            slot_id=slot_id,
            now=NOW,
        )
        session.commit()
    assert repeated.success is True
    assert repeated.idempotent is True

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, clinic_id):
            action_count = session.scalar(
                sa.select(sa.func.count()).select_from(BookingAction)
            )
            audit_count = session.scalar(
                sa.select(sa.func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == AuditAction.BOOK_APPOINTMENT)
            )
        session.commit()
    assert action_count == 1
    assert audit_count == 1

    other_clinic = "clinic-pr06-other"
    with Session(engine, expire_on_commit=False) as session:
        with pytest.raises(LookupError):
            book_slot(
                session,
                other_clinic,
                patient_id=f"patient-{other_clinic}-0",
                outreach_job_id=f"job-{other_clinic}-0",
                slot_id=slot_id,
                now=NOW,
            )
        session.rollback()
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, other_clinic):
            rows = session.execute(
                sa.text("SELECT id FROM availability_slot WHERE id = :slot_id"),
                {"slot_id": slot_id},
            ).all()
        session.commit()
    assert rows == []


def test_postgres_rolled_back_claim_releases_slot_without_orphans(
    clinic_recall_pg_engine,
) -> None:
    engine = clinic_recall_pg_engine
    _create_current_schema(engine)
    clinic_id = "clinic-pr06-rollback"
    slot_id = _seed_claim_clinic(engine, clinic_id, patient_count=1)
    assert slot_id is not None

    with Session(engine, expire_on_commit=False) as session:
        first = book_slot(
            session,
            clinic_id,
            patient_id=f"patient-{clinic_id}-0",
            outreach_job_id=f"job-{clinic_id}-0",
            slot_id=slot_id,
            now=NOW,
        )
        assert first.success is True
        session.rollback()

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, clinic_id):
            assert session.scalar(
                sa.select(sa.func.count()).select_from(BookingAction)
            ) == 0
            assert session.scalar(
                sa.select(sa.func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == AuditAction.BOOK_APPOINTMENT)
            ) == 0
        session.commit()

    with Session(engine, expire_on_commit=False) as session:
        retried = book_slot(
            session,
            clinic_id,
            patient_id=f"patient-{clinic_id}-0",
            outreach_job_id=f"job-{clinic_id}-0",
            slot_id=slot_id,
            now=NOW,
        )
        session.commit()
    assert retried.success is True
    assert retried.idempotent is False


def test_postgres_frozen_subject_cannot_create_or_resurrect_booking_action(
    clinic_recall_pg_engine,
) -> None:
    engine = clinic_recall_pg_engine
    _create_current_schema(engine)
    clinic_id = "clinic-pr06-frozen"
    patient_id = f"patient-{clinic_id}-0"
    appointment_id = f"appointment-{clinic_id}-0"
    slot_id = _seed_claim_clinic(engine, clinic_id, patient_count=1)
    assert slot_id is not None

    with Session(engine, expire_on_commit=False) as session:
        request_patient_erasure(
            session,
            clinic_id=clinic_id,
            patient_id=patient_id,
            confirm_token=f"ERASE {patient_id}",
            request_identity="tests-pr06-postgres-freeze",
            actor_role="dpo",
            actor_reference="tests-pr06-postgres-operator",
            keyring=SubjectKeyring(
                current=SubjectKey("tests-pr06-v1", b"tests-pr06-postgres-key")
            ),
            policy=RightsPolicy(
                "tests-pr06-policy-v1",
                "a" * 64,
                timedelta(days=28),
            ),
            now=NOW,
        )
        outbound = book_slot(
            session,
            clinic_id,
            patient_id=patient_id,
            outreach_job_id=f"job-{clinic_id}-0",
            slot_id=slot_id,
            now=NOW,
        )
        inbound = book_inbound_slot(
            session,
            clinic_id,
            patient_id=patient_id,
            appointment_id=appointment_id,
            slot_id=slot_id,
            now=NOW,
            action_type="reschedule",
        )
        session.commit()

    assert outbound.error == "subject_frozen"
    assert inbound.error == "subject_frozen"
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, clinic_id):
            count = session.scalar(
                sa.select(sa.func.count()).select_from(BookingAction)
            )
        session.commit()
    assert count == 0