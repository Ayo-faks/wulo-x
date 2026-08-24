"""Migration contracts for the PR-10 durable rights workflow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from src.clinic_recall.enums import (
    ExternalEffectState,
    ExternalEffectType,
    RightsRequestKind,
    RightsRequestState,
    RightsResidualCategory,
    RightsTargetAction,
    RightsTargetOwnerType,
    RightsTargetResource,
    RightsTargetState,
    RightsTargetSystem,
)
from src.clinic_recall.models import (
    TENANT_TABLES,
    Clinic,
    ExternalEffect,
    Patient,
    RightsRequest,
    RightsTarget,
)

MIGRATION_PATH = Path(
    "infra/postgres/migrations/versions/0019_rights_retention_purge.py"
)


def test_rights_models_are_minimized_tenant_scoped_and_convergent() -> None:
    assert {"rights_request", "rights_target"} <= set(TENANT_TABLES)
    assert ExternalEffectType.RIGHTS.value == "rights"
    forbidden = {
        "name",
        "phone",
        "email",
        "source_ref",
        "provider_sid",
        "provider_url",
        "blob_path",
        "message_body",
        "transcript",
        "raw_error",
        "key_material",
    }
    assert forbidden.isdisjoint(RightsRequest.__table__.c.keys())
    assert forbidden.isdisjoint(RightsTarget.__table__.c.keys())
    assert {constraint.name for constraint in RightsRequest.__table__.constraints} >= {
        "fk_rights_request_patient_tenant",
        "uq_rights_request_clinic_id_id",
        "uq_rights_request_convergence",
    }
    assert {constraint.name for constraint in RightsTarget.__table__.constraints} >= {
        "fk_rights_target_request_tenant",
        "fk_rights_target_prerequisite_tenant",
        "fk_rights_target_effect_tenant",
        "uq_rights_target_request_key",
    }


def test_0019_declares_forced_rls_and_database_guards() -> None:
    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "0019_rights_retention_purge"' in migration
    assert 'down_revision: str | None = "0018_recording_consent_ledger"' in migration
    assert "ADD VALUE IF NOT EXISTS 'rights'" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "rights_request_completion_guard" in migration
    assert "rights_target_prerequisite_guard" in migration
    assert "rights_locator_owner_guard" in migration
    assert "rollback by disabling rights dispatch" in migration


def test_sqlite_0019_empty_schema_round_trip(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'rights-empty.db'}"
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    config = Config("infra/postgres/alembic.ini")

    command.upgrade(config, "0019_rights_retention_purge")
    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    assert {"rights_request", "rights_target"} <= set(inspector.get_table_names())
    assert {column["name"] for column in inspector.get_columns("rights_request")} == set(
        RightsRequest.__table__.c.keys()
    )
    assert {column["name"] for column in inspector.get_columns("rights_target")} == set(
        RightsTarget.__table__.c.keys()
    )
    with Session(engine, expire_on_commit=False) as session:
        session.add(Clinic(id="clinic-rights-effect", name="Rights Effect Clinic"))
        session.add(
            ExternalEffect(
                id="effect-rights-migration",
                clinic_id="clinic-rights-effect",
                aggregate_type="rights_target",
                aggregate_id="target-rights-migration",
                effect_type=ExternalEffectType.RIGHTS,
                idempotency_key="rights:target-rights-migration:attempt:1",
                payload={
                    "intent": "rights_target_execute",
                    "target_id": "target-rights-migration",
                    "attempt_ordinal": 1,
                },
                request_hash="f" * 64,
                state=ExternalEffectState.PENDING,
                available_at=datetime.now(UTC),
                max_attempts=1,
            )
        )
        session.commit()
        assert session.get(ExternalEffect, "effect-rights-migration") is not None
        session.delete(session.get(ExternalEffect, "effect-rights-migration"))
        session.commit()

    command.downgrade(config, "0018_recording_consent_ledger")
    assert {"rights_request", "rights_target"}.isdisjoint(
        sa.inspect(engine).get_table_names()
    )
    engine.dispose()


def test_sqlite_0019_adopts_complete_matching_schema_and_installs_guards(
    tmp_path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'rights-adopt.db'}"
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    config = Config("infra/postgres/alembic.ini")
    command.upgrade(config, "0019_rights_retention_purge")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        trigger_names = connection.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE 'rights_%_guard%'"
            )
        ).scalars()
        for trigger_name in trigger_names:
            connection.execute(sa.text(f'DROP TRIGGER "{trigger_name}"'))
        connection.execute(
            sa.text(
                "UPDATE alembic_version SET version_num = "
                "'0018_recording_consent_ledger'"
            )
        )

    command.upgrade(config, "0019_rights_retention_purge")

    with engine.connect() as connection:
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        triggers = {
            row[0]
            for row in connection.execute(
                sa.text(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    "AND name LIKE 'rights_%_guard%'"
                )
            )
        }
    assert revision == "0019_rights_retention_purge"
    assert "rights_request_completion_guard" in triggers
    assert "rights_target_prerequisite_guard" in triggers
    assert "rights_locator_owner_guard_external_effect_update" in triggers
    engine.dispose()


def test_sqlite_0019_rejects_partial_rights_schema_without_advancing(
    tmp_path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'rights-partial.db'}"
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    config = Config("infra/postgres/alembic.ini")
    command.upgrade(config, "0019_rights_retention_purge")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE rights_target"))
        connection.execute(
            sa.text(
                "UPDATE alembic_version SET version_num = "
                "'0018_recording_consent_ledger'"
            )
        )

    with pytest.raises(RuntimeError, match="partial rights schema"):
        command.upgrade(config, "0019_rights_retention_purge")

    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0018_recording_consent_ledger"
        )
    engine.dispose()


def test_sqlite_0019_downgrade_refuses_rights_rows(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'rights-forward-only.db'}"
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    config = Config("infra/postgres/alembic.ini")
    command.upgrade(config, "0019_rights_retention_purge")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO clinic (id, name) VALUES ('clinic-rights', 'Rights Clinic')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO patient ("
                "id, clinic_id, source_ref, name, consent_flags, opt_out_flags"
                ") VALUES ("
                "'patient-rights', 'clinic-rights', 'P-rights', 'Rights Patient', '{}', '{}'"
                ")"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO rights_request ("
                "id, clinic_id, kind, subject_key_hash, subject_key_version, "
                "patient_reference_hash, patient_id, "
                "request_identity_hash, actor_role, actor_reference_hash, policy_version, "
                "approval_evidence_hash, scope_hash, state, requested_at, frozen_at, due_at"
                ") VALUES ("
                "'rights-row', 'clinic-rights', 'erasure', :digest, 'tests-v1', :digest, "
                "'patient-rights', :digest, 'dpo', :digest, 'tests-policy-v1', :digest, "
                ":digest, 'frozen', :now, :now, :now"
                ")"
            ),
            {"digest": "a" * 64, "now": "2026-07-22T00:00:00+00:00"},
        )

    with pytest.raises(RuntimeError, match="rollback by disabling rights dispatch"):
        command.downgrade(config, "0018_recording_consent_ledger")
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0019_rights_retention_purge"
        )
    engine.dispose()


def test_sqlite_0019_guards_require_current_completion_eligible_residual(
    tmp_path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'rights-residual-guards.db'}"
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    command.upgrade(Config("infra/postgres/alembic.ini"), "0019_rights_retention_purge")
    engine = sa.create_engine(database_url)
    now = datetime.now(UTC)
    with Session(engine, expire_on_commit=False) as session:
        session.add(Clinic(id="clinic-rights-guard", name="Rights Guard Clinic"))
        session.add(
            Patient(
                id="patient-rights-guard",
                clinic_id="clinic-rights-guard",
                source_ref="P-rights-guard",
                name="Synthetic Rights Guard",
            )
        )
        session.flush()
        request = RightsRequest(
            id="rights-guard",
            clinic_id="clinic-rights-guard",
            kind=RightsRequestKind.ERASURE,
            subject_key_hash="a" * 64,
            subject_key_version="tests-v1",
            patient_reference_hash="b" * 64,
            patient_id="patient-rights-guard",
            request_identity_hash="c" * 64,
            actor_role="dpo",
            actor_reference_hash="d" * 64,
            policy_version="tests-policy-v1",
            approval_evidence_hash="e" * 64,
            scope_hash="f" * 64,
            state=RightsRequestState.FROZEN,
            requested_at=now,
            frozen_at=now,
            inventory_finalized_at=now,
            due_at=now + timedelta(days=28),
        )
        effect = ExternalEffect(
            id="effect-rights-guard",
            clinic_id="clinic-rights-guard",
            aggregate_type="outreach_job",
            aggregate_id="job-rights-guard",
            effect_type=ExternalEffectType.SMS,
            idempotency_key="tests-rights-guard",
            payload={"intent": "recall"},
            request_hash="0" * 64,
            state=ExternalEffectState.SUCCEEDED,
            available_at=now,
            provider_resource_id="SM" + "1" * 32,
            completed_at=now,
        )
        prerequisite = RightsTarget(
            id="rights-target-prerequisite-guard",
            clinic_id="clinic-rights-guard",
            request_id=request.id,
            system=RightsTargetSystem.TWILIO,
            resource=RightsTargetResource.MESSAGE,
            action=RightsTargetAction.DELETE,
            owner_type=RightsTargetOwnerType.EXTERNAL_EFFECT,
            owner_id=effect.id,
            target_key_hash="1" * 64,
            state=RightsTargetState.RESIDUAL,
            available_at=now,
            due_at=now + timedelta(days=28),
            residual_category=RightsResidualCategory.PROVIDER_BACKUP_WINDOW,
            residual_policy_version="tests-residual-v1",
            residual_approval_evidence_hash="2" * 64,
            residual_completion_eligible=False,
            residual_due_at=now + timedelta(days=28),
        )
        dependent = RightsTarget(
            id="rights-target-dependent-guard",
            clinic_id="clinic-rights-guard",
            request_id=request.id,
            system=RightsTargetSystem.LOCAL,
            resource=RightsTargetResource.PATIENT_GRAPH,
            action=RightsTargetAction.MINIMIZE,
            owner_type=RightsTargetOwnerType.RIGHTS_REQUEST,
            owner_id=request.id,
            target_key_hash="3" * 64,
            prerequisite_target_id=prerequisite.id,
            state=RightsTargetState.REQUESTED,
            available_at=now,
            due_at=now + timedelta(days=28),
        )
        session.add_all([request, effect, prerequisite, dependent])
        session.commit()

        with pytest.raises(DBAPIError):
            session.execute(
                sa.update(ExternalEffect)
                .where(ExternalEffect.id == effect.id)
                .values(provider_resource_id=None)
            )
        session.rollback()
        with pytest.raises(DBAPIError):
            session.execute(
                sa.update(RightsTarget)
                .where(RightsTarget.id == dependent.id)
                .values(state=RightsTargetState.DISPATCHING)
            )
        session.rollback()

        session.execute(
            sa.update(RightsTarget)
            .where(RightsTarget.id == prerequisite.id)
            .values(
                residual_completion_eligible=True,
                residual_due_at=now - timedelta(days=1),
            )
        )
        session.commit()
        with pytest.raises(DBAPIError):
            session.execute(
                sa.update(ExternalEffect)
                .where(ExternalEffect.id == effect.id)
                .values(provider_resource_id=None)
            )
        session.rollback()

        session.execute(
            sa.update(RightsTarget)
            .where(RightsTarget.id == prerequisite.id)
            .values(residual_due_at=now + timedelta(days=28))
        )
        session.commit()
        session.execute(
            sa.update(RightsTarget)
            .where(RightsTarget.id == dependent.id)
            .values(state=RightsTargetState.DISPATCHING)
        )
        session.execute(
            sa.update(ExternalEffect)
            .where(ExternalEffect.id == effect.id)
            .values(provider_resource_id=None)
        )
        session.commit()

    engine.dispose()