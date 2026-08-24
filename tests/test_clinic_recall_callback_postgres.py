"""Real PostgreSQL proof for callback receipt RLS, locks, and reconciliation."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from src.clinic_recall.db import clinic_scope
from src.clinic_recall.durable.callbacks import (
    CallbackCorrelationError,
    ProviderCallbackKind,
    ProviderCallbackState,
    generate_effect_token,
    receive_twilio_callback,
    reconcile_once,
)
from src.clinic_recall.durable.effects import (
    claim_effects,
    lock_dispatching_effect,
    mark_dispatching,
    mark_succeeded,
)
from src.clinic_recall.durable.enqueue import enqueue_call_effect, enqueue_sms_effect
from src.clinic_recall.enums import ExternalEffectState, ExternalEffectType, OutreachState
from src.clinic_recall.models import (
    Appointment,
    Base,
    Campaign,
    Clinic,
    ExternalEffect,
    OutreachJob,
    Patient,
    ProviderCallbackReceipt,
)
from src.clinic_recall.rls import apply_rls_policies, drop_rls_policies

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 19, 11, 0, tzinfo=UTC)
CLINIC_A = "clinic-callback-pg-a"
CLINIC_B = "clinic-callback-pg-b"
MESSAGE_SID = "SM" + "a" * 32
CALL_SID = "CA" + "b" * 32


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


def _seed_sms_dispatch(engine, clinic_id: str = CLINIC_A) -> tuple[str, str]:
    with Session(engine, expire_on_commit=False) as session:
        session.add(Clinic(id=clinic_id, name="PostgreSQL Callback Clinic"))
        session.commit()
        with clinic_scope(session, clinic_id):
            patient = Patient(
                id=f"patient-{clinic_id}",
                clinic_id=clinic_id,
                source_ref=f"patient-{clinic_id}",
                name="Synthetic PostgreSQL Patient",
                phone="+447700900001",
                consent_flags={"sms": True},
                opt_out_flags={},
            )
            session.add(patient)
            session.flush()
            session.add(
                Appointment(
                    id=f"appointment-{clinic_id}",
                    clinic_id=clinic_id,
                    patient_id=f"patient-{clinic_id}",
                    source_ref=f"appointment-{clinic_id}",
                    status="missed",
                    start_at=NOW,
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
                    state=OutreachState.QUEUED,
                )
            )
            session.flush()
        session.commit()
        effect, _ = enqueue_sms_effect(
            session,
            clinic_id=clinic_id,
            outreach_job_id=f"job-{clinic_id}",
            idempotency_key=f"recall-sms:job-{clinic_id}",
            available_at=NOW,
            max_attempts=1,
        )
        effect_id = effect.id
        effect_token = effect.callback_token
        session.commit()
        claim_effects(
            session,
            clinic_id=clinic_id,
            worker_id="worker-postgres-dispatch",
            now=NOW,
            lease_for=timedelta(minutes=5),
        )
        mark_dispatching(
            session,
            clinic_id=clinic_id,
            effect_id=effect_id,
            worker_id="worker-postgres-dispatch",
            now=NOW,
        )
        session.commit()
    return effect_id, effect_token


def _delivery_fields() -> dict[str, str]:
    return {
        "MessageSid": MESSAGE_SID,
        "MessageStatus": "delivered",
        "From": "+447700900001",
        "To": "+447700900002",
    }


def _seed_call_dispatch(engine) -> tuple[str, str]:
    with Session(engine, expire_on_commit=False) as session:
        session.add(Clinic(id=CLINIC_A, name="PostgreSQL CALL Callback Clinic"))
        session.commit()
        with clinic_scope(session, CLINIC_A):
            session.add(
                Patient(
                    id=f"patient-{CLINIC_A}",
                    clinic_id=CLINIC_A,
                    source_ref=f"patient-{CLINIC_A}",
                    name="Synthetic PostgreSQL CALL Patient",
                    phone="+447700900001",
                    consent_flags={"call": True},
                    opt_out_flags={},
                )
            )
            session.add(
                Campaign(
                    id=f"campaign-{CLINIC_A}",
                    clinic_id=CLINIC_A,
                    type="recovery",
                    status="active",
                )
            )
            session.flush()
            session.add(
                OutreachJob(
                    id=f"job-{CLINIC_A}",
                    clinic_id=CLINIC_A,
                    campaign_id=f"campaign-{CLINIC_A}",
                    patient_id=f"patient-{CLINIC_A}",
                    channel="sms",
                    state=OutreachState.NO_REPLY,
                )
            )
            session.flush()
        session.commit()
        effect, _ = enqueue_call_effect(
            session,
            clinic_id=CLINIC_A,
            outreach_job_id=f"job-{CLINIC_A}",
            idempotency_key=f"cadence:call:job-{CLINIC_A}",
            available_at=NOW,
        )
        effect_id = effect.id
        effect_token = effect.callback_token
        session.commit()
        claim_effects(
            session,
            clinic_id=CLINIC_A,
            worker_id="worker-postgres-call-dispatch",
            now=NOW,
            lease_for=timedelta(minutes=5),
            effect_types=(ExternalEffectType.CALL,),
        )
        mark_dispatching(
            session,
            clinic_id=CLINIC_A,
            effect_id=effect_id,
            worker_id="worker-postgres-call-dispatch",
            now=NOW,
        )
        session.commit()
    return effect_id, effect_token


def test_postgres_callback_suite_uses_ordinary_role(clinic_recall_pg_engine) -> None:
    with clinic_recall_pg_engine.connect() as connection:
        role = connection.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()

    assert tuple(role) == (False, False)


def test_postgres_migration_0015_upgrades_0014_and_forces_rls(
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
        connection.execute(text("DROP TABLE IF EXISTS provider_callback_receipt CASCADE"))
        connection.execute(text("DROP TABLE IF EXISTS external_effect CASCADE"))
    command.upgrade(alembic_config, "0014_external_effect_outbox")
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO clinic (id, name) VALUES ('clinic-migrate', 'Migrate')")
        )
        connection.execute(text("SET LOCAL app.clinic_id = 'clinic-migrate'"))
        connection.execute(
            text(
                "INSERT INTO external_effect "
                "(id, clinic_id, aggregate_type, aggregate_id, effect_type, "
                "idempotency_key, payload_version, payload, request_hash, state, "
                "available_at, attempt_count, max_attempts) VALUES "
                "('effect-migrate', 'clinic-migrate', 'outreach_job', 'job-migrate', "
                "'sms', 'recall-sms:migrate', 1, '{}', :request_hash, "
                "'reconcile_required', CURRENT_TIMESTAMP, 1, 1)"
            ),
            {"request_hash": "0" * 64},
        )

    command.upgrade(alembic_config, "head")
    inspector = inspect(engine)
    assert "provider_callback_receipt" in inspector.get_table_names()
    with engine.connect() as connection:
        rls = connection.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = 'provider_callback_receipt'::regclass"
            )
        ).one()
        policy = connection.execute(
            text("SELECT policyname FROM pg_policies WHERE tablename = 'provider_callback_receipt'")
        ).scalar_one()
        connection.execute(text("SET LOCAL app.clinic_id = 'clinic-migrate'"))
        token_present = connection.execute(
            text(
                "SELECT callback_token IS NOT NULL FROM external_effect WHERE id = 'effect-migrate'"
            )
        ).scalar_one()
    assert tuple(rls) == (True, True)
    assert policy == "provider_callback_receipt_tenant_isolation"
    assert token_present is True


def test_postgres_receipts_are_tenant_isolated_and_tampered_scope_fails(
    clinic_recall_pg_engine,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_schema(engine)
    effect_id, effect_token = _seed_sms_dispatch(engine)
    with Session(engine, expire_on_commit=False) as session:
        callback_result = receive_twilio_callback(
            session,
            effect_token=effect_token,
            callback_kind=ProviderCallbackKind.SMS,
            fields=_delivery_fields(),
            raw_payload=b"postgres-rls-callback",
            received_at=NOW,
        )
        session.commit()
        receipt_id = callback_result.receipt_id
        with clinic_scope(session, CLINIC_A):
            assert session.scalar(select(sa.func.count()).select_from(ProviderCallbackReceipt)) == 1

    with Session(engine, expire_on_commit=False) as session:
        session.add(Clinic(id=CLINIC_B, name="Other Callback Clinic"))
        session.commit()
        with clinic_scope(session, CLINIC_B):
            assert session.execute(text("SELECT id FROM provider_callback_receipt")).all() == []
            update = session.execute(
                text(
                    "UPDATE provider_callback_receipt SET normalized_status = 'failed' "
                    "WHERE id = :receipt_id"
                ),
                {"receipt_id": receipt_id},
            )
            assert update.rowcount == 0
            session.add(
                ProviderCallbackReceipt(
                    id="receipt-cross-tenant",
                    clinic_id=CLINIC_B,
                    external_effect_id=effect_id,
                    provider="twilio",
                    callback_kind=ProviderCallbackKind.SMS,
                    deduplication_hash="9" * 64,
                    effect_token_hash="a" * 64,
                    provider_resource_id=MESSAGE_SID,
                    normalized_status="delivered",
                    payload_hash="b" * 64,
                    state=ProviderCallbackState.PENDING,
                    received_at=NOW,
                )
            )
            with pytest.raises(DBAPIError):
                session.flush()
            session.rollback()

    clinic_b_token = generate_effect_token(CLINIC_B)
    with Session(engine, expire_on_commit=False) as session:
        with pytest.raises(CallbackCorrelationError):
            receive_twilio_callback(
                session,
                effect_token=clinic_b_token,
                callback_kind=ProviderCallbackKind.SMS,
                fields=_delivery_fields(),
                raw_payload=b"tampered-tenant-callback",
                received_at=NOW,
            )
        session.rollback()
        with clinic_scope(session, CLINIC_A):
            effect = session.get(ExternalEffect, effect_id)
            assert effect is not None and effect.state == ExternalEffectState.SUCCEEDED


def test_postgres_early_callback_stays_pending_then_converges_without_replay(
    clinic_recall_pg_engine,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_schema(engine)
    effect_id, effect_token = _seed_sms_dispatch(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    dispatch_session = factory()
    lock_dispatching_effect(
        dispatch_session,
        clinic_id=CLINIC_A,
        effect_id=effect_id,
        worker_id="worker-postgres-dispatch",
    )

    def ingest_callback() -> tuple[str, ProviderCallbackState]:
        with factory() as session:
            result = receive_twilio_callback(
                session,
                effect_token=effect_token,
                callback_kind=ProviderCallbackKind.SMS,
                fields=_delivery_fields(),
                raw_payload=b"postgres-early-callback",
                received_at=NOW,
            )
            session.commit()
            return result.receipt_id, result.state

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(ingest_callback)
    try:
        receipt_id, receipt_state = future.result(timeout=5)
    except FutureTimeoutError:
        dispatch_session.rollback()
        future.result(timeout=5)
        pytest.fail("early callback blocked on the in-flight dispatch lock")
    finally:
        executor.shutdown(wait=True)

    assert receipt_state == ProviderCallbackState.PENDING
    settled = mark_succeeded(
        dispatch_session,
        clinic_id=CLINIC_A,
        effect_id=effect_id,
        worker_id="worker-postgres-dispatch",
        now=NOW + timedelta(seconds=1),
        provider_resource_id=MESSAGE_SID,
    )
    assert settled.state == ExternalEffectState.SUCCEEDED
    dispatch_session.commit()
    dispatch_session.close()

    first = reconcile_once(
        factory,
        clinic_id=CLINIC_A,
        worker_id="worker-postgres-reconcile-a",
        now=NOW + timedelta(seconds=2),
        enabled=True,
    )
    second = reconcile_once(
        factory,
        clinic_id=CLINIC_A,
        worker_id="worker-postgres-reconcile-b",
        now=NOW + timedelta(seconds=3),
        enabled=True,
    )
    with factory() as session:
        with clinic_scope(session, CLINIC_A):
            receipt = session.get(ProviderCallbackReceipt, receipt_id)
            effect = session.get(ExternalEffect, effect_id)
            job = session.get(OutreachJob, f"job-{CLINIC_A}")
            assert receipt is not None and receipt.state == ProviderCallbackState.APPLIED
            assert effect is not None
            assert effect.provider_status == "delivery_succeeded"
            assert effect.attempt_count == 1
            assert job is not None and job.state == OutreachState.DELIVERED
    assert first.claimed == 1 and first.applied == 1
    assert second.claimed == 0


def test_postgres_early_amd_converges_with_call_sid_without_replay(
    clinic_recall_pg_engine,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_schema(engine)
    effect_id, effect_token = _seed_call_dispatch(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    dispatch_session = factory()
    lock_dispatching_effect(
        dispatch_session,
        clinic_id=CLINIC_A,
        effect_id=effect_id,
        worker_id="worker-postgres-call-dispatch",
    )

    def ingest_amd() -> tuple[str, ProviderCallbackState]:
        with factory() as session:
            result = receive_twilio_callback(
                session,
                effect_token=effect_token,
                callback_kind=ProviderCallbackKind.AMD,
                fields={"CallSid": CALL_SID, "AnsweredBy": "human"},
                raw_payload=b"postgres-early-human-amd",
                received_at=NOW,
            )
            session.commit()
            return result.receipt_id, result.state

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(ingest_amd)
    try:
        receipt_id, receipt_state = future.result(timeout=5)
    except FutureTimeoutError:
        dispatch_session.rollback()
        future.result(timeout=5)
        pytest.fail("early AMD callback blocked on the in-flight dispatch lock")
    finally:
        executor.shutdown(wait=True)

    assert receipt_state == ProviderCallbackState.PENDING
    settled = mark_succeeded(
        dispatch_session,
        clinic_id=CLINIC_A,
        effect_id=effect_id,
        worker_id="worker-postgres-call-dispatch",
        now=NOW + timedelta(seconds=1),
        provider_resource_id=CALL_SID,
    )
    assert settled.state == ExternalEffectState.SUCCEEDED
    dispatch_session.commit()
    dispatch_session.close()

    first = reconcile_once(
        factory,
        clinic_id=CLINIC_A,
        worker_id="worker-postgres-call-reconcile-a",
        now=NOW + timedelta(seconds=2),
        enabled=True,
    )
    second = reconcile_once(
        factory,
        clinic_id=CLINIC_A,
        worker_id="worker-postgres-call-reconcile-b",
        now=NOW + timedelta(seconds=3),
        enabled=True,
    )
    with factory() as session:
        with clinic_scope(session, CLINIC_A):
            receipt = session.get(ProviderCallbackReceipt, receipt_id)
            effect = session.get(ExternalEffect, effect_id)
            job = session.get(OutreachJob, f"job-{CLINIC_A}")
            assert receipt is not None and receipt.state == ProviderCallbackState.APPLIED
            assert effect is not None
            assert effect.provider_resource_id == CALL_SID
            assert effect.provider_status == "human_confirmed"
            assert effect.attempt_count == 1
            assert job is not None and job.state == OutreachState.NO_REPLY
    assert first.claimed == 1 and first.applied == 1
    assert second.claimed == 0

    with Session(engine, expire_on_commit=False) as session:
        assert claim_effects(
            session,
            clinic_id=CLINIC_A,
            worker_id="worker-postgres-call-after-convergence",
            now=NOW + timedelta(seconds=4),
            lease_for=timedelta(minutes=5),
            effect_types=(ExternalEffectType.CALL,),
        ) == []


def test_postgres_overlapping_reconcilers_apply_receipt_once(
    clinic_recall_pg_engine,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_schema(engine)
    effect_id, effect_token = _seed_sms_dispatch(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        with clinic_scope(session, CLINIC_A):
            effect = session.get(ExternalEffect, effect_id)
            assert effect is not None
            session.add(
                ProviderCallbackReceipt(
                    id="receipt-overlap",
                    clinic_id=CLINIC_A,
                    external_effect_id=effect.id,
                    provider="twilio",
                    callback_kind=ProviderCallbackKind.SMS,
                    deduplication_hash="6" * 64,
                    effect_token_hash="7" * 64,
                    provider_resource_id=MESSAGE_SID,
                    normalized_status="delivered",
                    payload_hash="8" * 64,
                    state=ProviderCallbackState.PENDING,
                    received_at=NOW,
                )
            )
        session.commit()

    start = threading.Barrier(2)

    def run(worker_id: str):
        start.wait()
        return reconcile_once(
            factory,
            clinic_id=CLINIC_A,
            worker_id=worker_id,
            now=NOW,
            enabled=True,
            limit=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("worker-overlap-a", "worker-overlap-b")))

    assert sum(result.claimed for result in results) == 1
    assert sum(result.applied for result in results) == 1
    with factory() as session:
        with clinic_scope(session, CLINIC_A):
            receipt = session.get(ProviderCallbackReceipt, "receipt-overlap")
            assert receipt is not None
            assert receipt.state == ProviderCallbackState.APPLIED
            assert receipt.processing_attempts == 1
