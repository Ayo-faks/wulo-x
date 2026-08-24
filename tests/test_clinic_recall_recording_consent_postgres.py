"""Ordinary-role PostgreSQL proof for the PR-09 all-call ledger."""

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
from src.clinic_recall.enums import (
    ClinicPhoneProvider,
    InboundCallStatus,
    InteractionDirection,
)
from src.clinic_recall.models import CallRecord, Clinic, InboundCall, Patient
from src.clinic_recall.recording import bind_call_record_provider_identity, ensure_call_record

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
CLINIC_A = "clinic-recording-pg-a"
CLINIC_B = "clinic-recording-pg-b"
CALL_SID = "CA" + "a" * 32


def _reset_to_0018(engine, monkeypatch) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(sa.text("DROP SCHEMA public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))
    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        engine.url.render_as_string(hide_password=False),
    )
    command.upgrade(Config("infra/postgres/alembic.ini"), "0018_recording_consent_ledger")


def _seed_clinics_and_inbound_calls(engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        session.add_all(
            [
                Clinic(id=CLINIC_A, name="Recording PostgreSQL A"),
                Clinic(id=CLINIC_B, name="Recording PostgreSQL B"),
            ]
        )
        session.commit()
        with clinic_scope(session, CLINIC_A):
            session.add_all(
                [
                    InboundCall(
                        id="inbound-recording-pg-a",
                        clinic_id=CLINIC_A,
                        provider=ClinicPhoneProvider.TWILIO,
                        provider_call_id=CALL_SID,
                        called_number="+441111111111",
                        status=InboundCallStatus.STARTED,
                    ),
                    InboundCall(
                        id="inbound-recording-acs-pg-a",
                        clinic_id=CLINIC_A,
                        provider=ClinicPhoneProvider.ACS,
                        provider_call_id="acs-correlation-pg-a",
                        called_number="+441111111112",
                        status=InboundCallStatus.STARTED,
                    ),
                    Patient(
                        id="patient-recording-pg-a",
                        clinic_id=CLINIC_A,
                        source_ref="patient-recording-pg-a",
                        name="Recording PostgreSQL Patient A",
                    ),
                ]
            )
        session.commit()
        with clinic_scope(session, CLINIC_B):
            session.add_all(
                [
                    InboundCall(
                        id="inbound-recording-pg-b",
                        clinic_id=CLINIC_B,
                        provider=ClinicPhoneProvider.TWILIO,
                        provider_call_id="CA" + "b" * 32,
                        called_number="+442222222222",
                        status=InboundCallStatus.STARTED,
                    ),
                    Patient(
                        id="patient-recording-pg-b",
                        clinic_id=CLINIC_B,
                        source_ref="patient-recording-pg-b",
                        name="Recording PostgreSQL Patient B",
                    ),
                ]
            )
        session.commit()


def test_postgres_0018_upgrades_0017_with_truthful_backfill_and_forced_rls(
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
    command.upgrade(config, "0017_pilot_programme_controls")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO clinic (id, name) VALUES "
                "('clinic-recording-legacy', 'Legacy Recording Clinic')"
            )
        )
        connection.execute(sa.text("SET LOCAL app.clinic_id = 'clinic-recording-legacy'"))
        connection.execute(
            sa.text(
                "INSERT INTO call_record ("
                "id, clinic_id, provider, provider_call_id, direction, "
                "recording_status, consent_snapshot"
                ") VALUES ("
                "'callrec-recording-legacy', 'clinic-recording-legacy', 'twilio', "
                "'CAlegacy', 'outbound', 'stored', '{\"record_call\": true}'"
                ")"
            )
        )

    command.upgrade(config, "0018_recording_consent_ledger")

    with engine.connect() as connection:
        role = connection.execute(
            sa.text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
        rls = connection.execute(
            sa.text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = 'call_record'::regclass"
            )
        ).one()
        policy = connection.execute(
            sa.text("SELECT policyname FROM pg_policies WHERE tablename = 'call_record'")
        ).scalar_one()
        connection.execute(sa.text("BEGIN"))
        connection.execute(sa.text("SET LOCAL app.clinic_id = 'clinic-recording-legacy'"))
        legacy = connection.execute(
            sa.text(
                "SELECT consent_state, consent_decision_source, consent_version, "
                "deletion_state FROM call_record WHERE id = 'callrec-recording-legacy'"
            )
        ).one()
        connection.execute(sa.text("ROLLBACK"))
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        inspector = sa.inspect(connection)
        foreign_keys = {item["name"] for item in inspector.get_foreign_keys("call_record")}
        uniques = {item["name"] for item in inspector.get_unique_constraints("call_record")}
        checks = {item["name"] for item in inspector.get_check_constraints("call_record")}

    assert tuple(role) == (False, False)
    assert tuple(rls) == (True, True)
    assert policy == "call_record_tenant_isolation"
    assert tuple(legacy) == (
        "ambiguous",
        "policy",
        "legacy-stored-consent-v0",
        "not_requested",
    )
    assert revision == "0018_recording_consent_ledger"
    assert foreign_keys >= {
        "fk_call_record_external_effect_tenant",
        "fk_call_record_inbound_call_tenant",
        "fk_call_record_inbound_provider_tenant",
        "fk_call_record_patient_tenant",
    }
    assert uniques >= {
        "uq_call_record_external_effect",
        "uq_call_record_inbound_call",
    }
    assert checks >= {
        "ck_call_record_has_trusted_anchor",
        "ck_call_record_not_both_internal_anchors",
    }


def test_postgres_call_ledger_is_tenant_isolated_and_anchor_bound(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_0018(engine, monkeypatch)
    _seed_clinics_and_inbound_calls(engine)

    with Session(engine, expire_on_commit=False) as session:
        ensure_call_record(
            session,
            CLINIC_A,
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id=CALL_SID,
            inbound_call_id="inbound-recording-pg-a",
            session_id="recording-pg-a",
            direction=InteractionDirection.INBOUND,
            scenario="inbound_clinic",
            patient_id=None,
            consent_snapshot=None,
            now=NOW,
        )
        session.commit()

    with Session(engine, expire_on_commit=False) as session:
        record = ensure_call_record(
            session,
            CLINIC_A,
            provider=ClinicPhoneProvider.ACS,
            provider_call_id=None,
            inbound_call_id="inbound-recording-acs-pg-a",
            session_id="recording-acs-pg-a",
            direction=InteractionDirection.INBOUND,
            scenario="inbound_clinic",
            patient_id=None,
            consent_snapshot=None,
            now=NOW,
        )
        session.commit()
        bind_call_record_provider_identity(
            session,
            clinic_id=CLINIC_A,
            call_record_id=record.id,
            provider=ClinicPhoneProvider.ACS,
            provider_call_id="acs-connection-pg-a",
        )
        session.commit()

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_B):
            assert session.execute(sa.text("SELECT id FROM call_record")).all() == []
            result = session.execute(
                sa.text(
                    "UPDATE call_record SET outcome = 'hijacked' "
                    "WHERE id = 'callrec-recording-pg-a'"
                )
            )
            assert result.rowcount == 0
        session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "INSERT INTO call_record ("
                        "id, clinic_id, provider, direction, consent_state, "
                        "recording_status, deletion_state"
                        ") VALUES ("
                        "'callrec-no-anchor', :clinic_id, 'twilio', 'inbound', "
                        "'not_asked', 'none', 'not_requested'"
                        ")"
                    ),
                    {"clinic_id": CLINIC_A},
                )
            session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "INSERT INTO call_record ("
                        "id, clinic_id, patient_id, provider, provider_call_id, "
                        "direction, consent_state, recording_status, deletion_state"
                        ") VALUES ("
                        "'callrec-cross-patient', :clinic_id, 'patient-recording-pg-b', "
                        "'twilio', :call_sid, 'outbound', 'not_asked', 'none', "
                        "'not_requested'"
                        ")"
                    ),
                    {"clinic_id": CLINIC_A, "call_sid": "CA" + "d" * 32},
                )
            session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "INSERT INTO call_record ("
                        "id, clinic_id, provider, inbound_call_id, direction, "
                        "consent_state, recording_status, deletion_state"
                        ") VALUES ("
                        "'callrec-provider-mismatch', :clinic_id, 'acs', "
                        "'inbound-recording-pg-a', 'inbound', 'not_asked', "
                        "'none', 'not_requested'"
                        ")"
                    ),
                    {"clinic_id": CLINIC_A},
                )
            session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "INSERT INTO call_record ("
                        "id, clinic_id, provider, provider_call_id, inbound_call_id, "
                        "direction, consent_state, recording_status, deletion_state"
                        ") VALUES ("
                        "'callrec-provider-call-mismatch', :clinic_id, 'twilio', "
                        ":call_sid, 'inbound-recording-pg-a', 'inbound', "
                        "'not_asked', 'none', 'not_requested'"
                        ")"
                    ),
                    {"clinic_id": CLINIC_A, "call_sid": "CA" + "e" * 32},
                )
            session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "INSERT INTO call_record ("
                        "id, clinic_id, provider, provider_call_id, inbound_call_id, "
                        "direction, consent_state, recording_status, deletion_state"
                        ") VALUES ("
                        "'callrec-cross-anchor', :clinic_id, 'twilio', :call_sid, "
                        "'inbound-recording-pg-b', 'inbound', 'not_asked', 'none', "
                        "'not_requested'"
                        ")"
                    ),
                    {
                        "clinic_id": CLINIC_A,
                        "call_sid": "CA" + "c" * 32,
                    },
                )
            session.commit()


def test_postgres_0018_replay_preserves_new_anchored_call_state(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_0018(engine, monkeypatch)
    _seed_clinics_and_inbound_calls(engine)
    with Session(engine, expire_on_commit=False) as session:
        record = ensure_call_record(
            session,
            CLINIC_A,
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id=CALL_SID,
            inbound_call_id="inbound-recording-pg-a",
            session_id="recording-pg-replay",
            direction=InteractionDirection.INBOUND,
            scenario="inbound_clinic",
            patient_id=None,
            consent_snapshot=None,
            now=NOW,
        )
        record_id = record.id
        session.commit()

    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE alembic_version SET version_num = '0017_pilot_programme_controls'")
        )
    command.upgrade(Config("infra/postgres/alembic.ini"), "0018_recording_consent_ledger")

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            record = session.get(CallRecord, record_id)
            assert record is not None
            assert record.consent_state.value == "not_asked"
            assert record.consent_version is None
            assert record.inbound_call_id == "inbound-recording-pg-a"
    with engine.connect() as connection:
        constraints = connection.execute(
            sa.text(
                "SELECT conname, count(*) FROM pg_constraint "
                "WHERE conrelid = 'call_record'::regclass "
                "AND conname IN ("
                "'fk_call_record_external_effect_tenant', "
                "'fk_call_record_inbound_call_tenant', "
                "'fk_call_record_inbound_provider_tenant', "
                "'fk_call_record_patient_tenant', "
                "'uq_call_record_external_effect', "
                "'uq_call_record_inbound_call'"
                ") GROUP BY conname"
            )
        ).all()
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert {str(name): int(count) for name, count in constraints} == {
        "fk_call_record_external_effect_tenant": 1,
        "fk_call_record_inbound_call_tenant": 1,
        "fk_call_record_inbound_provider_tenant": 1,
        "fk_call_record_patient_tenant": 1,
        "uq_call_record_external_effect": 1,
        "uq_call_record_inbound_call": 1,
    }
    assert revision == "0018_recording_consent_ledger"


def test_postgres_concurrent_call_establishment_converges_to_one_ledger_row(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_0018(engine, monkeypatch)
    _seed_clinics_and_inbound_calls(engine)
    barrier = threading.Barrier(2)

    def establish(session_id: str) -> str:
        with Session(engine, expire_on_commit=False) as session:
            barrier.wait()
            record = ensure_call_record(
                session,
                CLINIC_A,
                provider=ClinicPhoneProvider.TWILIO,
                provider_call_id=CALL_SID,
                inbound_call_id="inbound-recording-pg-a",
                session_id=session_id,
                direction=InteractionDirection.INBOUND,
                scenario="inbound_clinic",
                patient_id=None,
                consent_snapshot=None,
                now=NOW,
            )
            record_id = record.id
            session.commit()
            return record_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        record_ids = list(pool.map(establish, ("recording-pg-1", "recording-pg-2")))

    assert len(set(record_ids)) == 1
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            assert session.scalar(sa.select(sa.func.count()).select_from(CallRecord)) == 1
