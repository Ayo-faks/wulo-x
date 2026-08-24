"""Ordinary-role PostgreSQL proof for PR-13 pilot controls."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from src.clinic_recall.db import clinic_scope
from src.clinic_recall.models import Clinic, Patient, PilotParticipant, PilotProgramme
from src.clinic_recall.pilot_controls import (
    create_programme,
    enroll_participant,
    mark_programme_dark,
    release_cumulative_limit,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
CLINIC_A = "clinic-pilot-pg-a"
CLINIC_B = "clinic-pilot-pg-b"
PROGRAMME_ID = "pilot-pg-a"


def _reset_migrated_schema(engine, monkeypatch) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(sa.text("DROP SCHEMA public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))
    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        engine.url.render_as_string(hide_password=False),
    )
    command.upgrade(Config("infra/postgres/alembic.ini"), "0017_pilot_programme_controls")


def _seed_two_clinics(engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        session.add(Clinic(id=CLINIC_A, name="Pilot PostgreSQL A"))
        session.add(Clinic(id=CLINIC_B, name="Pilot PostgreSQL B"))
        session.commit()
        with clinic_scope(session, CLINIC_A):
            session.add(
                Patient(
                    id="patient-pilot-pg-a-1",
                    clinic_id=CLINIC_A,
                    source_ref="patient-pilot-pg-a-1",
                    name="Synthetic Pilot A1",
                )
            )
            session.add(
                Patient(
                    id="patient-pilot-pg-a-2",
                    clinic_id=CLINIC_A,
                    source_ref="patient-pilot-pg-a-2",
                    name="Synthetic Pilot A2",
                )
            )
        session.commit()
        with clinic_scope(session, CLINIC_B):
            session.add(
                Patient(
                    id="patient-pilot-pg-b-1",
                    clinic_id=CLINIC_B,
                    source_ref="patient-pilot-pg-b-1",
                    name="Synthetic Pilot B1",
                )
            )
        session.commit()
        create_programme(
            session,
            clinic_id=CLINIC_A,
            programme_id=PROGRAMME_ID,
            environment="production",
            release_identity="release-pg-r1",
        )
        session.commit()


def test_postgres_migration_upgrades_real_0016_schema(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(sa.text("DROP SCHEMA public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))
    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        engine.url.render_as_string(hide_password=False),
    )
    config = Config("infra/postgres/alembic.ini")
    command.upgrade(config, "0016_scheduled_cadence")
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE IF EXISTS pilot_participant CASCADE"))
        connection.execute(sa.text("DROP TABLE IF EXISTS pilot_programme CASCADE"))
    command.upgrade(config, "0017_pilot_programme_controls")

    with engine.connect() as connection:
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        tables = set(sa.inspect(connection).get_table_names())
        patient_unique = {
            constraint["name"]
            for constraint in sa.inspect(connection).get_unique_constraints("patient")
        }
    assert revision == "0017_pilot_programme_controls"
    assert {"pilot_programme", "pilot_participant"} <= tables
    assert "uq_patient_clinic_id_id" in patient_unique


def test_postgres_migration_recreates_preexisting_triggers(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_migrated_schema(engine, monkeypatch)
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE alembic_version " "SET version_num = '0016_scheduled_cadence'")
        )

    command.upgrade(Config("infra/postgres/alembic.ini"), "0017_pilot_programme_controls")

    with engine.connect() as connection:
        trigger_rows = connection.execute(
            sa.text(
                "SELECT tgname, count(*) "
                "FROM pg_trigger "
                "WHERE NOT tgisinternal AND tgname IN ("
                "'pilot_programme_transition_guard', "
                "'pilot_participant_insert_guard', "
                "'pilot_participant_identity_guard', "
                "'pilot_participant_patient_erasure'"
                ") GROUP BY tgname"
            )
        ).all()
        triggers = {str(row[0]): int(row[1]) for row in trigger_rows}
    assert triggers == {
        "pilot_programme_transition_guard": 1,
        "pilot_participant_insert_guard": 1,
        "pilot_participant_identity_guard": 1,
        "pilot_participant_patient_erasure": 1,
    }


def test_postgres_service_releases_complete_dark_wave_one(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_migrated_schema(engine, monkeypatch)
    with Session(engine, expire_on_commit=False) as session:
        session.add(Clinic(id=CLINIC_A, name="Pilot PostgreSQL A"))
        session.commit()
        with clinic_scope(session, CLINIC_A):
            for ordinal in range(1, 6):
                session.add(
                    Patient(
                        id=f"patient-wave-one-{ordinal}",
                        clinic_id=CLINIC_A,
                        source_ref=f"patient-wave-one-{ordinal}",
                        name=f"Synthetic Wave One {ordinal}",
                    )
                )
        session.commit()
        programme = create_programme(
            session,
            clinic_id=CLINIC_A,
            programme_id=PROGRAMME_ID,
            environment="production",
            release_identity="release-pg-r1",
        )
        mark_programme_dark(
            session,
            clinic_id=CLINIC_A,
            programme_id=programme.id,
            actor="operator:test",
            evidence_hash="d" * 64,
            now=NOW,
        )
        for ordinal in range(1, 6):
            enroll_participant(
                session,
                clinic_id=CLINIC_A,
                programme_id=programme.id,
                patient_id=f"patient-wave-one-{ordinal}",
                now=NOW,
            )
        release_cumulative_limit(
            session,
            clinic_id=CLINIC_A,
            programme_id=programme.id,
            cumulative_limit=5,
            actor="operator:test",
            evidence_hash="a" * 64,
            now=NOW,
        )
        session.commit()

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            programme = session.get(PilotProgramme, PROGRAMME_ID)
            released = session.scalar(
                sa.select(sa.func.count())
                .select_from(PilotParticipant)
                .where(PilotParticipant.released_at.is_not(None))
            )
            assert programme is not None
            assert programme.state.value == "active"
            assert programme.active_cumulative_limit == 5
            assert released == 5


def test_postgres_0017_forces_rls_under_ordinary_role(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_migrated_schema(engine, monkeypatch)

    with engine.connect() as connection:
        role = connection.execute(
            sa.text("SELECT rolsuper, rolbypassrls FROM pg_roles " "WHERE rolname = current_user")
        ).one()
        rows = connection.execute(
            sa.text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname IN "
                "('pilot_programme', 'pilot_participant') ORDER BY relname"
            )
        ).all()
        policies = connection.execute(
            sa.text(
                "SELECT tablename, policyname FROM pg_policies "
                "WHERE tablename IN ('pilot_programme', 'pilot_participant') "
                "ORDER BY tablename"
            )
        ).all()
        triggers = set(
            connection.execute(
                sa.text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'pilot_participant'::regclass "
                    "AND NOT tgisinternal"
                )
            ).scalars()
        )
        revision = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()

    assert tuple(role) == (False, False)
    assert [tuple(row) for row in rows] == [
        ("pilot_participant", True, True),
        ("pilot_programme", True, True),
    ]
    assert [tuple(row) for row in policies] == [
        ("pilot_participant", "pilot_participant_tenant_isolation"),
        ("pilot_programme", "pilot_programme_tenant_isolation"),
    ]
    assert triggers == {
        "pilot_participant_identity_guard",
        "pilot_participant_insert_guard",
    }
    with engine.connect() as connection:
        patient_triggers = set(
            connection.execute(
                sa.text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'patient'::regclass AND NOT tgisinternal"
                )
            ).scalars()
        )
    assert "pilot_participant_patient_erasure" in patient_triggers
    assert revision == "0017_pilot_programme_controls"


def test_postgres_concurrent_enrollment_allocates_unique_sequential_ordinals(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_migrated_schema(engine, monkeypatch)
    _seed_two_clinics(engine)
    barrier = threading.Barrier(2)

    def enroll(patient_id: str) -> int:
        with Session(engine, expire_on_commit=False) as session:
            barrier.wait()
            participant = enroll_participant(
                session,
                clinic_id=CLINIC_A,
                programme_id=PROGRAMME_ID,
                patient_id=patient_id,
                now=NOW,
            )
            ordinal = participant.ordinal
            session.commit()
            return ordinal

    with ThreadPoolExecutor(max_workers=2) as pool:
        ordinals = list(
            pool.map(
                enroll,
                ("patient-pilot-pg-a-1", "patient-pilot-pg-a-2"),
            )
        )

    assert sorted(ordinals) == [1, 2]
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            persisted = list(
                session.scalars(sa.select(PilotParticipant).order_by(PilotParticipant.ordinal))
            )
            assert [participant.ordinal for participant in persisted] == [1, 2]


def test_postgres_participant_identity_is_tenant_bound_and_append_only(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_migrated_schema(engine, monkeypatch)
    _seed_two_clinics(engine)
    with Session(engine, expire_on_commit=False) as session:
        participant = enroll_participant(
            session,
            clinic_id=CLINIC_A,
            programme_id=PROGRAMME_ID,
            patient_id="patient-pilot-pg-a-1",
            now=NOW,
        )
        participant_id = participant.id
        session.commit()

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_B):
            assert session.execute(sa.text("SELECT id FROM pilot_programme")).all() == []
            assert session.execute(sa.text("SELECT id FROM pilot_participant")).all() == []
            assert (
                session.execute(
                    sa.text(
                        "UPDATE pilot_programme SET state = 'active' " "WHERE id = :programme_id"
                    ),
                    {"programme_id": PROGRAMME_ID},
                ).rowcount
                == 0
            )
        session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "UPDATE pilot_participant SET patient_id = :foreign_patient "
                        "WHERE id = :participant_id"
                    ),
                    {
                        "foreign_patient": "patient-pilot-pg-b-1",
                        "participant_id": participant_id,
                    },
                )
            session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "UPDATE pilot_participant SET first_contact_at = :now "
                        "WHERE id = :participant_id"
                    ),
                    {"now": NOW, "participant_id": participant_id},
                )
            session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "UPDATE pilot_programme "
                        "SET released_by = 'tampered', release_evidence_hash = :evidence "
                        "WHERE id = :programme_id"
                    ),
                    {"evidence": "f" * 64, "programme_id": PROGRAMME_ID},
                )
            session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "UPDATE pilot_programme "
                        "SET paused_by = 'tampered', pause_reason = 'tampered' "
                        "WHERE id = :programme_id"
                    ),
                    {"programme_id": PROGRAMME_ID},
                )
            session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "UPDATE pilot_programme "
                        "SET state = 'active', active_cumulative_limit = 5, "
                        "released_at = :now, released_by = 'operator:test', "
                        "release_evidence_hash = :evidence "
                        "WHERE id = :programme_id"
                    ),
                    {
                        "now": NOW,
                        "evidence": "a" * 64,
                        "programme_id": PROGRAMME_ID,
                    },
                )
            session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "INSERT INTO pilot_participant "
                        "(id, clinic_id, pilot_programme_id, patient_id, "
                        "patient_key_hash, ordinal, wave, enrolled_at) VALUES "
                        "('participant-duplicate-patient', :clinic_id, :programme_id, "
                        ":patient_id, :forged_hash, 2, 1, :now)"
                    ),
                    {
                        "clinic_id": CLINIC_A,
                        "programme_id": PROGRAMME_ID,
                        "patient_id": "patient-pilot-pg-a-1",
                        "forged_hash": "e" * 64,
                        "now": NOW,
                    },
                )
            session.commit()


def test_postgres_patient_erasure_retains_counted_participant(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_migrated_schema(engine, monkeypatch)
    _seed_two_clinics(engine)
    with Session(engine, expire_on_commit=False) as session:
        participant = enroll_participant(
            session,
            clinic_id=CLINIC_A,
            programme_id=PROGRAMME_ID,
            patient_id="patient-pilot-pg-a-1",
            now=NOW,
        )
        participant_id = participant.id
        patient_hash = participant.patient_key_hash
        session.commit()

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            patient = session.get(Patient, "patient-pilot-pg-a-1")
            assert patient is not None
            session.delete(patient)
        session.commit()

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            participant = session.get(PilotParticipant, participant_id)
            assert participant is not None
            assert participant.clinic_id == CLINIC_A
            assert participant.patient_id is None
            assert participant.patient_key_hash == patient_hash
            assert participant.ordinal == 1

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "INSERT INTO pilot_participant "
                        "(id, clinic_id, pilot_programme_id, patient_id, "
                        "patient_key_hash, ordinal, wave, enrolled_at) VALUES "
                        "('participant-cross-patient', :clinic_id, :programme_id, "
                        ":foreign_patient, :patient_hash, 2, 1, :now)"
                    ),
                    {
                        "clinic_id": CLINIC_A,
                        "programme_id": PROGRAMME_ID,
                        "foreign_patient": "patient-pilot-pg-b-1",
                        "patient_hash": "f" * 64,
                        "now": NOW,
                    },
                )
            session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "UPDATE pilot_participant SET ordinal = 2 " "WHERE id = :participant_id"
                    ),
                    {"participant_id": participant_id},
                )
            session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text("DELETE FROM pilot_participant WHERE id = :participant_id"),
                    {"participant_id": participant_id},
                )
            session.commit()


def test_postgres_programme_identity_and_wave_transition_are_database_guarded(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_migrated_schema(engine, monkeypatch)
    _seed_two_clinics(engine)

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "UPDATE pilot_programme "
                        "SET active_cumulative_limit = 15, state = 'active' "
                        "WHERE id = :programme_id"
                    ),
                    {"programme_id": PROGRAMME_ID},
                )
            session.commit()

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            session.execute(
                sa.text("UPDATE pilot_programme SET state = 'closed' " "WHERE id = :programme_id"),
                {"programme_id": PROGRAMME_ID},
            )
        session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "UPDATE pilot_programme SET state = 'draft' " "WHERE id = :programme_id"
                    ),
                    {"programme_id": PROGRAMME_ID},
                )
            session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "UPDATE pilot_programme SET release_identity = 'tampered' "
                        "WHERE id = :programme_id"
                    ),
                    {"programme_id": PROGRAMME_ID},
                )
            session.commit()
