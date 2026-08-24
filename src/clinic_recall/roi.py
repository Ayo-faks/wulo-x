"""Aggregate-only ROI dashboard read models for Clinic Recall Phase 4."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from io import StringIO

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .enums import (
    AppointmentStatus,
    BookingActionStatus,
    InteractionDirection,
    InteractionIntent,
)
from .models import Appointment, BookingAction, Interaction

MONEY = Decimal("0.01")
ZERO_MONEY = Decimal("0.00")
DEFAULT_SUBSCRIPTION_COST = Decimal("199.00")
DEFAULT_USAGE_COST = Decimal("0.00")
NO_SHOW_STATUSES = {AppointmentStatus.MISSED, AppointmentStatus.NO_SHOW}


class RoiMetrics(BaseModel):
    """Aggregate dashboard metrics without patient/message-level detail."""

    model_config = ConfigDict(json_encoders={Decimal: lambda value: str(value)})

    start: datetime
    end: datetime
    contacted: int
    rebooked: int
    conversion_rate: float
    recovered_revenue: Decimal
    no_show_delta: float
    opt_out_rate: float
    monthly_net: Decimal
    roi_multiple: Decimal
    subscription_cost: Decimal
    usage_cost: Decimal


def get_roi_metrics(
    session: Session,
    clinic_id: str,
    *,
    start: datetime,
    end: datetime,
    subscription_cost: Decimal = DEFAULT_SUBSCRIPTION_COST,
    usage_cost: Decimal = DEFAULT_USAGE_COST,
) -> RoiMetrics:
    """Return deterministic aggregate ROI metrics for one clinic and period."""
    _require_aware("start", start)
    _require_aware("end", end)
    if end <= start:
        raise ValueError("end must be after start")
    subscription = _money(subscription_cost)
    usage = _money(usage_cost)

    with clinic_scope(session, clinic_id):
        contacted_jobs = _contacted_job_ids(session, start, end)
        opt_out_jobs = _opt_out_job_ids(session, start, end)
        completed_bookings = _completed_bookings(session, start, end)
        revenue = _money(
            sum(
                (appointment.value or Decimal("0"))
                for appointment in _booking_appointments(session, completed_bookings)
            )
        )
        contacted = len(contacted_jobs)
        rebooked = len(completed_bookings)
        conversion_rate = _rate(rebooked, contacted)
        opt_out_rate = _rate(len(opt_out_jobs), contacted)
        no_show_delta = _no_show_delta(session, start, end)

    return RoiMetrics(
        start=start,
        end=end,
        contacted=contacted,
        rebooked=rebooked,
        conversion_rate=conversion_rate,
        recovered_revenue=revenue,
        no_show_delta=no_show_delta,
        opt_out_rate=opt_out_rate,
        monthly_net=_money(revenue - subscription - usage),
        roi_multiple=_money(revenue / subscription) if subscription > 0 else ZERO_MONEY,
        subscription_cost=subscription,
        usage_cost=usage,
    )


def roi_metrics_csv(metrics: RoiMetrics) -> str:
    """Render ROI metrics as aggregate-only CSV."""
    output = StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["metric", "value"])
    for key, value in (
        ("start", metrics.start.isoformat()),
        ("end", metrics.end.isoformat()),
        ("contacted", metrics.contacted),
        ("rebooked", metrics.rebooked),
        ("conversion_rate", metrics.conversion_rate),
        ("recovered_revenue", metrics.recovered_revenue),
        ("no_show_delta", metrics.no_show_delta),
        ("opt_out_rate", metrics.opt_out_rate),
        ("subscription_cost", metrics.subscription_cost),
        ("usage_cost", metrics.usage_cost),
        ("monthly_net", metrics.monthly_net),
        ("roi_multiple", metrics.roi_multiple),
    ):
        writer.writerow([key, value])
    return output.getvalue()


def _contacted_job_ids(session: Session, start: datetime, end: datetime) -> set[str]:
    rows = session.execute(
        tenant_select(Interaction).where(
            Interaction.direction == InteractionDirection.OUTBOUND,
            Interaction.occurred_at >= start,
            Interaction.occurred_at < end,
        )
    ).scalars()
    return {row.outreach_job_id for row in rows}


def _opt_out_job_ids(session: Session, start: datetime, end: datetime) -> set[str]:
    rows = session.execute(
        tenant_select(Interaction).where(
            Interaction.intent == InteractionIntent.OPT_OUT,
            Interaction.occurred_at >= start,
            Interaction.occurred_at < end,
        )
    ).scalars()
    return {row.outreach_job_id for row in rows}


def _completed_bookings(session: Session, start: datetime, end: datetime) -> list[BookingAction]:
    return list(
        session.execute(
            tenant_select(BookingAction).where(
                BookingAction.status == BookingActionStatus.COMPLETED,
                BookingAction.created_at >= start,
                BookingAction.created_at < end,
            )
        ).scalars()
    )


def _booking_appointments(
    session: Session, completed_bookings: list[BookingAction]
) -> list[Appointment]:
    if not completed_bookings:
        return []
    appointment_ids = {booking.appointment_id for booking in completed_bookings}
    return list(
        session.execute(
            tenant_select(Appointment).where(Appointment.id.in_(appointment_ids))
        ).scalars()
    )


def _no_show_delta(session: Session, start: datetime, end: datetime) -> float:
    duration = end - start
    if duration <= timedelta(0):
        return 0.0
    previous_start = start - duration
    previous_rate = _no_show_rate(session, previous_start, start)
    current_rate = _no_show_rate(session, start, end)
    return round(previous_rate - current_rate, 4)


def _no_show_rate(session: Session, start: datetime, end: datetime) -> float:
    appointments = list(
        session.execute(
            tenant_select(Appointment).where(
                Appointment.start_at >= start,
                Appointment.start_at < end,
            )
        ).scalars()
    )
    if not appointments:
        return 0.0
    no_shows = sum(1 for appointment in appointments if appointment.status in NO_SHOW_STATUSES)
    return _rate(no_shows, len(appointments))


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _money(value: Decimal | int | float) -> Decimal:
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    return decimal_value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _require_aware(field: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")