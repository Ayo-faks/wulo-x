"""Database-layer per-clinic isolation tests via PostgreSQL row-level security.

These are the highest-priority safety tests for Phase 1: they prove that, at the
database layer (independent of any application filter), a clinic can neither read
nor write another clinic's rows. They require a real PostgreSQL server supplied
via ``CLINIC_RECALL_TEST_DSN`` and connected as a NON-superuser role (superusers
bypass RLS). When no DSN is set the whole module skips, keeping the suite green
in environments without PostgreSQL.

Run locally with, e.g.::

    CLINIC_RECALL_TEST_DSN=postgresql+psycopg://user:pass@host:5432/db \
        pytest -m postgres tests/test_clinic_recall_rls_isolation.py
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from src.clinic_recall.db import clinic_scope
from src.clinic_recall.enums import ExternalEffectState, ExternalEffectType
from src.clinic_recall.models import Base, Clinic, ExternalEffect, Patient
from src.clinic_recall.rls import apply_rls_policies, drop_rls_policies

pytestmark = pytest.mark.postgres

CLINIC_A = "clinic-rls-a"
CLINIC_B = "clinic-rls-b"


@pytest.fixture
def rls_db(clinic_recall_pg_engine):
    """Provision a clean schema with RLS and two clinics, then tear it down."""
    engine = clinic_recall_pg_engine
    with engine.begin() as conn:
        drop_rls_policies_safely(conn)
        Base.metadata.drop_all(conn)
        Base.metadata.create_all(conn)
        apply_rls_policies(conn)
    # Clinics live in the (non-RLS) clinic table.
    with Session(engine, expire_on_commit=False) as session:
        session.add(Clinic(id=CLINIC_A, name="A"))
        session.add(Clinic(id=CLINIC_B, name="B"))
        session.commit()
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            drop_rls_policies_safely(conn)
            Base.metadata.drop_all(conn)


def drop_rls_policies_safely(conn) -> None:
    """Drop policies, ignoring the case where tables do not yet exist."""
    savepoint = conn.begin_nested()
    try:
        drop_rls_policies(conn)
    except DBAPIError:
        savepoint.rollback()
    else:
        savepoint.commit()


@contextmanager
def scoped(engine, clinic_id: str):
    """A short-lived session scoped to ``clinic_id`` (RLS + app layer)."""
    session = Session(engine, expire_on_commit=False)
    try:
        with clinic_scope(session, clinic_id):
            yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _new_patient(clinic_id: str, patient_id: str) -> Patient:
    return Patient(
        id=patient_id,
        clinic_id=clinic_id,
        source_ref=patient_id,
        name=patient_id,
        phone="+447700900001",
        consent_flags={"sms": True},
        opt_out_flags={},
    )


def _new_external_effect(clinic_id: str, effect_id: str) -> ExternalEffect:
    return ExternalEffect(
        id=effect_id,
        clinic_id=clinic_id,
        aggregate_type="outreach_job",
        aggregate_id="job-internal-001",
        effect_type=ExternalEffectType.SMS,
        idempotency_key=f"recall-sms:{effect_id}",
        payload_version=1,
        payload={"intent": "recall", "outreach_job_id": "job-internal-001"},
        request_hash="0" * 64,
        state=ExternalEffectState.PENDING,
        available_at=datetime.now(UTC),
        max_attempts=3,
    )


def test_insert_for_foreign_clinic_is_rejected(rls_db):
    # Scoped to clinic A, inserting a row tagged clinic B violates WITH CHECK.
    with pytest.raises(DBAPIError):
        with scoped(rls_db, CLINIC_A) as session:
            session.add(_new_patient(CLINIC_B, "intruder"))
            session.flush()


def test_reads_are_isolated_at_the_database_layer(rls_db):
    with scoped(rls_db, CLINIC_A) as session:
        session.add(_new_patient(CLINIC_A, "pa1"))
    with scoped(rls_db, CLINIC_B) as session:
        session.add(_new_patient(CLINIC_B, "pb1"))

    # A raw, unfiltered SELECT still only sees the active clinic's rows (RLS).
    with scoped(rls_db, CLINIC_A) as session:
        ids = {row[0] for row in session.execute(text("SELECT id FROM patient")).all()}
        assert ids == {"pa1"}
    with scoped(rls_db, CLINIC_B) as session:
        ids = {row[0] for row in session.execute(text("SELECT id FROM patient")).all()}
        assert ids == {"pb1"}


def test_cross_clinic_update_affects_zero_rows(rls_db):
    with scoped(rls_db, CLINIC_A) as session:
        session.add(_new_patient(CLINIC_A, "pa1"))

    # Clinic B cannot even see clinic A's row, so the UPDATE matches nothing.
    with scoped(rls_db, CLINIC_B) as session:
        result = session.execute(
            text("UPDATE patient SET name = 'hijacked' WHERE id = :id"),
            {"id": "pa1"},
        )
        assert result.rowcount == 0

    # Confirm clinic A's row is untouched.
    with scoped(rls_db, CLINIC_A) as session:
        name = session.execute(text("SELECT name FROM patient WHERE id = 'pa1'")).scalar_one()
        assert name == "pa1"


def test_unscoped_access_returns_nothing(rls_db):
    with scoped(rls_db, CLINIC_A) as session:
        session.add(_new_patient(CLINIC_A, "pa1"))

    # With no app.clinic_id set, current_setting(...) is NULL -> policy denies all.
    with Session(rls_db, expire_on_commit=False) as session:
        rows = session.execute(text("SELECT id FROM patient")).all()
        assert rows == []


def test_external_effect_rows_fail_closed_across_clinics(rls_db):
    with scoped(rls_db, CLINIC_A) as session:
        session.add(_new_external_effect(CLINIC_A, "effect-a"))

    with scoped(rls_db, CLINIC_B) as session:
        rows = session.execute(text("SELECT id FROM external_effect")).all()
        assert rows == []

    with pytest.raises(DBAPIError):
        with scoped(rls_db, CLINIC_B) as session:
            session.add(_new_external_effect(CLINIC_A, "effect-intruder"))
            session.flush()
