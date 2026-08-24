"""Ordinary-role PostgreSQL proof for the PR-10 rights workflow."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from src.clinic_recall.db import clinic_scope
from src.clinic_recall.durable.effects import claim_effects
from src.clinic_recall.enums import (
    ExternalEffectState,
    ExternalEffectType,
    RightsTargetAction,
    RightsTargetOwnerType,
    RightsTargetResource,
    RightsTargetState,
    RightsTargetSystem,
)
from src.clinic_recall.models import (
    Appointment,
    Campaign,
    Clinic,
    ExternalEffect,
    Interaction,
    OutreachJob,
    Patient,
    RightsRequest,
    RightsTarget,
)
from src.clinic_recall.retention import RetentionPolicy, schedule_retention_requests
from src.clinic_recall.rights import (
    ResidualApproval,
    RightsPolicy,
    SubjectKey,
    SubjectKeyring,
    apply_residual_approvals,
    complete_patient_erasure,
    request_patient_erasure,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
CLINIC_A = "clinic-rights-pg-a"
CLINIC_B = "clinic-rights-pg-b"
KEYRING = SubjectKeyring(
    current=SubjectKey(version="tests-pg-v1", secret=b"tests-postgres-rights-key"),
)
POLICY = RightsPolicy(
    version="tests-pg-policy-v1",
    approval_evidence_hash="a" * 64,
    request_due_after=timedelta(days=28),
)
RETENTION_POLICY = RetentionPolicy(
    version="tests-pg-retention-policy-v1",
    approval_evidence_hash="b" * 64,
    approved_at=NOW - timedelta(days=2),
    effective_at=NOW - timedelta(days=1),
    expires_at=NOW + timedelta(days=90),
    retain_for=timedelta(days=40),
    request_due_after=timedelta(days=7),
)


def _reset_to_0019(engine, monkeypatch) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(sa.text("DROP SCHEMA public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))
    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        engine.url.render_as_string(hide_password=False),
    )
    config = Config("infra/postgres/alembic.ini")
    command.upgrade(config, "0018_recording_consent_ledger")
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE IF EXISTS rights_target CASCADE"))
        connection.execute(sa.text("DROP TABLE IF EXISTS rights_request CASCADE"))
        for enum_name in (
            "rights_residual_category",
            "rights_target_state",
            "rights_target_owner_type",
            "rights_target_action",
            "rights_target_resource",
            "rights_target_system",
            "rights_request_state",
            "rights_request_kind",
        ):
            connection.execute(sa.text(f"DROP TYPE IF EXISTS {enum_name}"))
    command.upgrade(config, "0019_rights_retention_purge")


def _seed_subjects(engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            session.add(Clinic(id=CLINIC_A, name="Rights PostgreSQL A"))
        session.commit()
        with clinic_scope(session, CLINIC_B):
            session.add(Clinic(id=CLINIC_B, name="Rights PostgreSQL B"))
        session.commit()
        with clinic_scope(session, CLINIC_A):
            session.add(
                Patient(
                    id="patient-rights-pg-a",
                    clinic_id=CLINIC_A,
                    source_ref="source-rights-pg-a",
                    name="Synthetic Rights A",
                )
            )
        session.commit()
        with clinic_scope(session, CLINIC_B):
            session.add(
                Patient(
                    id="patient-rights-pg-b",
                    clinic_id=CLINIC_B,
                    source_ref="source-rights-pg-b",
                    name="Synthetic Rights B",
                )
            )
        session.commit()


def _request_for_a(engine) -> str:
    with Session(engine, expire_on_commit=False) as session:
        result = request_patient_erasure(
            session,
            clinic_id=CLINIC_A,
            patient_id="patient-rights-pg-a",
            confirm_token="ERASE patient-rights-pg-a",
            request_identity="tests-pg-request-a",
            actor_role="dpo",
            actor_reference="tests-pg-operator",
            keyring=KEYRING,
            policy=POLICY,
            now=NOW,
        )
        session.commit()
        return result.request_id


def test_postgres_0019_forces_rls_and_refuses_evidence_downgrade(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_0019(engine, monkeypatch)
    _seed_subjects(engine)
    request_id = _request_for_a(engine)

    with engine.connect() as connection:
        role = connection.execute(
            sa.text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
        rls = connection.execute(
            sa.text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname IN ('rights_request', 'rights_target') "
                "ORDER BY relname"
            )
        ).all()
        policies = connection.execute(
            sa.text(
                "SELECT tablename, policyname FROM pg_policies "
                "WHERE tablename IN ('rights_request', 'rights_target') ORDER BY tablename"
            )
        ).all()
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))

    assert tuple(role) == (False, False)
    assert [tuple(row) for row in rls] == [
        ("rights_request", True, True),
        ("rights_target", True, True),
    ]
    assert [tuple(row) for row in policies] == [
        ("rights_request", "rights_request_tenant_isolation"),
        ("rights_target", "rights_target_tenant_isolation"),
    ]
    assert revision == "0019_rights_retention_purge"

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_B):
            assert session.execute(sa.text("SELECT id FROM rights_request")).all() == []
        session.rollback()
        with pytest.raises(DBAPIError):
            with clinic_scope(session, CLINIC_B):
                session.add(
                    RightsTarget(
                        id="rights-target-cross-tenant",
                        clinic_id=CLINIC_A,
                        request_id=request_id,
                        system=RightsTargetSystem.LOCAL,
                        resource=RightsTargetResource.PATIENT_GRAPH,
                        action=RightsTargetAction.MINIMIZE,
                        owner_type=RightsTargetOwnerType.RIGHTS_REQUEST,
                        owner_id=request_id,
                        target_key_hash="b" * 64,
                        state=RightsTargetState.REQUESTED,
                        available_at=NOW,
                        due_at=NOW + timedelta(days=28),
                    )
                )
                session.flush()
        session.rollback()

    with pytest.raises(RuntimeError, match="rollback by disabling rights dispatch"):
        command.downgrade(
            Config("infra/postgres/alembic.ini"),
            "0018_recording_consent_ledger",
        )


def test_postgres_completion_settles_local_target_before_request_guard(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_0019(engine, monkeypatch)
    _seed_subjects(engine)
    request_id = _request_for_a(engine)

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            residual_targets = list(
                session.execute(
                    sa.select(RightsTarget).where(
                        RightsTarget.clinic_id == CLINIC_A,
                        RightsTarget.request_id == request_id,
                        RightsTarget.state == RightsTargetState.RESIDUAL,
                    )
                ).scalars()
            )
        approvals = {
            target.residual_category: ResidualApproval(
                category=target.residual_category,
                policy_version="tests-pg-residual-policy-v1",
                approval_evidence_hash="b" * 64,
                due_at=NOW + timedelta(days=90),
                completion_eligible=True,
            )
            for target in residual_targets
            if target.residual_category is not None
        }
        apply_residual_approvals(
            session,
            clinic_id=CLINIC_A,
            request_id=request_id,
            approvals=approvals,
            now=NOW,
            actor_role="dpo",
        )
        status = complete_patient_erasure(
            session,
            clinic_id=CLINIC_A,
            request_id=request_id,
            keyring=KEYRING,
            now=NOW,
            actor_role="dpo",
        )
        session.commit()

        assert status.state.value == "completed"
        with clinic_scope(session, CLINIC_A):
            assert session.get(Patient, "patient-rights-pg-a") is None


def test_postgres_rights_guards_block_unsafe_direct_sql(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_0019(engine, monkeypatch)
    _seed_subjects(engine)
    request_id = _request_for_a(engine)

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            session.add(
                ExternalEffect(
                    id="effect-rights-owner-pg",
                    clinic_id=CLINIC_A,
                    aggregate_type="outreach_job",
                    aggregate_id="job-rights-owner-pg",
                    effect_type=ExternalEffectType.SMS,
                    idempotency_key="tests-rights-owner-pg",
                    payload={"intent": "recall", "outreach_job_id": "job-rights-owner-pg"},
                    request_hash="c" * 64,
                    state=ExternalEffectState.SUCCEEDED,
                    available_at=NOW,
                    provider_resource_id="SM" + "1" * 32,
                    completed_at=NOW,
                )
            )
            prerequisite = RightsTarget(
                id="rights-target-prerequisite-pg",
                clinic_id=CLINIC_A,
                request_id=request_id,
                system=RightsTargetSystem.LOCAL,
                resource=RightsTargetResource.PATIENT_GRAPH,
                action=RightsTargetAction.MINIMIZE,
                owner_type=RightsTargetOwnerType.RIGHTS_REQUEST,
                owner_id=request_id,
                target_key_hash="d" * 64,
                state=RightsTargetState.REQUESTED,
                available_at=NOW,
                due_at=NOW + timedelta(days=28),
            )
            session.add(prerequisite)
            session.add(
                RightsTarget(
                    id="rights-target-dependent-pg",
                    clinic_id=CLINIC_A,
                    request_id=request_id,
                    system=RightsTargetSystem.TWILIO,
                    resource=RightsTargetResource.MESSAGE,
                    action=RightsTargetAction.DELETE,
                    owner_type=RightsTargetOwnerType.EXTERNAL_EFFECT,
                    owner_id="effect-rights-owner-pg",
                    target_key_hash="e" * 64,
                    prerequisite_target_id=prerequisite.id,
                    state=RightsTargetState.REQUESTED,
                    available_at=NOW,
                    due_at=NOW + timedelta(days=28),
                )
            )
        session.commit()

    with Session(engine, expire_on_commit=False) as session:
        with pytest.raises(DBAPIError):
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "UPDATE rights_request SET state = 'completed', "
                        "inventory_finalized_at = :now, completed_at = :now WHERE id = :id"
                    ),
                    {"now": NOW, "id": request_id},
                )
        session.rollback()

        with pytest.raises(DBAPIError):
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "UPDATE rights_target SET state = 'dispatching' "
                        "WHERE id = 'rights-target-dependent-pg'"
                    )
                )
        session.rollback()

        with pytest.raises(DBAPIError):
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "UPDATE external_effect SET provider_resource_id = NULL "
                        "WHERE id = 'effect-rights-owner-pg'"
                    )
                )
        session.rollback()

        with clinic_scope(session, CLINIC_A):
            session.execute(
                sa.text(
                    "UPDATE rights_target SET state = 'residual', "
                    "residual_category = 'provider_backup_window', "
                    "residual_policy_version = 'tests-residual-v1', "
                    "residual_approval_evidence_hash = :digest, "
                    "residual_completion_eligible = false, residual_due_at = :due_at "
                    "WHERE id = 'rights-target-dependent-pg'"
                ),
                {"digest": "f" * 64, "due_at": NOW + timedelta(days=28)},
            )
        session.commit()
        with pytest.raises(DBAPIError):
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "UPDATE external_effect SET provider_resource_id = NULL "
                        "WHERE id = 'effect-rights-owner-pg'"
                    )
                )
        session.rollback()

        with clinic_scope(session, CLINIC_A):
            session.execute(
                sa.text(
                    "UPDATE rights_target SET residual_completion_eligible = true, "
                    "residual_due_at = :due_at "
                    "WHERE id = 'rights-target-dependent-pg'"
                ),
                {"due_at": NOW - timedelta(days=1)},
            )
        session.commit()
        with pytest.raises(DBAPIError):
            with clinic_scope(session, CLINIC_A):
                session.execute(
                    sa.text(
                        "UPDATE external_effect SET provider_resource_id = NULL "
                        "WHERE id = 'effect-rights-owner-pg'"
                    )
                )
        session.rollback()

        with clinic_scope(session, CLINIC_A):
            session.execute(
                sa.text(
                    "UPDATE rights_target SET state = 'requested', "
                    "residual_category = NULL, residual_policy_version = NULL, "
                    "residual_approval_evidence_hash = NULL, "
                    "residual_completion_eligible = false, residual_due_at = NULL "
                    "WHERE id = 'rights-target-dependent-pg'"
                )
            )
        session.commit()

        with clinic_scope(session, CLINIC_A):
            session.execute(
                sa.text(
                    "UPDATE rights_target SET state = 'verified', verified_at = :now "
                    "WHERE id = 'rights-target-prerequisite-pg'"
                ),
                {"now": NOW},
            )
            session.execute(
                sa.text(
                    "UPDATE rights_target SET state = 'dispatching' "
                    "WHERE id = 'rights-target-dependent-pg'"
                )
            )
        session.commit()

    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            request = session.get(RightsRequest, request_id)
            assert request is not None
            assert request.state.value == "frozen"
            assert session.get(ExternalEffect, "effect-rights-owner-pg").provider_resource_id == (
                "SM" + "1" * 32
            )


def test_postgres_concurrent_retention_schedulers_converge_and_effect_claims_once(
    clinic_recall_pg_engine,
    monkeypatch,
) -> None:
    engine = clinic_recall_pg_engine
    _reset_to_0019(engine, monkeypatch)
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            session.add(Clinic(id=CLINIC_A, name="Rights PostgreSQL A"))
        session.commit()
        with clinic_scope(session, CLINIC_A):
            session.add(
                Patient(
                    id="patient-retention-pg-a",
                    clinic_id=CLINIC_A,
                    source_ref="source-retention-pg-a",
                    name="Synthetic Retention A",
                )
            )
            session.add(
                Campaign(
                    id="campaign-retention-pg-a",
                    clinic_id=CLINIC_A,
                    type="recovery",
                    status="active",
                )
            )
            session.flush()
            session.add(
                Appointment(
                    id="appointment-retention-pg-a",
                    clinic_id=CLINIC_A,
                    patient_id="patient-retention-pg-a",
                    source_ref="appointment-retention-pg-a",
                    status="missed",
                    start_at=NOW - timedelta(days=41),
                )
            )
            session.flush()
            session.add(
                OutreachJob(
                    id="job-retention-pg-a",
                    clinic_id=CLINIC_A,
                    campaign_id="campaign-retention-pg-a",
                    patient_id="patient-retention-pg-a",
                    appointment_id="appointment-retention-pg-a",
                    channel="sms",
                    state="sent",
                )
            )
            session.flush()
            session.add(
                Interaction(
                    id="interaction-retention-pg-a",
                    clinic_id=CLINIC_A,
                    outreach_job_id="job-retention-pg-a",
                    channel="sms",
                    direction="inbound",
                    content="synthetic retention concurrency content",
                    outcome="auto_handled",
                    occurred_at=NOW - timedelta(days=40),
                )
            )
        session.commit()

    scheduler_start = threading.Barrier(2)

    def schedule() -> tuple[int, int]:
        with Session(engine, expire_on_commit=False) as session:
            scheduler_start.wait()
            result = schedule_retention_requests(
                session,
                clinic_id=CLINIC_A,
                keyring=KEYRING,
                policy=RETENTION_POLICY,
                now=NOW,
                enabled=True,
            )
            session.commit()
            return result.created_count, result.existing_count

    with ThreadPoolExecutor(max_workers=2) as pool:
        schedule_results = list(pool.map(lambda _: schedule(), range(2)))

    assert sum(created for created, _ in schedule_results) == 1
    assert sum(existing for _, existing in schedule_results) == 1
    with Session(engine, expire_on_commit=False) as session:
        with clinic_scope(session, CLINIC_A):
            assert session.scalar(
                sa.select(sa.func.count()).select_from(RightsRequest).where(
                    RightsRequest.kind == "retention"
                )
            ) == 1
            assert session.scalar(
                sa.select(sa.func.count()).select_from(RightsTarget).where(
                    RightsTarget.resource == "interaction_content"
                )
            ) == 1
            effect_id = session.scalar(
                sa.select(ExternalEffect.id).where(
                    ExternalEffect.effect_type == ExternalEffectType.RIGHTS
                )
            )
            assert effect_id is not None

    claim_start = threading.Barrier(2)

    def claim(worker_id: str) -> list[str]:
        with Session(engine, expire_on_commit=False) as session:
            claim_start.wait()
            effects = claim_effects(
                session,
                clinic_id=CLINIC_A,
                worker_id=worker_id,
                now=NOW,
                lease_for=timedelta(minutes=5),
                effect_types=(ExternalEffectType.RIGHTS,),
            )
            claimed_ids = [effect.id for effect in effects]
            session.commit()
            return claimed_ids

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("rights-worker-a", "rights-worker-b")))

    assert sum(len(worker_claims) for worker_claims in claims) == 1
    assert {item for worker_claims in claims for item in worker_claims} == {effect_id}