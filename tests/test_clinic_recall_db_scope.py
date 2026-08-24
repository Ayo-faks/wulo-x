"""Application-layer per-clinic scoping tests (SQLite, runs everywhere).

These prove the app-layer half of the isolation invariant: ``tenant_select``
only ever returns rows for the clinic bound by ``clinic_scope``, and tenant
access without a scope fails closed. The database-layer half (RLS) is proven by
``test_clinic_recall_rls_isolation.py`` against real PostgreSQL.
"""

from __future__ import annotations

import pytest
from src.clinic_recall.db import clinic_scope, current_clinic_id, tenant_select
from src.clinic_recall.models import Clinic, Patient


def _add_clinic(session, clinic_id: str) -> None:
    session.add(Clinic(id=clinic_id, name=clinic_id))
    session.flush()


def _add_patient(session, clinic_id: str, patient_id: str) -> None:
    session.add(
        Patient(
            id=patient_id,
            clinic_id=clinic_id,
            source_ref=patient_id,
            name=patient_id,
            phone="+447700900001",
            consent_flags={"sms": True},
            opt_out_flags={},
        )
    )
    session.flush()


def test_current_clinic_id_requires_active_scope():
    with pytest.raises(LookupError):
        current_clinic_id()


def test_clinic_scope_sets_and_resets(sqlite_session):
    with clinic_scope(sqlite_session, "clinic-a"):
        assert current_clinic_id() == "clinic-a"
    # Scope is reset on exit -> fail closed again.
    with pytest.raises(LookupError):
        current_clinic_id()


def test_tenant_select_filters_to_the_active_clinic(sqlite_session):
    session = sqlite_session
    _add_clinic(session, "clinic-a")
    _add_clinic(session, "clinic-b")
    _add_patient(session, "clinic-a", "pa1")
    _add_patient(session, "clinic-a", "pa2")
    _add_patient(session, "clinic-b", "pb1")

    with clinic_scope(session, "clinic-a"):
        rows = session.execute(tenant_select(Patient)).scalars().all()
        assert {r.id for r in rows} == {"pa1", "pa2"}

    with clinic_scope(session, "clinic-b"):
        rows = session.execute(tenant_select(Patient)).scalars().all()
        assert {r.id for r in rows} == {"pb1"}


def test_tenant_select_scopes_clinic_table_by_id(sqlite_session):
    session = sqlite_session
    _add_clinic(session, "clinic-a")
    _add_clinic(session, "clinic-b")
    with clinic_scope(session, "clinic-a"):
        rows = session.execute(tenant_select(Clinic)).scalars().all()
        assert {r.id for r in rows} == {"clinic-a"}


def test_tenant_select_without_scope_raises(sqlite_session):
    with pytest.raises(LookupError):
        tenant_select(Patient)
