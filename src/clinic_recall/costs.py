"""Per-interaction cost estimates for Clinic Recall NFR telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .enums import Channel, InteractionDirection
from .models import Interaction

DEFAULT_SMS_COST = Decimal("0.05")
DEFAULT_EMAIL_COST = Decimal("0.00")
DEFAULT_CALL_COST = Decimal("0.25")


@dataclass(frozen=True)
class InteractionCostSummary:
    """Aggregate usage-cost estimate for a clinic and time window."""

    sms_count: int
    email_count: int
    call_count: int
    sms_cost: Decimal
    email_cost: Decimal
    call_cost: Decimal

    @property
    def total_estimated_cost(self) -> Decimal:
        return self.sms_cost + self.email_cost + self.call_cost

    def as_summary(self) -> dict[str, object]:
        return {
            "sms_count": self.sms_count,
            "email_count": self.email_count,
            "call_count": self.call_count,
            "sms_cost": str(self.sms_cost),
            "email_cost": str(self.email_cost),
            "call_cost": str(self.call_cost),
            "total_estimated_cost": str(self.total_estimated_cost),
        }


def get_interaction_cost_summary(
    session: Session,
    clinic_id: str,
    *,
    start: datetime,
    end: datetime,
    sms_unit_cost: Decimal = DEFAULT_SMS_COST,
    email_unit_cost: Decimal = DEFAULT_EMAIL_COST,
    call_unit_cost: Decimal = DEFAULT_CALL_COST,
) -> InteractionCostSummary:
    """Estimate outbound interaction costs for one scoped clinic window."""
    _require_aware("start", start)
    _require_aware("end", end)
    if end <= start:
        raise ValueError("end must be after start")

    counts = {Channel.SMS: 0, Channel.EMAIL: 0, Channel.CALL: 0}
    with clinic_scope(session, clinic_id):
        interactions = list(
            session.execute(
                tenant_select(Interaction).where(Interaction.direction == InteractionDirection.OUTBOUND)
            ).scalars()
        )
        for interaction in interactions:
            occurred_at = _as_utc(interaction.occurred_at)
            if start <= occurred_at < end and interaction.channel in counts:
                counts[interaction.channel] += 1

    return InteractionCostSummary(
        sms_count=counts[Channel.SMS],
        email_count=counts[Channel.EMAIL],
        call_count=counts[Channel.CALL],
        sms_cost=sms_unit_cost * counts[Channel.SMS],
        email_cost=email_unit_cost * counts[Channel.EMAIL],
        call_cost=call_unit_cost * counts[Channel.CALL],
    )


def _require_aware(field: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)