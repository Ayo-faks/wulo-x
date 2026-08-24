"""Finite transactional cadence planning with a tenant-scoped UTC cursor."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..db import clinic_scope, get_sessionmaker, tenant_select
from ..enums import Channel
from ..messaging.orchestrator import CadenceResult, run_cadence
from ..models import CadenceCursor
from ..pilot_controls import (
    PatientPilotGate,
    job_gate_for_snapshot,
    operational_switch_snapshot_from_environment,
    patient_gate_for_snapshot,
)
from ..sync.base import make_id
from ..voice_planner import ProgrammeGate, VoiceCadenceResult, run_voice_cadence
from .config import cadence_planning_enabled

SessionFactory = Callable[[], Session]

PLANNER_NAME = "scheduled_cadence"
MAX_WINDOW = timedelta(hours=24)
MIN_WINDOW = timedelta(minutes=1)

SmsPlanner = Callable[..., CadenceResult]
VoicePlanner = Callable[..., VoiceCadenceResult]


@dataclass(frozen=True)
class PlanningPassResult:
    """Aggregate-only result from one finite UTC planning transaction."""

    enabled: bool
    cursor_advanced: bool = False
    sms_enqueued: int = 0
    sms_existing: int = 0
    sms_canceled: int = 0
    email_policy_excluded: int = 0
    calls_enqueued: int = 0
    call_existing: int = 0
    calls_canceled: int = 0

    def as_summary(self) -> dict[str, int | bool]:
        return {
            "enabled": self.enabled,
            "cursor_advanced": self.cursor_advanced,
            "sms_enqueued": self.sms_enqueued,
            "sms_existing": self.sms_existing,
            "sms_canceled": self.sms_canceled,
            "email_policy_excluded": self.email_policy_excluded,
            "calls_enqueued": self.calls_enqueued,
            "call_existing": self.call_existing,
            "calls_canceled": self.calls_canceled,
        }


def run_planning_pass(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    now: datetime,
    enabled: bool = False,
    window: timedelta = timedelta(hours=1),
    batch_size: int = 50,
    sms_planner: SmsPlanner = run_cadence,
    voice_planner: VoicePlanner = run_voice_cadence,
    sms_pilot_gate: PatientPilotGate | None = None,
    programme_gate: ProgrammeGate | None = None,
) -> PlanningPassResult:
    """Plan one bounded interval and advance its cursor in the same commit."""
    _require_aware(now)
    if not clinic_id:
        raise ValueError("clinic_id is required")
    if not MIN_WINDOW <= window <= MAX_WINDOW:
        raise ValueError("window must be between 1 minute and 24 hours")
    if not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    if not enabled:
        return PlanningPassResult(enabled=False)
    if sms_pilot_gate is None or programme_gate is None:
        return PlanningPassResult(enabled=False)

    now_utc = now.astimezone(UTC)
    initial_watermark = now_utc - window
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            cursor = _lock_cursor(
                session,
                clinic_id=clinic_id,
                initial_watermark=initial_watermark,
            )
            watermark = _as_utc(cursor.watermark_at)
            if watermark >= now_utc:
                session.commit()
                return PlanningPassResult(enabled=True)

            cursor.last_started_at = now_utc
            cursor.last_run_id = uuid.uuid4().hex
            sms_result = sms_planner(
                session,
                clinic_id,
                now_utc,
                limit=batch_size,
                pilot_gate=sms_pilot_gate,
            )
            voice_result = voice_planner(
                session,
                clinic_id,
                now_utc,
                programme_gate=programme_gate,
                limit=batch_size,
            )
            cursor.watermark_at = now_utc
            cursor.last_completed_at = now_utc
            session.flush()
        session.commit()

    return PlanningPassResult(
        enabled=True,
        cursor_advanced=True,
        sms_enqueued=sms_result.sms_enqueued,
        sms_existing=sms_result.sms_existing,
        sms_canceled=sms_result.sms_canceled,
        email_policy_excluded=sms_result.email_policy_excluded,
        calls_enqueued=voice_result.calls_enqueued,
        call_existing=voice_result.call_existing,
        calls_canceled=voice_result.calls_canceled,
    )


def _lock_cursor(
    session: Session,
    *,
    clinic_id: str,
    initial_watermark: datetime,
) -> CadenceCursor:
    values = {
        "id": make_id("cadence-cursor", clinic_id, PLANNER_NAME),
        "clinic_id": clinic_id,
        "planner_name": PLANNER_NAME,
        "watermark_at": initial_watermark,
    }
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        session.execute(
            postgresql_insert(CadenceCursor)
            .values(**values)
            .on_conflict_do_nothing()
        )
    elif dialect == "sqlite":
        session.execute(
            sqlite_insert(CadenceCursor)
            .values(**values)
            .on_conflict_do_nothing()
        )
    else:
        existing = session.execute(
            tenant_select(CadenceCursor).where(CadenceCursor.planner_name == PLANNER_NAME)
        ).scalar_one_or_none()
        if existing is None:
            session.add(CadenceCursor(**values))
            session.flush()

    statement = tenant_select(CadenceCursor).where(CadenceCursor.planner_name == PLANNER_NAME)
    if dialect == "postgresql":
        statement = statement.with_for_update()
    cursor = session.execute(statement).scalar_one_or_none()
    if cursor is None:
        raise RuntimeError("cadence cursor could not be created")
    return cursor


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one finite, configuration-gated UTC planning pass."""
    parser = argparse.ArgumentParser(description="Run one Clinic Recall cadence plan.")
    parser.add_argument("--clinic-id", required=True, help="Internal clinic scope identifier.")
    parser.add_argument("--batch-size", type=int, default=50, help="Maximum jobs per planner.")
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=60,
        help="Maximum cursor catch-up window in minutes.",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="Timezone-aware ISO-8601 timestamp; defaults to current UTC time.",
    )
    args = parser.parse_args(argv)
    now = _parse_now(args.now)
    _bootstrap_runtime_configuration()
    if not cadence_planning_enabled(now):
        print(json.dumps(PlanningPassResult(enabled=False).as_summary(), sort_keys=True))
        return 0
    switches = operational_switch_snapshot_from_environment()
    if not switches.decision(Channel.SMS, now).allowed:
        print(json.dumps(PlanningPassResult(enabled=False).as_summary(), sort_keys=True))
        return 0

    result = run_planning_pass(
        get_sessionmaker(),
        clinic_id=args.clinic_id,
        now=now,
        enabled=True,
        window=timedelta(minutes=args.window_minutes),
        batch_size=args.batch_size,
        sms_pilot_gate=patient_gate_for_snapshot(switches),
        programme_gate=job_gate_for_snapshot(switches, Channel.CALL),
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
    """Hydrate operational configuration before opening the planning database."""
    if not os.getenv("AZURE_APPCONFIG_ENDPOINT", "").strip():
        return
    if bootstrap is None:
        from apps.artagent.backend.config.appconfig_provider import bootstrap_appconfig

        bootstrap = bootstrap_appconfig
    if not bootstrap():
        raise RuntimeError("Azure App Configuration failed to load; planner stopped")


if __name__ == "__main__":
    raise SystemExit(main())
