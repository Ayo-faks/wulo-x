"""Durable execution and reconciliation contracts for PR-10 rights targets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.clinic_recall.durable.enqueue import enqueue_sms_effect
from src.clinic_recall.durable.rights_worker import reconcile_once, run_once
from src.clinic_recall.enums import (
    ExternalEffectState,
    RightsRequestState,
    RightsTargetState,
    RightsTargetSystem,
)
from src.clinic_recall.models import (
    Appointment,
    Base,
    Campaign,
    Clinic,
    ExternalEffect,
    ExternalEffectHandoff,
    Interaction,
    OutreachJob,
    Patient,
    RightsRequest,
    RightsTarget,
)
from src.clinic_recall.retention import RetentionPolicy, schedule_retention_requests
from src.clinic_recall.rights import (
    RightsPolicy,
    SubjectKey,
    SubjectKeyring,
    get_rights_operations_status,
    request_patient_erasure,
)
from src.clinic_recall.rights_adapters import (
    RightsAdapterDisposition,
    RightsAdapterReason,
    RightsAdapterResult,
)

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
MESSAGE_SID = "SM" + "1" * 32


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    result = sessionmaker(bind=engine, expire_on_commit=False)
    with result.begin() as session:
        session.add(Clinic(id="clinic-rights-worker", name="Rights Worker Clinic"))
        session.add(
            Patient(
                id="patient-rights-worker",
                clinic_id="clinic-rights-worker",
                source_ref="P-RIGHTS-WORKER",
                name="Synthetic Rights Worker",
                phone="+447700900001",
                consent_flags={"sms": True},
            )
        )
        session.add(
            Appointment(
                id="appointment-rights-worker",
                clinic_id="clinic-rights-worker",
                patient_id="patient-rights-worker",
                source_ref="A-RIGHTS-WORKER",
                status="missed",
                start_at=NOW - timedelta(days=2),
            )
        )
        session.add(
            Campaign(
                id="campaign-rights-worker",
                clinic_id="clinic-rights-worker",
                type="recovery",
                status="active",
            )
        )
        session.add(
            OutreachJob(
                id="job-rights-worker",
                clinic_id="clinic-rights-worker",
                campaign_id="campaign-rights-worker",
                patient_id="patient-rights-worker",
                appointment_id="appointment-rights-worker",
                channel="sms",
                state="sent",
            )
        )
        effect, _ = enqueue_sms_effect(
            session,
            clinic_id="clinic-rights-worker",
            outreach_job_id="job-rights-worker",
            idempotency_key="rights-worker-sent-message",
            available_at=NOW,
        )
        effect.state = ExternalEffectState.SUCCEEDED
        effect.provider_resource_id = MESSAGE_SID
        effect.completed_at = NOW
        request_patient_erasure(
            session,
            clinic_id="clinic-rights-worker",
            patient_id="patient-rights-worker",
            confirm_token="ERASE patient-rights-worker",
            request_identity="tests-rights-worker-request",
            actor_role="dpo",
            actor_reference="tests-rights-worker-operator",
            keyring=SubjectKeyring(
                current=SubjectKey(
                    version="tests-worker-v1",
                    secret=b"tests-rights-worker-hmac-key",
                )
            ),
            policy=RightsPolicy(
                version="tests-worker-policy-v1",
                approval_evidence_hash="a" * 64,
                request_due_after=timedelta(days=28),
            ),
            now=NOW,
        )
    return result


@pytest.fixture
def retention_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    result = sessionmaker(bind=engine, expire_on_commit=False)
    with result.begin() as session:
        session.add(Clinic(id="clinic-retention-worker", name="Retention Worker Clinic"))
        session.add(
            Patient(
                id="patient-retention-worker",
                clinic_id="clinic-retention-worker",
                source_ref="P-RETENTION-WORKER",
                name="Synthetic Retention Worker",
                phone="+447700900002",
                consent_flags={"sms": True},
            )
        )
        session.add(
            Appointment(
                id="appointment-retention-worker",
                clinic_id="clinic-retention-worker",
                patient_id="patient-retention-worker",
                source_ref="A-RETENTION-WORKER",
                status="missed",
                start_at=NOW - timedelta(days=45),
            )
        )
        session.add(
            Campaign(
                id="campaign-retention-worker",
                clinic_id="clinic-retention-worker",
                type="recovery",
                status="active",
            )
        )
        session.add(
            OutreachJob(
                id="job-retention-worker",
                clinic_id="clinic-retention-worker",
                campaign_id="campaign-retention-worker",
                patient_id="patient-retention-worker",
                appointment_id="appointment-retention-worker",
                channel="sms",
                state="sent",
            )
        )
        session.add(
            Interaction(
                id="interaction-retention-worker",
                clinic_id="clinic-retention-worker",
                outreach_job_id="job-retention-worker",
                channel="sms",
                direction="inbound",
                content="synthetic expired interaction content",
                outcome="auto_handled",
                occurred_at=NOW - timedelta(days=40),
            )
        )
        session.flush()
        schedule_retention_requests(
            session,
            clinic_id="clinic-retention-worker",
            keyring=SubjectKeyring(
                current=SubjectKey(
                    version="tests-retention-worker-v1",
                    secret=b"tests-retention-worker-hmac-key",
                )
            ),
            policy=RetentionPolicy(
                version="tests-retention-policy-v1",
                approval_evidence_hash="b" * 64,
                approved_at=NOW - timedelta(days=2),
                effective_at=NOW - timedelta(days=1),
                expires_at=NOW + timedelta(days=90),
                retain_for=timedelta(days=40),
                request_due_after=timedelta(days=7),
            ),
            now=NOW,
            enabled=True,
        )
    return result


def _target(factory: sessionmaker[Session]) -> RightsTarget:
    with factory() as session:
        return session.execute(
            select(RightsTarget).where(RightsTarget.current_effect_id.is_not(None))
        ).scalar_one()


class _ObservingAdapter:
    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        delete_result: RightsAdapterResult,
        verify_result: RightsAdapterResult,
    ) -> None:
        self.factory = factory
        self.delete_result = delete_result
        self.verify_result = verify_result
        self.delete_calls = 0
        self.verify_calls = 0

    def delete(self, **kwargs) -> RightsAdapterResult:
        self.delete_calls += 1
        assert kwargs["locator"] == MESSAGE_SID
        with self.factory() as session:
            target = session.execute(
                select(RightsTarget).where(RightsTarget.current_effect_id.is_not(None))
            ).scalar_one()
            effect = session.get(ExternalEffect, target.current_effect_id)
            assert target.state == RightsTargetState.DISPATCHING
            assert effect is not None
            assert effect.state == ExternalEffectState.DISPATCHING
            assert effect.attempt_count == 1
        return self.delete_result

    def verify_absent(self, **kwargs) -> RightsAdapterResult:
        self.verify_calls += 1
        assert kwargs["locator"] == MESSAGE_SID
        return self.verify_result


def _result(disposition, reason, *, retry_at=None) -> RightsAdapterResult:
    return RightsAdapterResult(disposition, reason, retry_at=retry_at)


def test_rights_worker_commits_dispatching_before_io_and_verifies_absence(factory) -> None:
    adapter = _ObservingAdapter(
        factory,
        delete_result=_result(
            RightsAdapterDisposition.DELETED,
            RightsAdapterReason.PROVIDER_DELETED,
        ),
        verify_result=_result(
            RightsAdapterDisposition.ALREADY_ABSENT,
            RightsAdapterReason.ALREADY_ABSENT,
        ),
    )

    result = run_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-worker-1",
        adapters={RightsTargetSystem.TWILIO: adapter},
        now=NOW,
        enabled=True,
        max_target_attempts=2,
    )

    assert result.claimed == 1
    assert result.verified == 1
    assert adapter.delete_calls == 1
    assert adapter.verify_calls == 1
    with factory() as session:
        target = session.execute(
            select(RightsTarget).where(RightsTarget.current_effect_id.is_not(None))
        ).scalar_one()
        effect = session.get(ExternalEffect, target.current_effect_id)
        owner = session.execute(
            select(ExternalEffect).where(ExternalEffect.provider_resource_id == MESSAGE_SID)
        ).scalar_one()
        assert target.state == RightsTargetState.VERIFIED
        assert target.verified_at == NOW
        assert effect is not None and effect.state == ExternalEffectState.SUCCEEDED
        assert effect.provider_resource_id is None
        assert owner.provider_resource_id == MESSAGE_SID


def test_ambiguous_delete_is_quarantined_and_never_blindly_replayed(factory) -> None:
    adapter = _ObservingAdapter(
        factory,
        delete_result=_result(
            RightsAdapterDisposition.AMBIGUOUS,
            RightsAdapterReason.TRANSPORT_ERROR,
        ),
        verify_result=_result(
            RightsAdapterDisposition.AMBIGUOUS,
            RightsAdapterReason.TRANSPORT_ERROR,
        ),
    )

    first = run_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-worker-1",
        adapters={RightsTargetSystem.TWILIO: adapter},
        now=NOW,
        enabled=True,
        max_target_attempts=2,
    )
    second = run_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-worker-2",
        adapters={RightsTargetSystem.TWILIO: adapter},
        now=NOW + timedelta(minutes=10),
        enabled=True,
        max_target_attempts=2,
    )

    assert first.reconcile_required == 1
    assert second.claimed == 0
    assert adapter.delete_calls == 1
    with factory() as session:
        target = session.execute(
            select(RightsTarget).where(RightsTarget.current_effect_id.is_not(None))
        ).scalar_one()
        effect = session.get(ExternalEffect, target.current_effect_id)
        assert target.state == RightsTargetState.RECONCILE_REQUIRED
        assert effect is not None
        assert effect.state == ExternalEffectState.RECONCILE_REQUIRED


def test_malformed_residual_is_quarantined_and_not_counted_as_settled(factory) -> None:
    adapter = _ObservingAdapter(
        factory,
        delete_result=_result(
            RightsAdapterDisposition.RESIDUAL,
            RightsAdapterReason.PROVIDER_BACKUP_WINDOW,
        ),
        verify_result=_result(
            RightsAdapterDisposition.AMBIGUOUS,
            RightsAdapterReason.TRANSPORT_ERROR,
        ),
    )

    result = run_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-worker-1",
        adapters={RightsTargetSystem.TWILIO: adapter},
        now=NOW,
        enabled=True,
        max_target_attempts=2,
    )

    assert result.residual == 0
    assert result.reconcile_required == 1
    with factory() as session:
        target = session.execute(
            select(RightsTarget).where(RightsTarget.current_effect_id.is_not(None))
        ).scalar_one()
        assert target.state == RightsTargetState.RECONCILE_REQUIRED


def test_known_rate_limit_creates_one_new_bounded_effect(factory) -> None:
    adapter = _ObservingAdapter(
        factory,
        delete_result=_result(
            RightsAdapterDisposition.RETRYABLE_KNOWN_FAILURE,
            RightsAdapterReason.RATE_LIMITED,
            retry_at=NOW + timedelta(minutes=5),
        ),
        verify_result=_result(
            RightsAdapterDisposition.ALREADY_ABSENT,
            RightsAdapterReason.ALREADY_ABSENT,
        ),
    )

    result = run_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-worker-1",
        adapters={RightsTargetSystem.TWILIO: adapter},
        now=NOW,
        enabled=True,
        max_target_attempts=2,
    )

    assert result.retried == 1
    with factory() as session:
        target = session.execute(
            select(RightsTarget).where(RightsTarget.current_effect_id.is_not(None))
        ).scalar_one()
        effects = session.execute(
            select(ExternalEffect)
            .where(ExternalEffect.aggregate_type == "rights_target")
            .order_by(ExternalEffect.created_at, ExternalEffect.id)
        ).scalars().all()
        assert target.state == RightsTargetState.REQUESTED
        assert target.attempt_ordinal == 2
        assert len(effects) == 2
        assert sum(effect.state == ExternalEffectState.PENDING for effect in effects) == 1
        assert target.current_effect_id == next(
            effect.id for effect in effects if effect.state == ExternalEffectState.PENDING
        )

    success_adapter = _ObservingAdapter(
        factory,
        delete_result=_result(
            RightsAdapterDisposition.DELETED,
            RightsAdapterReason.PROVIDER_DELETED,
        ),
        verify_result=_result(
            RightsAdapterDisposition.ALREADY_ABSENT,
            RightsAdapterReason.ALREADY_ABSENT,
        ),
    )
    second = run_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-worker-2",
        adapters={RightsTargetSystem.TWILIO: success_adapter},
        now=NOW + timedelta(minutes=5),
        enabled=True,
        max_target_attempts=2,
    )

    assert second.claimed == 1
    assert second.verified == 1
    assert success_adapter.delete_calls == 1


def test_missing_adapter_recovers_via_presence_check_before_retry(factory) -> None:
    blocked = run_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-worker-1",
        adapters={},
        now=NOW,
        enabled=True,
        max_target_attempts=2,
    )

    assert blocked.configuration_blocked == 1
    with factory() as session:
        target = session.execute(
            select(RightsTarget).where(RightsTarget.current_effect_id.is_not(None))
        ).scalar_one()
        effect = session.get(ExternalEffect, target.current_effect_id)
        assert target.state == RightsTargetState.RECONCILE_REQUIRED
        assert effect is not None
        assert effect.state == ExternalEffectState.RECONCILE_REQUIRED

    restored_adapter = _ObservingAdapter(
        factory,
        delete_result=_result(
            RightsAdapterDisposition.DELETED,
            RightsAdapterReason.PROVIDER_DELETED,
        ),
        verify_result=_result(
            RightsAdapterDisposition.RETRYABLE_KNOWN_FAILURE,
            RightsAdapterReason.RESOURCE_PRESENT,
        ),
    )
    reconciled = reconcile_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-reconciler-1",
        adapters={RightsTargetSystem.TWILIO: restored_adapter},
        now=NOW + timedelta(minutes=1),
        enabled=True,
        max_target_attempts=2,
    )

    assert reconciled.retried == 1
    assert restored_adapter.delete_calls == 0
    assert restored_adapter.verify_calls == 1


def test_attempt_limit_stays_reconcilable_without_another_delete(factory) -> None:
    rate_limited = _ObservingAdapter(
        factory,
        delete_result=_result(
            RightsAdapterDisposition.RETRYABLE_KNOWN_FAILURE,
            RightsAdapterReason.RATE_LIMITED,
            retry_at=NOW + timedelta(minutes=5),
        ),
        verify_result=_result(
            RightsAdapterDisposition.ALREADY_ABSENT,
            RightsAdapterReason.ALREADY_ABSENT,
        ),
    )
    exhausted = run_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-worker-1",
        adapters={RightsTargetSystem.TWILIO: rate_limited},
        now=NOW,
        enabled=True,
        max_target_attempts=1,
    )

    assert exhausted.reconcile_required == 1
    assert rate_limited.delete_calls == 1
    with factory() as session:
        target = session.execute(
            select(RightsTarget).where(RightsTarget.current_effect_id.is_not(None))
        ).scalar_one()
        effect = session.get(ExternalEffect, target.current_effect_id)
        assert target.state == RightsTargetState.RECONCILE_REQUIRED
        assert effect is not None
        assert effect.state == ExternalEffectState.RECONCILE_REQUIRED

    absent = reconcile_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-reconciler-1",
        adapters={RightsTargetSystem.TWILIO: rate_limited},
        now=NOW + timedelta(minutes=5),
        enabled=True,
        max_target_attempts=1,
    )

    assert absent.verified == 1
    assert rate_limited.delete_calls == 1
    assert rate_limited.verify_calls == 1


def test_reconciliation_checks_presence_before_authorizing_retry(factory) -> None:
    first_adapter = _ObservingAdapter(
        factory,
        delete_result=_result(
            RightsAdapterDisposition.AMBIGUOUS,
            RightsAdapterReason.TRANSPORT_ERROR,
        ),
        verify_result=_result(
            RightsAdapterDisposition.AMBIGUOUS,
            RightsAdapterReason.TRANSPORT_ERROR,
        ),
    )
    run_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-worker-1",
        adapters={RightsTargetSystem.TWILIO: first_adapter},
        now=NOW,
        enabled=True,
        max_target_attempts=2,
    )
    present_adapter = _ObservingAdapter(
        factory,
        delete_result=_result(
            RightsAdapterDisposition.DELETED,
            RightsAdapterReason.PROVIDER_DELETED,
        ),
        verify_result=_result(
            RightsAdapterDisposition.RETRYABLE_KNOWN_FAILURE,
            RightsAdapterReason.RESOURCE_PRESENT,
        ),
    )

    result = reconcile_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-reconciler-1",
        adapters={RightsTargetSystem.TWILIO: present_adapter},
        now=NOW + timedelta(minutes=1),
        enabled=True,
        max_target_attempts=2,
    )

    assert result.inspected == 1
    assert result.retried == 1
    assert present_adapter.delete_calls == 0
    assert present_adapter.verify_calls == 1
    with factory() as session:
        target = session.execute(
            select(RightsTarget).where(RightsTarget.current_effect_id.is_not(None))
        ).scalar_one()
        assert target.state == RightsTargetState.REQUESTED
        assert target.attempt_ordinal == 2


def test_persistent_ambiguity_queues_one_handoff_and_stops_automatic_polling(
    factory,
) -> None:
    adapter = _ObservingAdapter(
        factory,
        delete_result=_result(
            RightsAdapterDisposition.AMBIGUOUS,
            RightsAdapterReason.TRANSPORT_ERROR,
        ),
        verify_result=_result(
            RightsAdapterDisposition.AMBIGUOUS,
            RightsAdapterReason.TRANSPORT_ERROR,
        ),
    )
    run_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-worker-1",
        adapters={RightsTargetSystem.TWILIO: adapter},
        now=NOW,
        enabled=True,
        max_target_attempts=2,
    )

    first = reconcile_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-reconciler-1",
        adapters={RightsTargetSystem.TWILIO: adapter},
        now=NOW + timedelta(minutes=1),
        enabled=True,
        max_target_attempts=2,
    )
    second = reconcile_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-reconciler-2",
        adapters={RightsTargetSystem.TWILIO: adapter},
        now=NOW + timedelta(minutes=2),
        enabled=True,
        max_target_attempts=2,
    )
    repeated = reconcile_once(
        factory,
        clinic_id="clinic-rights-worker",
        worker_id="rights-reconciler-3",
        adapters={RightsTargetSystem.TWILIO: adapter},
        now=NOW + timedelta(minutes=3),
        enabled=True,
        max_target_attempts=2,
    )

    assert first.reconcile_required == 1
    assert first.handoffs_queued == 0
    assert second.reconcile_required == 1
    assert second.handoffs_queued == 1
    assert repeated.inspected == 0
    assert repeated.handoffs_queued == 0
    assert adapter.delete_calls == 1
    assert adapter.verify_calls == 2
    with factory() as session:
        target = session.execute(
            select(RightsTarget).where(RightsTarget.current_effect_id.is_not(None))
        ).scalar_one()
        effect = session.get(ExternalEffect, target.current_effect_id)
        handoffs = session.execute(select(ExternalEffectHandoff)).scalars().all()
        assert target.state == RightsTargetState.RECONCILE_REQUIRED
        assert target.reconciliation_count == 2
        assert target.reason_code == "reconciliation_exhausted"
        assert effect is not None
        assert effect.state == ExternalEffectState.RECONCILE_REQUIRED
        assert len(handoffs) == 1
        assert handoffs[0].external_effect_id == effect.id
        assert handoffs[0].reason_code == "reconciliation_exhausted"
        status = get_rights_operations_status(
            session,
            clinic_id="clinic-rights-worker",
            now=NOW + timedelta(minutes=3),
        )
        assert status.handoff_count == 1
        assert status.ready is False


def test_local_retention_target_minimizes_and_settles_atomically(
    retention_factory,
) -> None:
    result = run_once(
        retention_factory,
        clinic_id="clinic-retention-worker",
        worker_id="retention-worker-1",
        adapters={},
        now=NOW,
        enabled=True,
        max_target_attempts=2,
    )

    assert result.claimed == 1
    assert result.verified == 1
    assert result.configuration_blocked == 0
    with retention_factory() as session:
        interaction = session.get(Interaction, "interaction-retention-worker")
        target = session.execute(select(RightsTarget)).scalar_one()
        effect = session.get(ExternalEffect, target.current_effect_id)
        request = session.execute(select(RightsRequest)).scalar_one()
        assert interaction is not None and interaction.content is None
        assert target.state == RightsTargetState.VERIFIED
        assert target.locator_cleared_at == NOW
        assert effect is not None and effect.state == ExternalEffectState.SUCCEEDED
        assert request.state == RightsRequestState.COMPLETED
        assert request.verified_target_count == 1
        assert request.completed_at == NOW
        assert request.completion_evidence_hash is not None


def test_local_retention_reconciliation_retries_present_content_without_clearing_it(
    retention_factory,
) -> None:
    with retention_factory.begin() as session:
        target = session.execute(select(RightsTarget)).scalar_one()
        effect = session.get(ExternalEffect, target.current_effect_id)
        assert effect is not None
        effect.state = ExternalEffectState.RECONCILE_REQUIRED
        effect.attempt_count = 1
        effect.dispatch_started_at = NOW
        target.state = RightsTargetState.RECONCILE_REQUIRED

    reconciled = reconcile_once(
        retention_factory,
        clinic_id="clinic-retention-worker",
        worker_id="retention-reconciler-1",
        adapters={},
        now=NOW + timedelta(minutes=1),
        enabled=True,
        max_target_attempts=2,
    )

    assert reconciled.inspected == 1
    assert reconciled.retried == 1
    with retention_factory() as session:
        interaction = session.get(Interaction, "interaction-retention-worker")
        target = session.execute(select(RightsTarget)).scalar_one()
        assert interaction is not None
        assert interaction.content == "synthetic expired interaction content"
        assert target.state == RightsTargetState.REQUESTED
        assert target.attempt_ordinal == 2

    executed = run_once(
        retention_factory,
        clinic_id="clinic-retention-worker",
        worker_id="retention-worker-2",
        adapters={},
        now=NOW + timedelta(minutes=1),
        enabled=True,
        max_target_attempts=2,
    )

    assert executed.claimed == 1
    assert executed.verified == 1
    with retention_factory() as session:
        interaction = session.get(Interaction, "interaction-retention-worker")
        assert interaction is not None and interaction.content is None