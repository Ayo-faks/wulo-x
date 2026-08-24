"""Disposable PostgreSQL proof for durable SMS concurrency and RLS."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from src.clinic_recall.db import clinic_scope
from src.clinic_recall.durable.callbacks import generate_effect_token
from src.clinic_recall.durable.effects import claim_effects
from src.clinic_recall.durable.enqueue import enqueue_call_effect, enqueue_sms_effect
from src.clinic_recall.enums import ExternalEffectState, ExternalEffectType
from src.clinic_recall.models import Base, Clinic, ExternalEffect
from src.clinic_recall.rls import apply_rls_policies, drop_rls_policies

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
CLINIC_ID = "clinic-durable-pg"


def _drop_rls_safely(connection) -> None:
    savepoint = connection.begin_nested()
    try:
        drop_rls_policies(connection)
    except DBAPIError:
        savepoint.rollback()
    else:
        savepoint.commit()


def test_postgres_concurrent_workers_claim_effect_once(clinic_recall_pg_engine) -> None:
    engine = clinic_recall_pg_engine
    with engine.begin() as connection:
        _drop_rls_safely(connection)
        Base.metadata.drop_all(connection)
        Base.metadata.create_all(connection)
        apply_rls_policies(connection)
    with Session(engine, expire_on_commit=False) as session:
        session.add(Clinic(id=CLINIC_ID, name="Durable PostgreSQL Clinic"))
        session.commit()
        effect, _ = enqueue_sms_effect(
            session,
            clinic_id=CLINIC_ID,
            outreach_job_id="job-internal-001",
            idempotency_key="recall-sms:job-internal-001",
            available_at=NOW,
        )
        effect_id = effect.id
        session.commit()

    start = threading.Barrier(2)

    def claim(worker_id: str) -> list[str]:
        with Session(engine, expire_on_commit=False) as session:
            start.wait()
            claimed = claim_effects(
                session,
                clinic_id=CLINIC_ID,
                worker_id=worker_id,
                now=NOW,
                lease_for=timedelta(minutes=5),
            )
            ids = [item.id for item in claimed]
            session.commit()
            return ids

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-a", "worker-b")))

    assert sum(len(worker_claims) for worker_claims in claims) == 1
    assert {item for worker_claims in claims for item in worker_claims} == {effect_id}


def test_postgres_concurrent_call_workers_claim_only_one_call_effect(
    clinic_recall_pg_engine,
) -> None:
    engine = clinic_recall_pg_engine
    with engine.begin() as connection:
        _drop_rls_safely(connection)
        Base.metadata.drop_all(connection)
        Base.metadata.create_all(connection)
        apply_rls_policies(connection)
    with Session(engine, expire_on_commit=False) as session:
        session.add(Clinic(id=CLINIC_ID, name="Durable PostgreSQL CALL Clinic"))
        session.commit()
        call_effect, _ = enqueue_call_effect(
            session,
            clinic_id=CLINIC_ID,
            outreach_job_id="job-call-internal-001",
            idempotency_key="cadence:call:job-call-internal-001",
            available_at=NOW,
        )
        sms_effect, _ = enqueue_sms_effect(
            session,
            clinic_id=CLINIC_ID,
            outreach_job_id="job-call-internal-001",
            idempotency_key="synthetic:sms:job-call-internal-001",
            available_at=NOW,
        )
        recording_effect = ExternalEffect(
            id="effect-postgres-call-recording",
            clinic_id=CLINIC_ID,
            aggregate_type="outreach_job",
            aggregate_id="job-call-internal-001",
            effect_type=ExternalEffectType.RECORDING,
            idempotency_key="synthetic:recording:job-call-internal-001",
            callback_token=generate_effect_token(CLINIC_ID),
            payload_version=1,
            payload={"intent": "synthetic_recording_fixture"},
            request_hash="f" * 64,
            state=ExternalEffectState.PENDING,
            available_at=NOW,
            max_attempts=1,
        )
        session.add(recording_effect)
        session.commit()
        call_effect_id = call_effect.id
        sms_effect_id = sms_effect.id

    start = threading.Barrier(2)

    def claim(worker_id: str) -> list[str]:
        with Session(engine, expire_on_commit=False) as session:
            start.wait()
            claimed = claim_effects(
                session,
                clinic_id=CLINIC_ID,
                worker_id=worker_id,
                now=NOW,
                lease_for=timedelta(minutes=5),
                effect_types=(ExternalEffectType.CALL,),
            )
            ids = [item.id for item in claimed]
            session.commit()
            return ids

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("call-worker-a", "call-worker-b")))

    assert sum(len(worker_claims) for worker_claims in claims) == 1
    assert {item for worker_claims in claims for item in worker_claims} == {
        call_effect_id
    }
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_ID):
            sms = session.get(ExternalEffect, sms_effect_id)
            recording = session.get(
                ExternalEffect,
                "effect-postgres-call-recording",
            )
            assert sms is not None and sms.state == ExternalEffectState.PENDING
            assert recording is not None
            assert recording.state == ExternalEffectState.PENDING


def test_postgres_migration_0014_installs_forced_rls(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))

    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        engine.url.render_as_string(hide_password=False),
    )
    alembic_config = Config("infra/postgres/alembic.ini")
    command.upgrade(alembic_config, "0013_recall_task_idempotency")
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS external_effect CASCADE"))
        connection.execute(text("DROP TYPE IF EXISTS external_effect_state"))
        connection.execute(text("DROP TYPE IF EXISTS external_effect_type"))

    command.upgrade(alembic_config, "0014_external_effect_outbox")
    assert "external_effect" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        rls = connection.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE oid = 'external_effect'::regclass"
            )
        ).one()
        policy = connection.execute(
            text(
                "SELECT policyname FROM pg_policies "
                "WHERE tablename = 'external_effect'"
            )
        ).scalar_one()
    assert tuple(rls) == (True, True)
    assert policy == "external_effect_tenant_isolation"

    command.downgrade(alembic_config, "0013_recall_task_idempotency")
    assert "external_effect" not in inspect(engine).get_table_names()
    command.upgrade(alembic_config, "0014_external_effect_outbox")
    assert "external_effect" in inspect(engine).get_table_names()