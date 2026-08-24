"""Ordinary-role PostgreSQL proofs for PR-07 durability, RLS, and races."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from src.clinic_recall.availability import AvailabilitySlotInput, upsert_availability_slots
from src.clinic_recall.booking import book_slot
from src.clinic_recall.db import clinic_scope, tenant_select
from src.clinic_recall.durable.cliniko_booking_state import (
    finalize_verified,
    load_dispatch_context,
    preflight_zero_match_hash,
)
from src.clinic_recall.durable.effects import claim_effects, mark_dispatching
from src.clinic_recall.enums import (
    BookingWriteBackState,
    Channel,
    ExternalEffectType,
)
from src.clinic_recall.identity_evidence import IdentityEvidenceService
from src.clinic_recall.models import (
    Appointment,
    Base,
    BookingAction,
    Campaign,
    Clinic,
    ExternalEffect,
    ExternalEffectHandoff,
    OutreachJob,
    Patient,
)
from src.clinic_recall.pilot_controls import PilotGateDecision
from src.clinic_recall.rls import apply_rls_policies, drop_rls_policies
from src.clinic_recall.sync.cliniko_booking import ObservedAppointment

from tests.identity_evidence_support import (
    grant_synthetic_t2,
    synthetic_identity_policy,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
CLINIC_ID = "clinic-pr07-pg"


def _allow_pilot(*_args) -> PilotGateDecision:
    return PilotGateDecision(True, "allowed")


def _identity_service() -> IdentityEvidenceService:
    return IdentityEvidenceService(
        policy=synthetic_identity_policy(),
        clock=lambda: NOW,
        identifier_factory=lambda: "unused-pr07-pg-identity",
        challenge_factory=lambda: "unused-pr07-pg-challenge",
    )


def _drop_rls_safely(connection) -> None:
    savepoint = connection.begin_nested()
    try:
        drop_rls_policies(connection)
    except sa.exc.DBAPIError:
        savepoint.rollback()
    else:
        savepoint.commit()


def _reset(engine) -> None:
    with engine.begin() as connection:
        _drop_rls_safely(connection)
        Base.metadata.drop_all(connection)
        Base.metadata.create_all(connection)
        apply_rls_policies(connection)


def _seed(engine, clinic_id: str = CLINIC_ID) -> tuple[str, str]:
    with Session(engine, expire_on_commit=False) as session:
        session.add(Clinic(id=clinic_id, name="Synthetic PR-07 PostgreSQL Clinic"))
        session.flush()
        with clinic_scope(session, clinic_id):
            session.add(
                Patient(
                    id=f"patient-{clinic_id}",
                    clinic_id=clinic_id,
                    source_ref="900700001",
                    name="Synthetic Patient",
                    phone="+447700900701",
                    consent_flags={"call": True, "sms": True},
                    opt_out_flags={},
                )
            )
            session.flush()
            session.add(
                Appointment(
                    id=f"appointment-{clinic_id}",
                    clinic_id=clinic_id,
                    patient_id=f"patient-{clinic_id}",
                    source_ref="910700001",
                    status="missed",
                    start_at=NOW - timedelta(days=5),
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
                    channel="call",
                    state="no_reply",
                )
            )
            session.flush()
            slot = upsert_availability_slots(
                session,
                clinic_id,
                [
                    AvailabilitySlotInput(
                        source_ref="cliniko:v1:" + "9" * 64,
                        source_provider="cliniko",
                        business_id="920700001",
                        clinician_id="930700001",
                        appointment_type_id="940700001",
                        start_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
                        end_at=datetime(2026, 8, 4, 9, 30, tzinfo=UTC),
                        fetched_at=NOW,
                        expires_at=NOW + timedelta(minutes=10),
                    )
                ],
                now=NOW,
            )[0]
        session.commit()
        return slot.slot_id, f"job-{clinic_id}"


def _book(engine, slot_id: str) -> tuple[str, str]:
    with Session(engine, expire_on_commit=False) as session:
        identity_service, identity_context = grant_synthetic_t2(
            session,
            clinic_id=CLINIC_ID,
            patient_id=f"patient-{CLINIC_ID}",
            channel=Channel.CALL,
            now=NOW,
            suffix=f"pr07-pg-{uuid.uuid4().hex}",
        )
        result = book_slot(
            session,
            CLINIC_ID,
            patient_id=f"patient-{CLINIC_ID}",
            outreach_job_id=f"job-{CLINIC_ID}",
            slot_id=slot_id,
            now=NOW,
            write_back_enabled=True,
            identity_service=identity_service,
            identity_context=identity_context,
        )
        session.commit()
        assert result.booking_action_id is not None
        with clinic_scope(session, CLINIC_ID):
            effect = session.execute(
                tenant_select(ExternalEffect).where(
                    ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
                )
            ).scalar_one()
        return result.booking_action_id, effect.id


def test_pr07_action_effect_and_handoff_are_cross_clinic_denied(
    clinic_recall_pg_engine,
) -> None:
    engine = clinic_recall_pg_engine
    _reset(engine)
    slot_id, _ = _seed(engine)
    action_id, effect_id = _book(engine, slot_id)
    _seed(engine, "clinic-pr07-pg-other")
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_ID):
            session.add(
                ExternalEffectHandoff(
                    id=f"handoff-{uuid.uuid4().hex}",
                    clinic_id=CLINIC_ID,
                    external_effect_id=effect_id,
                    status="queued",
                    reason_code="synthetic_review",
                )
            )
        session.commit()

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, "clinic-pr07-pg-other"):
            assert session.get(BookingAction, action_id) is None
            assert session.get(ExternalEffect, effect_id) is None
            assert session.execute(
                tenant_select(ExternalEffectHandoff)
            ).scalar_one_or_none() is None

    with engine.begin() as connection:
        connection.execute(
            sa.text("SELECT set_config('app.clinic_id', :clinic_id, true)"),
            {"clinic_id": "clinic-pr07-pg-other"},
        )
        for table in ("booking_action", "external_effect", "external_effect_handoff"):
            assert connection.scalar(
                sa.text(f"SELECT count(*) FROM {table} WHERE clinic_id = :clinic_id"),
                {"clinic_id": CLINIC_ID},
            ) == 0


def test_concurrent_same_intent_creates_one_action_and_effect(
    clinic_recall_pg_engine,
) -> None:
    engine = clinic_recall_pg_engine
    _reset(engine)
    slot_id, _ = _seed(engine)
    identity_dependencies = []
    for index in range(2):
        with Session(engine, expire_on_commit=False) as session:
            identity_dependencies.append(
                grant_synthetic_t2(
                    session,
                    clinic_id=CLINIC_ID,
                    patient_id=f"patient-{CLINIC_ID}",
                    channel=Channel.CALL,
                    now=NOW,
                    suffix=f"pr07-pg-race-{index}-{uuid.uuid4().hex}",
                )
            )
            session.commit()
    start = threading.Barrier(2)

    def create(index: int) -> tuple[bool, bool]:
        identity_service, identity_context = identity_dependencies[index]
        with Session(engine, expire_on_commit=False) as session:
            start.wait()
            result = book_slot(
                session,
                CLINIC_ID,
                patient_id=f"patient-{CLINIC_ID}",
                outreach_job_id=f"job-{CLINIC_ID}",
                slot_id=slot_id,
                now=NOW,
                write_back_enabled=True,
                identity_service=identity_service,
                identity_context=identity_context,
            )
            session.commit()
            return result.success, result.idempotent

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))

    assert all(success for success, _idempotent in results)
    assert sum(int(idempotent) for _success, idempotent in results) == 1
    with Session(engine) as session, clinic_scope(session, CLINIC_ID):
        assert session.scalar(sa.select(sa.func.count()).select_from(BookingAction)) == 1
        assert session.scalar(
            sa.select(sa.func.count())
            .select_from(ExternalEffect)
            .where(ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING)
        ) == 1


def test_concurrent_workers_claim_one_cliniko_effect(clinic_recall_pg_engine) -> None:
    engine = clinic_recall_pg_engine
    _reset(engine)
    slot_id, _ = _seed(engine)
    _action_id, effect_id = _book(engine, slot_id)
    start = threading.Barrier(2)

    def claim(worker_id: str) -> list[str]:
        with Session(engine, expire_on_commit=False) as session:
            start.wait()
            effects = claim_effects(
                session,
                clinic_id=CLINIC_ID,
                worker_id=worker_id,
                now=NOW,
                lease_for=timedelta(minutes=5),
                effect_types=(ExternalEffectType.CLINIKO_BOOKING,),
            )
            ids = [effect.id for effect in effects]
            session.commit()
            return ids

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("pr07-worker-a", "pr07-worker-b")))

    assert sum(len(items) for items in claims) == 1
    assert {item for items in claims for item in items} == {effect_id}


def test_competing_finalizers_verify_once_and_release_one_confirmation(
    clinic_recall_pg_engine,
) -> None:
    engine = clinic_recall_pg_engine
    _reset(engine)
    slot_id, _ = _seed(engine)
    action_id, effect_id = _book(engine, slot_id)
    with Session(engine, expire_on_commit=False) as session:
        claimed = claim_effects(
            session,
            clinic_id=CLINIC_ID,
            worker_id="pr07-finalizer-owner",
            now=NOW,
            lease_for=timedelta(minutes=5),
            effect_types=(ExternalEffectType.CLINIKO_BOOKING,),
        )
        assert [effect.id for effect in claimed] == [effect_id]
        mark_dispatching(
            session,
            clinic_id=CLINIC_ID,
            effect_id=effect_id,
            worker_id="pr07-finalizer-owner",
            now=NOW,
        )
        with clinic_scope(session, CLINIC_ID):
            action = session.get(BookingAction, action_id)
            assert action is not None
            action.write_back_state = BookingWriteBackState.DISPATCHING
            action.provider_attempted_at = NOW
            effect = session.get(ExternalEffect, effect_id)
            assert effect is not None
            effect.preflight_evidence_hash = preflight_zero_match_hash(
                effect.request_hash
            )
            context = load_dispatch_context(
                session,
                clinic_id=CLINIC_ID,
                effect_id=effect_id,
                now=NOW,
                programme_gate=_allow_pilot,
                identity_service=_identity_service(),
            )
        session.commit()
    observed = ObservedAppointment(
        provider_id="950700001",
        signature=context.expected,
        active=True,
        updated_at=NOW,
    )
    start = threading.Barrier(2)

    def finalize() -> bool:
        with Session(engine, expire_on_commit=False) as session:
            start.wait()
            changed = finalize_verified(
                session,
                clinic_id=CLINIC_ID,
                context=context,
                observed=observed,
                now=NOW,
                programme_gate=_allow_pilot,
                confirmation_release_enabled=True,
                identity_service=_identity_service(),
            )
            session.commit()
            return changed

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: finalize(), range(2)))

    assert sorted(outcomes) == [False, True]
    with Session(engine) as session, clinic_scope(session, CLINIC_ID):
        action = session.get(BookingAction, action_id)
        assert action is not None
        assert action.write_back_state == BookingWriteBackState.VERIFIED
        assert action.written_back is True
        assert session.scalar(
            sa.select(sa.func.count())
            .select_from(ExternalEffect)
            .where(ExternalEffect.effect_type == ExternalEffectType.SMS)
        ) == 1