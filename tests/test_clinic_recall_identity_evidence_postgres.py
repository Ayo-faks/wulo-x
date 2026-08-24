"""Ordinary-owner PostgreSQL proofs for PR-11 identity evidence."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from src.clinic_recall.availability import AvailabilitySlotInput, upsert_availability_slots
from src.clinic_recall.booking import book_inbound_slot
from src.clinic_recall.db import clinic_scope
from src.clinic_recall.enums import (
    Channel,
    IdentityEvidenceReason,
    IdentityEvidenceState,
    IdentityTier,
)
from src.clinic_recall.identity_evidence import IdentityAction, IdentityEvidenceService
from src.clinic_recall.models import (
    BookingAction,
    Clinic,
    IdentityEvidence,
    IdentityFactorAttempt,
    Patient,
)

from tests.identity_evidence_support import grant_synthetic_t2, synthetic_identity_policy

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
CLINIC_A = "clinic-identity-pg-a"
CLINIC_B = "clinic-identity-pg-b"
PATIENT_A = "patient-identity-pg-a"


def _reset_to_0023(engine, monkeypatch) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(sa.text("DROP SCHEMA public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))
    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        engine.url.render_as_string(hide_password=False),
    )
    command.upgrade(Config("infra/postgres/alembic.ini"), "0023_identity_evidence_tiers")


def _service() -> IdentityEvidenceService:
    identifiers = iter(("identity-evidence-pg-a",))
    challenges = iter(("identity-challenge-pg-a",))
    return IdentityEvidenceService(
        policy=synthetic_identity_policy(),
        clock=lambda: NOW,
        identifier_factory=lambda: next(identifiers),
        challenge_factory=lambda: next(challenges),
    )


def _seed_identity(engine) -> str:
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            session.add(Clinic(id=CLINIC_A, name="Synthetic Identity Clinic A"))
        session.commit()
        with clinic_scope(session, CLINIC_B):
            session.add(Clinic(id=CLINIC_B, name="Synthetic Identity Clinic B"))
        session.commit()
        with clinic_scope(session, CLINIC_A):
            session.add(
                Patient(
                    id=PATIENT_A,
                    clinic_id=CLINIC_A,
                    source_ref="synthetic-identity-pg-a",
                    name="Synthetic Identity Patient A",
                    consent_flags={"sms": True},
                    opt_out_flags={},
                )
            )
        session.commit()
        started = _service().begin(
            session,
            clinic_id=CLINIC_A,
            session_id="identity-session-pg-a",
            route_id="identity-route-pg-a",
            channel=Channel.SMS,
            patient_id=PATIENT_A,
            route_possession=True,
        )
        session.commit()
        assert started.evidence_id is not None
        return started.evidence_id


def test_postgres_0023_forces_rls_and_refuses_retained_evidence_downgrade(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_0023(engine, monkeypatch)
    evidence_id = _seed_identity(engine)

    with engine.connect() as connection:
        role = connection.execute(
            sa.text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
        rls = connection.execute(
            sa.text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname IN "
                "('identity_evidence', 'identity_factor_attempt') ORDER BY relname"
            )
        ).all()
        policies = connection.execute(
            sa.text(
                "SELECT tablename, policyname FROM pg_policies WHERE tablename IN "
                "('identity_evidence', 'identity_factor_attempt') ORDER BY tablename"
            )
        ).all()

    assert tuple(role) == (False, False)
    assert [tuple(row) for row in rls] == [
        ("identity_evidence", True, True),
        ("identity_factor_attempt", True, True),
    ]
    assert [tuple(row) for row in policies] == [
        ("identity_evidence", "identity_evidence_tenant_isolation"),
        ("identity_factor_attempt", "identity_factor_attempt_tenant_isolation"),
    ]

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_B):
            assert session.get(IdentityEvidence, evidence_id) is None
        session.rollback()
        with pytest.raises(DBAPIError):
            with clinic_scope(session, CLINIC_B):
                session.add(
                    IdentityEvidence(
                        id="identity-evidence-cross-tenant",
                        clinic_id=CLINIC_A,
                        session_key_hash="a" * 64,
                        route_key_hash="b" * 64,
                        patient_key_hash="c" * 64,
                        channel=Channel.SMS,
                        policy_version="synthetic-test-policy-v1",
                        tier=IdentityTier.T0,
                        state=IdentityEvidenceState.ACTIVE,
                        reason=IdentityEvidenceReason.ROUTE_ONLY,
                        matched_factor_count=0,
                        dob_verified=False,
                        attempt_count=0,
                        max_attempts=3,
                        issued_at=NOW,
                        expires_at=NOW + timedelta(minutes=5),
                        challenge_token_hash="d" * 64,
                        revision=0,
                    )
                )
                session.flush()
        session.rollback()

    with pytest.raises(RuntimeError, match="revoke identity evidence before downgrade"):
        command.downgrade(
            Config("infra/postgres/alembic.ini"),
            "0022_cliniko_booking_effect",
        )


def test_postgres_concurrent_duplicate_promotion_records_once_then_revokes_replay(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_0023(engine, monkeypatch)
    evidence_id = _seed_identity(engine)
    attempt_ids = iter(("identity-attempt-pg-1", "identity-attempt-pg-2"))
    service = IdentityEvidenceService(
        policy=synthetic_identity_policy(),
        clock=lambda: NOW,
        identifier_factory=lambda: next(attempt_ids),
        challenge_factory=lambda: "identity-challenge-pg-next",
    )
    start = threading.Barrier(2)

    def promote_once():
        with Session(engine, expire_on_commit=False) as session:
            start.wait()
            decision = service.record_factor_result(
                session,
                clinic_id=CLINIC_A,
                evidence_id=evidence_id,
                session_id="identity-session-pg-a",
                route_id="identity-route-pg-a",
                channel=Channel.SMS,
                patient_id=PATIENT_A,
                challenge_token="identity-challenge-pg-a",
                factor_type="full_name",
                matched=True,
                uncertain=False,
            )
            session.commit()
            return decision

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = [future.result() for future in (pool.submit(promote_once), pool.submit(promote_once))]

    assert {decision.reason for decision in decisions} == {
        IdentityEvidenceReason.MATCHED,
        IdentityEvidenceReason.REPLAYED,
    }
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            evidence = session.get(IdentityEvidence, evidence_id)
            attempt_count = session.scalar(
                select(func.count()).select_from(IdentityFactorAttempt)
            )
        assert evidence is not None
        assert evidence.state == IdentityEvidenceState.REVOKED
        assert evidence.tier == IdentityTier.T0
        assert evidence.reason == IdentityEvidenceReason.REPLAYED
        assert attempt_count == 1


def test_postgres_booking_revocation_race_never_leaves_dispatchable_action(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_0023(engine, monkeypatch)
    _seed_identity(engine)
    with Session(engine, expire_on_commit=False) as session:
        slot_id = upsert_availability_slots(
            session,
            CLINIC_A,
            [
                AvailabilitySlotInput(
                    source_ref="synthetic-identity-race-slot",
                    source_provider="cliniko",
                    business_id="synthetic-business",
                    appointment_type_id="synthetic-appointment-type",
                    clinician_id="synthetic-clinician",
                    start_at=NOW + timedelta(days=1),
                    end_at=NOW + timedelta(days=1, minutes=30),
                    fetched_at=NOW,
                    expires_at=NOW + timedelta(minutes=10),
                )
            ],
            now=NOW,
        )[0].slot_id
        identity_service, identity_context = grant_synthetic_t2(
            session,
            clinic_id=CLINIC_A,
            patient_id=PATIENT_A,
            channel=Channel.SMS,
            now=NOW,
            suffix="postgres-action-race",
        )
        session.commit()

    start = threading.Barrier(2)

    def create_action():
        with Session(engine, expire_on_commit=False) as session:
            start.wait()
            result = book_inbound_slot(
                session,
                CLINIC_A,
                patient_id=PATIENT_A,
                slot_id=slot_id,
                now=NOW,
                action_type="book",
                identity_service=identity_service,
                identity_context=identity_context,
            )
            session.commit()
            return result

    def revoke_evidence():
        with Session(engine, expire_on_commit=False) as session:
            start.wait()
            decision = identity_service.revoke(
                session,
                clinic_id=CLINIC_A,
                evidence_id=identity_context.evidence_id,
                reason=IdentityEvidenceReason.REVOKED,
            )
            session.commit()
            return decision

    with ThreadPoolExecutor(max_workers=2) as pool:
        booking_future = pool.submit(create_action)
        revoke_future = pool.submit(revoke_evidence)
        booking_result = booking_future.result()
        revoke_future.result()

    assert booking_result.success or booking_result.error == "identity_t2_required"
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            evidence = session.get(IdentityEvidence, identity_context.evidence_id)
            actions = list(session.execute(select(BookingAction)).scalars())
        assert evidence is not None
        assert evidence.state == IdentityEvidenceState.REVOKED
        assert evidence.tier == IdentityTier.T0
        assert len(actions) <= 1
        if actions:
            action = actions[0]
            assert action.identity_evidence_id == identity_context.evidence_id
            assert identity_service.authorize_bound_action(
                session,
                clinic_id=CLINIC_A,
                evidence_id=action.identity_evidence_id,
                evidence_policy_version=action.identity_policy_version,
                evidence_revision=action.identity_evidence_revision,
                patient_id=PATIENT_A,
                channel=Channel.SMS,
                action=IdentityAction.PROVIDER_EFFECT,
            ).allowed is False