"""Finite tenant-scoped SLA ageing for unacknowledged human handoffs."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import clinic_scope, get_sessionmaker, tenant_select
from .durable.config import handoff_ageing_enabled
from .enums import HandoffSeverity
from .handoffs import (
    handoff_owner_is_active,
    pause_clinic_programmes_for_handoff,
    request_alternate_notification,
)
from .models import HandoffReceipt
from .telemetry import configure_job_telemetry, queue_after_commit

SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class HandoffAgeingResult:
    """Aggregate-only result of one bounded SLA pass."""

    enabled: bool
    overdue: int = 0
    critical_high_breaches: int = 0
    normal_breaches: int = 0
    alternate_requested: int = 0
    programmes_paused: int = 0

    def as_summary(self) -> dict[str, int | bool]:
        return {
            "enabled": self.enabled,
            "overdue": self.overdue,
            "critical_high_breaches": self.critical_high_breaches,
            "normal_breaches": self.normal_breaches,
            "alternate_requested": self.alternate_requested,
            "programmes_paused": self.programmes_paused,
        }


def run_handoff_ageing_once(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    now: datetime,
    enabled: bool = False,
    limit: int = 50,
) -> HandoffAgeingResult:
    """Age one locked batch; never contact a patient or replay provider work."""
    if not enabled:
        return HandoffAgeingResult(enabled=False)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not 1 <= limit <= 250:
        raise ValueError("limit must be between 1 and 250")
    now = now.astimezone(UTC)
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            statement = (
                tenant_select(HandoffReceipt)
                .where(
                    HandoffReceipt.acknowledged_at.is_(None),
                    HandoffReceipt.resolved_at.is_(None),
                    HandoffReceipt.due_at <= now,
                )
                .order_by(HandoffReceipt.due_at, HandoffReceipt.id)
                .limit(limit)
            )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            receipts = list(session.execute(statement).scalars())
            active = [
                receipt
                for receipt in receipts
                if handoff_owner_is_active(session, receipt)
            ]
            alternate_requested = 0
            critical_high = 0
            normal = 0
            for receipt in active:
                alternate_requested += int(
                    request_alternate_notification(
                        session,
                        receipt,
                        now=now,
                        reason_code="handoff_sla_breached",
                    )
                )
                if receipt.severity in {
                    HandoffSeverity.CRITICAL,
                    HandoffSeverity.HIGH,
                }:
                    critical_high += 1
                else:
                    normal += 1
            _queue_snapshot(session, clinic_id=clinic_id, now=now)
            for severity in (HandoffSeverity.CRITICAL, HandoffSeverity.HIGH):
                count = sum(receipt.severity == severity for receipt in active)
                if count:
                    queue_after_commit(
                        session,
                        "handoff.sla.breach",
                        {"severity": severity.value, "count": count},
                    )
            programmes_paused = 0
            if critical_high:
                programmes_paused = pause_clinic_programmes_for_handoff(
                    session,
                    clinic_id=clinic_id,
                    now=now,
                    reason_code="handoff_sla_breached",
                )
            session.commit()
    return HandoffAgeingResult(
        enabled=True,
        overdue=len(active),
        critical_high_breaches=critical_high,
        normal_breaches=normal,
        alternate_requested=alternate_requested,
        programmes_paused=programmes_paused,
    )


def _queue_snapshot(
    session: Session,
    *,
    clinic_id: str,
    now: datetime,
) -> None:
    statement = (
        select(
            HandoffReceipt.severity,
            HandoffReceipt.delivery_state,
            func.count(HandoffReceipt.id),
            func.min(HandoffReceipt.queued_at),
        )
        .where(
            HandoffReceipt.clinic_id == clinic_id,
            HandoffReceipt.resolved_at.is_(None),
        )
        .group_by(HandoffReceipt.severity, HandoffReceipt.delivery_state)
    )
    for severity, delivery_state, count, oldest_queued_at in session.execute(statement):
        if oldest_queued_at is None:
            continue
        queue_after_commit(
            session,
            "handoff.queue.snapshot",
            {
                "severity": severity.value,
                "delivery_state": delivery_state.value,
                "oldest_age_bucket": _age_bucket(oldest_queued_at, now),
                "count": int(count),
            },
        )


def _age_bucket(queued_at: datetime, now: datetime) -> str:
    age_seconds = max(0, int((now - _as_utc(queued_at)).total_seconds()))
    if age_seconds < 300:
        return "under_5m"
    if age_seconds < 900:
        return "5m_to_15m"
    if age_seconds < 3600:
        return "15m_to_1h"
    if age_seconds < 14_400:
        return "1h_to_4h"
    return "over_4h"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one finite, default-off handoff ageing pass."""
    parser = argparse.ArgumentParser(description="Run Clinic Recall handoff ageing.")
    parser.add_argument("--clinic-id", required=True, help="Internal clinic scope.")
    parser.add_argument("--limit", type=int, default=50, help="Maximum due receipts.")
    parser.add_argument(
        "--now",
        default=None,
        help="Timezone-aware ISO-8601 timestamp; defaults to current UTC time.",
    )
    args = parser.parse_args(argv)
    now = _parse_now(args.now)
    _bootstrap_runtime_configuration()
    if not handoff_ageing_enabled():
        print(json.dumps(HandoffAgeingResult(enabled=False).as_summary(), sort_keys=True))
        return 0
    result = run_handoff_ageing_once(
        get_sessionmaker(),
        clinic_id=args.clinic_id,
        now=now,
        enabled=True,
        limit=args.limit,
    )
    print(json.dumps(result.as_summary(), sort_keys=True))
    return 0


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must include a timezone")
    return parsed.astimezone(UTC)


def _bootstrap_runtime_configuration(
    bootstrap: Callable[[], bool] | None = None,
) -> None:
    if os.getenv("AZURE_APPCONFIG_ENDPOINT", "").strip():
        if bootstrap is None:
            from apps.artagent.backend.config.appconfig_provider import bootstrap_appconfig

            bootstrap = bootstrap_appconfig
        if not bootstrap():
            raise RuntimeError("Azure App Configuration failed to load; ageing stopped")
    configure_job_telemetry()


if __name__ == "__main__":
    raise SystemExit(main())