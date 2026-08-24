"""Finite, read-only, tenant-scoped pilot observability snapshot (PR-14).

Collects bounded aggregate evidence for the closed signal registry in
``observability_registry.py`` and emits it through the allow-listed
non-transactional runtime telemetry path. The pass is read-only by
construction: it opens one session, runs closed-enum group-bys, closes the
session, then emits. Telemetry failure can never alter business state,
authorize an action, replay ambiguity, or re-enable outreach.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import clinic_scope, get_sessionmaker
from .durable.config import operational_snapshot_enabled
from .durable.worker import _bootstrap_runtime_configuration
from .enums import (
    CallRecordingStatus,
    ExternalEffectState,
    PilotProgrammeState,
    ProviderCallbackState,
    RightsRequestState,
    RightsTargetState,
)
from .handoff_ageing import _age_bucket
from .models import (
    CallRecord,
    ExternalEffect,
    PilotProgramme,
    ProviderCallbackReceipt,
    RightsRequest,
    RightsTarget,
)
from .pilot_controls import (
    OperationalSwitchSnapshot,
    operational_switch_snapshot_from_environment,
)
from .telemetry import emit_runtime_event

SessionFactory = Callable[[], Session]
EmitEvent = Callable[[str, Mapping[str, str | bool | int | float]], bool]

_CALLBACK_SNAPSHOT_STATES = (
    ProviderCallbackState.PENDING,
    ProviderCallbackState.PROCESSING,
)
_CONFIRMATION_GROUNDING_REASON = "booking_confirmation_authority_invalid"
_RECORDING_CONFLICT_REASON = "provider_outcome_conflict"
_ACTIVE_PROGRAMME_STATES = (
    PilotProgrammeState.DARK,
    PilotProgrammeState.ACTIVE,
    PilotProgrammeState.PAUSED,
)
_MIN_LOOKBACK = timedelta(hours=1)
_MAX_LOOKBACK = timedelta(days=7)


@dataclass(frozen=True)
class OperationalSnapshotResult:
    """Aggregate-only outcome of one bounded read-only snapshot pass."""

    enabled: bool
    events_emitted: int = 0
    emit_failures: int = 0
    callback_groups: int = 0
    confirmation_grounding_failures: int = 0
    recording_consent_mismatches: int = 0
    rights_overdue_total: int = 0
    configuration_reason: str = "unknown"
    release_mismatches: int = 0

    def as_summary(self) -> dict[str, int | bool | str]:
        return {
            "enabled": self.enabled,
            "events_emitted": self.events_emitted,
            "emit_failures": self.emit_failures,
            "callback_groups": self.callback_groups,
            "confirmation_grounding_failures": self.confirmation_grounding_failures,
            "recording_consent_mismatches": self.recording_consent_mismatches,
            "rights_overdue_total": self.rights_overdue_total,
            "configuration_reason": self.configuration_reason,
            "release_mismatches": self.release_mismatches,
        }


def run_operational_snapshot_once(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    now: datetime,
    enabled: bool = False,
    switches: OperationalSwitchSnapshot | None = None,
    lookback: timedelta = timedelta(hours=24),
    emit: EmitEvent = emit_runtime_event,
) -> OperationalSnapshotResult:
    """Collect one bounded aggregate snapshot; never mutate business state."""
    if not enabled:
        return OperationalSnapshotResult(enabled=False)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not _MIN_LOOKBACK <= lookback <= _MAX_LOOKBACK:
        raise ValueError("lookback must be between 1 hour and 7 days")
    now = now.astimezone(UTC)
    if switches is None:
        switches = operational_switch_snapshot_from_environment()

    events: list[tuple[str, dict[str, str | bool | int | float]]] = []
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            callback_groups = _callback_queue_events(session, clinic_id, now, events)
            grounding = _confirmation_grounding_event(session, clinic_id, now, lookback, events)
            mismatches = _recording_mismatch_events(session, clinic_id, now, lookback, events)
            rights_overdue = _rights_overdue_events(session, clinic_id, now, events)
            release_mismatches = _release_mismatch_event(session, clinic_id, switches, events)
    configuration_reason = _configuration_status_event(switches, now, events)

    emitted = 0
    failures = 0
    for name, attributes in events:
        try:
            published = emit(name, attributes)
        except Exception:
            published = False
        if not published:
            failures += 1
            continue
        emitted += 1
    return OperationalSnapshotResult(
        enabled=True,
        events_emitted=emitted,
        emit_failures=failures,
        callback_groups=callback_groups,
        confirmation_grounding_failures=grounding,
        recording_consent_mismatches=mismatches,
        rights_overdue_total=rights_overdue,
        configuration_reason=configuration_reason,
        release_mismatches=release_mismatches,
    )


def _callback_queue_events(
    session: Session,
    clinic_id: str,
    now: datetime,
    events: list[tuple[str, dict[str, str | bool | int | float]]],
) -> int:
    statement = (
        select(
            ProviderCallbackReceipt.state,
            func.count(ProviderCallbackReceipt.id),
            func.min(ProviderCallbackReceipt.received_at),
        )
        .where(
            ProviderCallbackReceipt.clinic_id == clinic_id,
            ProviderCallbackReceipt.state.in_(_CALLBACK_SNAPSHOT_STATES),
        )
        .group_by(ProviderCallbackReceipt.state)
    )
    groups = 0
    for state, count, oldest in session.execute(statement):
        if oldest is None:
            continue
        events.append(
            (
                "callbacks.queue.snapshot",
                {
                    "state": state.value,
                    "oldest_age_bucket": _age_bucket(oldest, now),
                    "count": int(count),
                },
            )
        )
        groups += 1
    return groups


def _confirmation_grounding_event(
    session: Session,
    clinic_id: str,
    now: datetime,
    lookback: timedelta,
    events: list[tuple[str, dict[str, str | bool | int | float]]],
) -> int:
    count = session.execute(
        select(func.count(ExternalEffect.id)).where(
            ExternalEffect.clinic_id == clinic_id,
            ExternalEffect.state == ExternalEffectState.CANCELED,
            ExternalEffect.last_error_code == _CONFIRMATION_GROUNDING_REASON,
            ExternalEffect.completed_at.is_not(None),
            ExternalEffect.completed_at >= now - lookback,
        )
    ).scalar_one()
    events.append(
        (
            "booking.confirmation.blocked",
            {"reason_code": _CONFIRMATION_GROUNDING_REASON, "count": int(count)},
        )
    )
    return int(count)


def _recording_mismatch_events(
    session: Session,
    clinic_id: str,
    now: datetime,
    lookback: timedelta,
    events: list[tuple[str, dict[str, str | bool | int | float]]],
) -> int:
    conflict_count = session.execute(
        select(func.count(ExternalEffect.id)).where(
            ExternalEffect.clinic_id == clinic_id,
            ExternalEffect.last_error_code == _RECORDING_CONFLICT_REASON,
            ExternalEffect.completed_at.is_not(None),
            ExternalEffect.completed_at >= now - lookback,
        )
    ).scalar_one()
    reconcile_count = session.execute(
        select(func.count(CallRecord.id)).where(
            CallRecord.clinic_id == clinic_id,
            CallRecord.recording_status == CallRecordingStatus.RECONCILE_REQUIRED,
        )
    ).scalar_one()
    events.append(
        (
            "recording.consent.mismatch",
            {"reason_code": _RECORDING_CONFLICT_REASON, "count": int(conflict_count)},
        )
    )
    events.append(
        (
            "recording.consent.mismatch",
            {
                "reason_code": "recording_status_reconcile_required",
                "count": int(reconcile_count),
            },
        )
    )
    return int(conflict_count) + int(reconcile_count)


def _rights_overdue_events(
    session: Session,
    clinic_id: str,
    now: datetime,
    events: list[tuple[str, dict[str, str | bool | int | float]]],
) -> int:
    request_count = session.execute(
        select(func.count(RightsRequest.id)).where(
            RightsRequest.clinic_id == clinic_id,
            RightsRequest.state != RightsRequestState.COMPLETED,
            RightsRequest.due_at < now,
        )
    ).scalar_one()
    target_count = session.execute(
        select(func.count(RightsTarget.id)).where(
            RightsTarget.clinic_id == clinic_id,
            RightsTarget.state.not_in((RightsTargetState.VERIFIED, RightsTargetState.RESIDUAL)),
            RightsTarget.due_at < now,
        )
    ).scalar_one()
    residual_count = session.execute(
        select(func.count(RightsTarget.id)).where(
            RightsTarget.clinic_id == clinic_id,
            RightsTarget.state == RightsTargetState.RESIDUAL,
            RightsTarget.residual_due_at.is_not(None),
            RightsTarget.residual_due_at < now,
        )
    ).scalar_one()
    for kind, count in (
        ("request", request_count),
        ("target", target_count),
        ("residual", residual_count),
    ):
        events.append(("rights.deletion.overdue", {"kind": kind, "count": int(count)}))
    return int(request_count) + int(target_count) + int(residual_count)


def _release_mismatch_event(
    session: Session,
    clinic_id: str,
    switches: OperationalSwitchSnapshot,
    events: list[tuple[str, dict[str, str | bool | int | float]]],
) -> int:
    if not switches.environment or not switches.release_identity:
        events.append(("pilot.release.mismatch", {"count": 0}))
        return 0
    count = session.execute(
        select(func.count(PilotProgramme.id)).where(
            PilotProgramme.clinic_id == clinic_id,
            PilotProgramme.state.in_(_ACTIVE_PROGRAMME_STATES),
            (PilotProgramme.environment != switches.environment)
            | (PilotProgramme.release_identity != switches.release_identity),
        )
    ).scalar_one()
    events.append(("pilot.release.mismatch", {"count": int(count)}))
    return int(count)


def _configuration_status_event(
    switches: OperationalSwitchSnapshot,
    now: datetime,
    events: list[tuple[str, dict[str, str | bool | int | float]]],
) -> str:
    blocked = switches._configuration_block(now)
    reason = "fresh" if blocked is None else blocked.reason
    events.append(("pilot.configuration.status", {"reason": reason, "count": 1}))
    return reason


def main(argv: Sequence[str] | None = None) -> int:
    """Run one finite, default-off, read-only snapshot and print aggregates."""
    parser = argparse.ArgumentParser(
        description="Run one read-only Clinic Recall pilot observability snapshot."
    )
    parser.add_argument("--clinic-id", required=True, help="Internal clinic scope.")
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=24,
        help="Recency window for terminal-state evidence (1-168).",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="Timezone-aware ISO-8601 timestamp; defaults to current UTC time.",
    )
    args = parser.parse_args(argv)
    _bootstrap_runtime_configuration()
    now = (
        datetime.now(UTC)
        if args.now is None
        else datetime.fromisoformat(args.now.replace("Z", "+00:00"))
    )
    if not operational_snapshot_enabled():
        print(json.dumps({"enabled": False, "reason": "operational_snapshot_disabled"}))
        return 0
    result = run_operational_snapshot_once(
        get_sessionmaker(),
        clinic_id=args.clinic_id,
        now=now,
        enabled=True,
        lookback=timedelta(hours=args.lookback_hours),
    )
    print(json.dumps(result.as_summary()))
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
