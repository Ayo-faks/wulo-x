"""Finite notification and SLA ageing contracts for PR-12 handoffs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.clinic_recall import handoff_ageing
from src.clinic_recall.durable.handoff_worker import (
    HandoffNotificationResult,
    HandoffNotificationStatus,
    OperationalDestination,
    OperationalNotificationOutcomeUnknown,
    run_handoff_notifications_once,
)
from src.clinic_recall.enums import (
    EscalationReason,
    EscalationStatus,
    ExternalEffectState,
    ExternalEffectType,
    HandoffAlternateState,
    HandoffDeliveryState,
    HandoffDestinationRole,
    HandoffRouteKind,
    PilotProgrammeState,
)
from src.clinic_recall.handoff_ageing import run_handoff_ageing_once
from src.clinic_recall.handoffs import acknowledge_handoff_owner, ensure_handoff_receipt
from src.clinic_recall.models import (
    Base,
    Clinic,
    Escalation,
    ExternalEffect,
    HandoffReceipt,
    Patient,
    PilotProgramme,
)
from src.clinic_recall.pilot_controls import create_programme

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(
            Clinic(
                id="clinic-pr12",
                name="PR12",
                timezone="Europe/London",
                contact_hours={"start_hour": 8, "end_hour": 20},
            )
        )
        for suffix in ("normal", "critical"):
            session.add(
                Patient(
                    id=f"patient-{suffix}",
                    clinic_id="clinic-pr12",
                    source_ref=f"P-{suffix}",
                    name=f"Synthetic {suffix}",
                )
            )
        create_programme(
            session,
            clinic_id="clinic-pr12",
            programme_id="programme-pr12",
            environment="production",
            release_identity="sha256:pr12",
        )
    return factory


def _add_escalation_receipt(
    session: Session,
    *,
    suffix: str,
    reason: EscalationReason,
) -> HandoffReceipt:
    escalation = Escalation(
        id=f"escalation-{suffix}",
        clinic_id="clinic-pr12",
        patient_id=f"patient-{suffix}",
        reason=reason,
        status=EscalationStatus.OPEN,
    )
    session.add(escalation)
    session.flush()
    return ensure_handoff_receipt(
        session,
        "clinic-pr12",
        escalation,
        now=NOW,
    ).receipt


class _Resolver:
    def __init__(self, *, configured: bool = True) -> None:
        self.configured = configured
        self.calls = 0

    def resolve(
        self,
        *,
        destination_role: HandoffDestinationRole,
        route_kind: HandoffRouteKind,
    ) -> OperationalDestination | None:
        self.calls += 1
        if not self.configured:
            return None
        return OperationalDestination(
            destination_role=destination_role,
            route_kind=route_kind,
            dispatch_token="approved-route",
        )


class _Notifier:
    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        status: HandoffNotificationStatus = HandoffNotificationStatus.ACCEPTED,
        ambiguous: bool = False,
    ) -> None:
        self.factory = factory
        self.status = status
        self.ambiguous = ambiguous
        self.calls = 0
        self.batch_was_dispatching = False

    def send(
        self,
        *,
        destination: OperationalDestination,
        receipt_id: str,
        template_version: str,
    ) -> HandoffNotificationResult:
        del destination, receipt_id, template_version
        self.calls += 1
        with self.factory() as session:
            states = list(
                session.scalars(
                    select(ExternalEffect.state).where(
                        ExternalEffect.effect_type
                        == ExternalEffectType.HANDOFF_NOTIFICATION
                    )
                )
            )
        if self.calls == 1:
            self.batch_was_dispatching = bool(states) and all(
                state == ExternalEffectState.DISPATCHING for state in states
            )
        if self.ambiguous:
            raise OperationalNotificationOutcomeUnknown()
        return HandoffNotificationResult(
            status=self.status,
            provider_resource_id=(
                f"provider-{self.calls}"
                if self.status == HandoffNotificationStatus.ACCEPTED
                else None
            ),
            reason_code=(
                "provider_permanent_rejection"
                if self.status == HandoffNotificationStatus.PERMANENT_REJECTION
                else None
            ),
        )


def test_disabled_notification_worker_never_opens_database_or_calls_provider() -> None:
    resolver = _Resolver()
    notifier = _Notifier(_factory())

    result = run_handoff_notifications_once(
        lambda: (_ for _ in ()).throw(AssertionError("database must remain untouched")),
        clinic_id="clinic-pr12",
        worker_id="worker-disabled",
        destination_resolver=resolver,
        notifier=notifier,
        now=NOW,
        enabled=False,
    )

    assert result.enabled is False
    assert resolver.calls == 0
    assert notifier.calls == 0


def test_disabled_ageing_cli_never_opens_database(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("AZURE_APPCONFIG_ENDPOINT", raising=False)
    monkeypatch.setattr(handoff_ageing, "handoff_ageing_enabled", lambda: False)
    monkeypatch.setattr(
        handoff_ageing,
        "get_sessionmaker",
        lambda: (_ for _ in ()).throw(AssertionError("database must remain untouched")),
    )

    result = handoff_ageing.main(
        ["--clinic-id", "clinic-pr12", "--now", NOW.isoformat()]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is False


def test_notification_worker_commits_complete_batch_before_acceptance() -> None:
    factory = _factory()
    with factory.begin() as session:
        _add_escalation_receipt(
            session,
            suffix="normal",
            reason=EscalationReason.AMBIGUOUS,
        )
        _add_escalation_receipt(
            session,
            suffix="critical",
            reason=EscalationReason.URGENT,
        )
    resolver = _Resolver()
    notifier = _Notifier(factory)

    result = run_handoff_notifications_once(
        factory,
        clinic_id="clinic-pr12",
        worker_id="worker-accepted",
        destination_resolver=resolver,
        notifier=notifier,
        now=NOW + timedelta(seconds=1),
        enabled=True,
    )

    assert result.claimed == 2
    assert result.sent == 2
    assert notifier.calls == 2
    assert notifier.batch_was_dispatching is True
    with factory() as session:
        effects = list(
            session.scalars(
                select(ExternalEffect).where(
                    ExternalEffect.effect_type
                    == ExternalEffectType.HANDOFF_NOTIFICATION
                )
            )
        )
        receipts = list(session.scalars(select(HandoffReceipt)))
    assert {effect.state for effect in effects} == {ExternalEffectState.SUCCEEDED}
    assert {receipt.delivery_state for receipt in receipts} == {
        HandoffDeliveryState.SENT
    }
    assert all(receipt.sent_at is not None for receipt in receipts)
    assert all(receipt.delivered_at is None for receipt in receipts)
    assert all(receipt.acknowledged_at is None for receipt in receipts)


def test_acknowledgement_before_send_cancels_notification_and_remains_recorded() -> None:
    factory = _factory()
    with factory.begin() as session:
        receipt = _add_escalation_receipt(
            session,
            suffix="normal",
            reason=EscalationReason.AMBIGUOUS,
        )
        owner = session.get(Escalation, "escalation-normal")
        assert owner is not None
        acknowledge_handoff_owner(
            session,
            clinic_id="clinic-pr12",
            owner=owner,
            actor="staff:test",
            now=NOW + timedelta(seconds=1),
        )
    resolver = _Resolver()
    notifier = _Notifier(factory)

    result = run_handoff_notifications_once(
        factory,
        clinic_id="clinic-pr12",
        worker_id="worker-acknowledged",
        destination_resolver=resolver,
        notifier=notifier,
        now=NOW + timedelta(seconds=2),
        enabled=True,
    )

    assert result.canceled == 1
    assert resolver.calls == 0
    assert notifier.calls == 0
    with factory() as session:
        stored = session.get(HandoffReceipt, receipt.id)
        owner = session.get(Escalation, "escalation-normal")
    assert stored is not None
    assert stored.acknowledged_by == "staff:test"
    assert stored.resolved_at is None
    assert owner is not None and owner.status == EscalationStatus.ACKNOWLEDGED


def test_missing_destination_retains_queue_requests_alternate_and_pauses() -> None:
    factory = _factory()
    with factory.begin() as session:
        receipt = _add_escalation_receipt(
            session,
            suffix="normal",
            reason=EscalationReason.AMBIGUOUS,
        )
    resolver = _Resolver(configured=False)
    notifier = _Notifier(factory)

    result = run_handoff_notifications_once(
        factory,
        clinic_id="clinic-pr12",
        worker_id="worker-no-destination",
        destination_resolver=resolver,
        notifier=notifier,
        now=NOW + timedelta(seconds=1),
        enabled=True,
    )

    assert result.destination_unavailable == 1
    assert result.alternate_requested == 1
    assert result.programmes_paused == 1
    assert notifier.calls == 0
    with factory() as session:
        stored = session.get(HandoffReceipt, receipt.id)
        owner = session.get(Escalation, "escalation-normal")
        programme = session.get(PilotProgramme, "programme-pr12")
    assert stored is not None
    assert stored.delivery_state == HandoffDeliveryState.DEFINITIVE_FAILURE
    assert stored.alternate_state == HandoffAlternateState.REQUESTED
    assert owner is not None and owner.status == EscalationStatus.OPEN
    assert programme is not None and programme.state == PilotProgrammeState.PAUSED


def test_ambiguous_provider_outcome_is_quarantined_and_never_replayed() -> None:
    factory = _factory()
    with factory.begin() as session:
        receipt = _add_escalation_receipt(
            session,
            suffix="normal",
            reason=EscalationReason.AMBIGUOUS,
        )
    resolver = _Resolver()
    notifier = _Notifier(factory, ambiguous=True)

    first = run_handoff_notifications_once(
        factory,
        clinic_id="clinic-pr12",
        worker_id="worker-ambiguous",
        destination_resolver=resolver,
        notifier=notifier,
        now=NOW + timedelta(seconds=1),
        enabled=True,
    )
    second = run_handoff_notifications_once(
        factory,
        clinic_id="clinic-pr12",
        worker_id="worker-replay",
        destination_resolver=resolver,
        notifier=notifier,
        now=NOW + timedelta(minutes=10),
        enabled=True,
    )

    assert first.reconcile_required == 1
    assert first.alternate_requested == 1
    assert first.programmes_paused == 0
    assert second.claimed == 0
    assert notifier.calls == 1
    with factory() as session:
        stored = session.get(HandoffReceipt, receipt.id)
        effect = session.scalar(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.HANDOFF_NOTIFICATION
            )
        )
        programme = session.get(PilotProgramme, "programme-pr12")
    assert stored is not None
    assert stored.delivery_state == HandoffDeliveryState.RECONCILE_REQUIRED
    assert effect is not None and effect.state == ExternalEffectState.RECONCILE_REQUIRED
    assert programme is not None and programme.state == PilotProgrammeState.DRAFT


def test_ageing_requests_once_and_pauses_only_for_critical_or_high() -> None:
    factory = _factory()
    with factory.begin() as session:
        normal = _add_escalation_receipt(
            session,
            suffix="normal",
            reason=EscalationReason.AMBIGUOUS,
        )
        _add_escalation_receipt(
            session,
            suffix="critical",
            reason=EscalationReason.URGENT,
        )
        normal.due_at = NOW + timedelta(minutes=4)

    first = run_handoff_ageing_once(
        factory,
        clinic_id="clinic-pr12",
        now=NOW + timedelta(minutes=6),
        enabled=True,
    )
    second = run_handoff_ageing_once(
        factory,
        clinic_id="clinic-pr12",
        now=NOW + timedelta(minutes=7),
        enabled=True,
    )

    assert first.overdue == 2
    assert first.alternate_requested == 2
    assert first.programmes_paused == 1
    assert first.critical_high_breaches == 1
    assert first.normal_breaches == 1
    assert second.alternate_requested == 0
    assert second.programmes_paused == 0
    with factory() as session:
        programme = session.get(PilotProgramme, "programme-pr12")
        receipts = list(session.scalars(select(HandoffReceipt)))
    assert programme is not None and programme.state == PilotProgrammeState.PAUSED
    assert all(
        receipt.alternate_state == HandoffAlternateState.REQUESTED
        for receipt in receipts
    )


def test_ageing_snapshot_counts_all_open_receipts_with_oldest_bucket(
    monkeypatch,
) -> None:
    factory = _factory()
    with factory.begin() as session:
        _add_escalation_receipt(
            session,
            suffix="normal",
            reason=EscalationReason.AMBIGUOUS,
        )
        critical = _add_escalation_receipt(
            session,
            suffix="critical",
            reason=EscalationReason.URGENT,
        )
        critical.due_at = NOW + timedelta(minutes=5)

    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        handoff_ageing,
        "queue_after_commit",
        lambda _session, name, attributes: events.append((name, dict(attributes))),
    )
    result = run_handoff_ageing_once(
        factory,
        clinic_id="clinic-pr12",
        now=NOW + timedelta(minutes=6),
        enabled=True,
    )

    snapshots = {
        attributes["severity"]: attributes
        for name, attributes in events
        if name == "handoff.queue.snapshot"
    }
    assert result.overdue == 1
    assert set(snapshots) == {"critical", "normal"}
    assert snapshots["critical"] == {
        "severity": "critical",
        "delivery_state": "queued",
        "oldest_age_bucket": "5m_to_15m",
        "count": 1,
    }
    assert snapshots["normal"] == {
        "severity": "normal",
        "delivery_state": "queued",
        "oldest_age_bucket": "5m_to_15m",
        "count": 1,
    }