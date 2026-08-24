"""Ordinary-role PostgreSQL proof for PR-03 cursor, RLS, and overlap."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from src.clinic_recall.db import clinic_scope
from src.clinic_recall.durable.enqueue import enqueue_sms_effect
from src.clinic_recall.durable.planner import run_planning_pass as _run_planning_pass
from src.clinic_recall.models import (
    Appointment,
    Base,
    CadenceCursor,
    Campaign,
    Clinic,
    ExternalEffect,
    ExternalEffectHandoff,
    OutreachJob,
    Patient,
)
from src.clinic_recall.pilot_controls import PilotGateDecision
from src.clinic_recall.rls import apply_rls_policies, drop_rls_policies
from src.clinic_recall.sync.base import make_id

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
CLINIC_A = "clinic-cadence-pg-a"
CLINIC_B = "clinic-cadence-pg-b"


def _allow_pilot(*_args) -> PilotGateDecision:
    return PilotGateDecision(True, "allowed")


def run_planning_pass(*args, **kwargs):
    kwargs.setdefault("sms_pilot_gate", _allow_pilot)
    kwargs.setdefault("programme_gate", _allow_pilot)
    return _run_planning_pass(*args, **kwargs)


def _drop_rls_safely(connection) -> None:
    savepoint = connection.begin_nested()
    try:
        drop_rls_policies(connection)
    except DBAPIError:
        savepoint.rollback()
    else:
        savepoint.commit()


def _reset_schema(engine) -> None:
    with engine.begin() as connection:
        _drop_rls_safely(connection)
        Base.metadata.drop_all(connection)
        Base.metadata.create_all(connection)
        apply_rls_policies(connection)


def _seed_due_sms(engine, clinic_id: str = CLINIC_A) -> None:
    with Session(engine, expire_on_commit=False) as session:
        session.add(Clinic(id=clinic_id, name="PostgreSQL Cadence Clinic"))
        session.commit()
        with clinic_scope(session, clinic_id):
            session.add(
                Patient(
                    id=f"patient-{clinic_id}",
                    clinic_id=clinic_id,
                    source_ref=f"patient-{clinic_id}",
                    name="Synthetic PostgreSQL Patient",
                    phone="+447700900001",
                    consent_flags={"sms": True, "call": True},
                    opt_out_flags={},
                )
            )
            session.flush()
            session.add(
                Appointment(
                    id=f"appointment-{clinic_id}",
                    clinic_id=clinic_id,
                    patient_id=f"patient-{clinic_id}",
                    source_ref=f"appointment-{clinic_id}",
                    status="missed",
                    start_at=NOW - timedelta(days=2),
                )
            )
            session.add(
                Campaign(
                    id=f"campaign-{clinic_id}",
                    clinic_id=clinic_id,
                    type="recovery",
                    status="active",
                )
            )
            session.flush()
            session.add(
                OutreachJob(
                    id=f"job-{clinic_id}",
                    clinic_id=clinic_id,
                    campaign_id=f"campaign-{clinic_id}",
                    patient_id=f"patient-{clinic_id}",
                    appointment_id=f"appointment-{clinic_id}",
                    channel="sms",
                    state="queued",
                )
            )
        session.commit()


def test_postgres_migration_0016_upgrades_0015_and_forces_rls(
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
    command.upgrade(alembic_config, "0015_provider_callback_receipts")
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS external_effect_handoff CASCADE"))
        connection.execute(text("DROP TABLE IF EXISTS cadence_cursor CASCADE"))

    command.upgrade(alembic_config, "0016_scheduled_cadence")

    inspector = inspect(engine)
    assert {"cadence_cursor", "external_effect_handoff"} <= set(inspector.get_table_names())
    with engine.connect() as connection:
        role = connection.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles " "WHERE rolname = current_user")
        ).one()
        rls_rows = connection.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname IN "
                "('cadence_cursor', 'external_effect_handoff') ORDER BY relname"
            )
        ).all()
        policies = connection.execute(
            text(
                "SELECT tablename, policyname FROM pg_policies WHERE tablename IN "
                "('cadence_cursor', 'external_effect_handoff') ORDER BY tablename"
            )
        ).all()
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert tuple(role) == (False, False)
    assert [tuple(row) for row in rls_rows] == [
        ("cadence_cursor", True, True),
        ("external_effect_handoff", True, True),
    ]
    assert [tuple(row) for row in policies] == [
        ("cadence_cursor", "cadence_cursor_tenant_isolation"),
        (
            "external_effect_handoff",
            "external_effect_handoff_tenant_isolation",
        ),
    ]
    assert revision == "0016_scheduled_cadence"


def test_postgres_cursor_and_handoff_are_tenant_isolated(
    clinic_recall_pg_engine,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_schema(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(Clinic(id=CLINIC_A, name="Cadence A"))
        session.add(Clinic(id=CLINIC_B, name="Cadence B"))
        session.commit()
        effect, _ = enqueue_sms_effect(
            session,
            clinic_id=CLINIC_A,
            outreach_job_id="job-internal-a",
            idempotency_key="cadence:sms:job-internal-a",
            available_at=NOW,
        )
        with clinic_scope(session, CLINIC_A):
            session.add(
                CadenceCursor(
                    id=make_id("cadence-cursor", CLINIC_A, "scheduled_cadence"),
                    clinic_id=CLINIC_A,
                    planner_name="scheduled_cadence",
                    watermark_at=NOW,
                )
            )
            session.add(
                ExternalEffectHandoff(
                    id="effect-handoff-a",
                    clinic_id=CLINIC_A,
                    external_effect_id=effect.id,
                    status="queued",
                    reason_code="retry_exhausted",
                )
            )
        session.commit()

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_B):
            assert session.execute(text("SELECT id FROM cadence_cursor")).all() == []
            assert session.execute(text("SELECT id FROM cadence_cursor FOR UPDATE")).all() == []
            assert session.execute(text("SELECT id FROM external_effect_handoff")).all() == []
            assert (
                session.execute(
                    text(
                        "UPDATE cadence_cursor SET last_run_id = 'tampered' "
                        "WHERE clinic_id = :clinic_id"
                    ),
                    {"clinic_id": CLINIC_A},
                ).rowcount
                == 0
            )
            assert (
                session.execute(
                    text(
                        "UPDATE external_effect_handoff SET reason_code = 'tampered' "
                        "WHERE clinic_id = :clinic_id"
                    ),
                    {"clinic_id": CLINIC_A},
                ).rowcount
                == 0
            )
        session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_B):
                session.execute(
                    text(
                        "INSERT INTO cadence_cursor "
                        "(id, clinic_id, planner_name, watermark_at) "
                        "VALUES ('cursor-intruder', :clinic_id, 'scheduled_cadence', :now)"
                    ),
                    {"clinic_id": CLINIC_A, "now": NOW},
                )
            session.commit()

    with pytest.raises(DBAPIError):
        with Session(engine, expire_on_commit=False) as session:
            with clinic_scope(session, CLINIC_B):
                session.execute(
                    text(
                        "INSERT INTO external_effect_handoff "
                        "(id, clinic_id, external_effect_id, status, reason_code) "
                        "VALUES ('handoff-intruder', :clinic_id, :effect_id, "
                        "'queued', 'retry_exhausted')"
                    ),
                    {"clinic_id": CLINIC_A, "effect_id": effect.id},
                )
            session.commit()

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            cursor = session.execute(select(CadenceCursor)).scalar_one()
            handoff = session.execute(select(ExternalEffectHandoff)).scalar_one()
            assert cursor.last_run_id is None
            assert handoff.reason_code == "retry_exhausted"


def test_postgres_overlapping_ticks_create_one_cursor_and_effect(
    clinic_recall_pg_engine,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_schema(engine)
    _seed_due_sms(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    start = threading.Barrier(2)

    def plan_once() -> tuple[int, bool]:
        start.wait()
        result = run_planning_pass(
            factory,
            clinic_id=CLINIC_A,
            now=NOW,
            enabled=True,
            batch_size=10,
        )
        return result.sms_enqueued, result.cursor_advanced

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: plan_once(), range(2)))

    assert sum(item[0] for item in results) == 1
    assert sum(1 for item in results if item[1]) == 1
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            assert session.scalar(select(sa.func.count()).select_from(CadenceCursor)) == 1
            assert session.scalar(select(sa.func.count()).select_from(ExternalEffect)) == 1


def test_postgres_overlapping_voice_ticks_create_one_call_effect(
    clinic_recall_pg_engine,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_schema(engine)
    _seed_due_sms(engine)
    with Session(engine, expire_on_commit=False) as session:
        sms_effect, _ = enqueue_sms_effect(
            session,
            clinic_id=CLINIC_A,
            outreach_job_id=f"job-{CLINIC_A}",
            idempotency_key=f"cadence:sms:job-{CLINIC_A}",
            available_at=NOW - timedelta(hours=49),
        )
        sms_effect.state = "succeeded"
        sms_effect.dispatch_started_at = NOW - timedelta(hours=49)
        sms_effect.provider_status = "delivery_succeeded"
        sms_effect.provider_resource_id = "SM-synthetic-terminal"
        sms_effect.completed_at = NOW - timedelta(hours=48, minutes=59)
        session.commit()

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    start = threading.Barrier(2)

    def plan_once() -> tuple[int, bool]:
        start.wait()
        result = run_planning_pass(
            factory,
            clinic_id=CLINIC_A,
            now=NOW,
            enabled=True,
            batch_size=10,
            programme_gate=lambda *_args: True,
        )
        return result.calls_enqueued, result.cursor_advanced

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: plan_once(), range(2)))

    assert sum(item[0] for item in results) == 1
    assert sum(1 for item in results if item[1]) == 1
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            call_count = session.scalar(
                select(sa.func.count())
                .select_from(ExternalEffect)
                .where(ExternalEffect.effect_type == "call")
            )
            assert call_count == 1
