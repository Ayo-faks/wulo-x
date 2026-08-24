"""Deterministic provider-sourced appointment availability."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .enums import BookingActionStatus
from .models import AvailabilitySlot, BookingAction

_MAX_FRESHNESS_DURATION = timedelta(minutes=30)


class AvailabilityConflictError(ValueError):
    """A bounded conflict that never includes provider values."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class AvailabilityPreflightReason(StrEnum):
    """Closed result vocabulary for a selected-slot read comparison."""

    MATCH = "match"
    STALE = "stale"
    MISSING = "missing"
    PROVIDER_MISMATCH = "provider_mismatch"
    SOURCE_MISMATCH = "source_mismatch"
    BUSINESS_MISMATCH = "business_mismatch"
    PRACTITIONER_MISMATCH = "practitioner_mismatch"
    APPOINTMENT_TYPE_MISMATCH = "appointment_type_mismatch"
    START_MISMATCH = "start_mismatch"
    END_MISMATCH = "end_mismatch"
    ALREADY_CLAIMED = "already_claimed"


@dataclass(frozen=True)
class AvailabilitySlotSignature:
    """Immutable provider-neutral facts compared immediately before a write."""

    source_ref: str
    source_provider: str
    business_id: str
    practitioner_id: str
    appointment_type_id: str
    start_at: datetime
    end_at: datetime
    fetched_at: datetime
    expires_at: datetime
    available: bool

    def __post_init__(self) -> None:
        for field in (
            "source_ref",
            "source_provider",
            "business_id",
            "practitioner_id",
            "appointment_type_id",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} is required")
        for field in ("start_at", "end_at", "fetched_at", "expires_at"):
            value = getattr(self, field)
            _require_aware(field, value)
            object.__setattr__(self, field, _as_utc(value))
        if self.end_at <= self.start_at:
            raise ValueError("end_at must follow start_at")
        if self.expires_at <= self.fetched_at:
            raise ValueError("expires_at must follow fetched_at")


@dataclass(frozen=True)
class AvailabilityPreflightResult:
    """Value-free result of comparing one selected and one observed slot."""

    matches: bool
    reason: AvailabilityPreflightReason

    def as_dict(self) -> dict[str, object]:
        return {"matches": self.matches, "reason": self.reason.value}


@dataclass(frozen=True)
class AvailabilitySlotInput:
    """One slot supplied by a deterministic PMS/calendar adapter."""

    source_ref: str
    start_at: datetime
    end_at: datetime
    source_provider: str | None = None
    business_id: str | None = None
    appointment_type_id: str | None = None
    clinician_id: str | None = None
    appointment_id: str | None = None
    details: dict[str, Any] | None = None
    fetched_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class AvailabilitySlotSummary:
    """Patient-safe slot facts returned to the voice agent."""

    slot_id: str
    start_at: datetime
    end_at: datetime
    clinician_id: str | None = None
    details: dict[str, Any] | None = None
    source_ref: str | None = None
    source_provider: str | None = None
    business_id: str | None = None
    appointment_type_id: str | None = None
    fetched_at: datetime | None = None
    expires_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
        }


@runtime_checkable
class AvailabilityProvider(Protocol):
    """Deterministic source of real appointment slots."""

    name: str

    def list_slots(
        self,
        *,
        clinic_id: str,
        window_start: datetime,
        window_end: datetime,
        clinician_id: str | None = None,
    ) -> Sequence[AvailabilitySlotInput]:
        """Return real slots from an external PMS/calendar."""
        ...


class FakeAvailabilityProvider:
    """Offline provider used by tests and local ART simulations."""

    name = "fake"

    def __init__(self, slots: Iterable[AvailabilitySlotInput] = ()) -> None:
        self.slots = list(slots)

    def list_slots(
        self,
        *,
        clinic_id: str,
        window_start: datetime,
        window_end: datetime,
        clinician_id: str | None = None,
    ) -> Sequence[AvailabilitySlotInput]:
        return [
            slot
            for slot in self.slots
            if slot.start_at >= window_start
            and slot.start_at < window_end
            and (clinician_id is None or slot.clinician_id == clinician_id)
        ]


def upsert_availability_slots(
    session: Session,
    clinic_id: str,
    slots: Iterable[AvailabilitySlotInput],
    *,
    now: datetime | None = None,
) -> list[AvailabilitySlotSummary]:
    """Persist provider slots idempotently for later tool reads."""
    materialized = tuple(slots)
    for slot in materialized:
        _validate_slot_input(slot, now=now)
    summaries: list[AvailabilitySlotSummary] = []
    with clinic_scope(session, clinic_id):
        with session.begin_nested():
            for slot in materialized:
                row = session.execute(
                    tenant_select(AvailabilitySlot).where(
                        AvailabilitySlot.source_ref == slot.source_ref
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = AvailabilitySlot(
                        id=f"slot-{uuid.uuid4().hex}",
                        clinic_id=clinic_id,
                        source_ref=slot.source_ref,
                        start_at=_as_utc(slot.start_at),
                        end_at=_as_utc(slot.end_at),
                    )
                    session.add(row)
                    _assign_new_slot(row, slot)
                elif slot.source_provider is None:
                    if row.source_provider is not None:
                        raise AvailabilityConflictError("authority_downgrade")
                    row.start_at = _as_utc(slot.start_at)
                    row.end_at = _as_utc(slot.end_at)
                    row.clinician_id = slot.clinician_id
                    row.appointment_id = slot.appointment_id
                    row.details = dict(slot.details or {})
                else:
                    _apply_authoritative_observation(row, slot)
                summaries.append(_summary(row))
            session.flush()
    return summaries


def get_availability(
    session: Session,
    clinic_id: str,
    *,
    now: datetime,
    window_start: datetime,
    window_end: datetime,
    clinician_id: str | None = None,
    limit: int = 5,
) -> list[AvailabilitySlotSummary]:
    """Return only unbooked provider-sourced slots inside the requested window."""
    _require_aware("now", now)
    _require_aware("window_start", window_start)
    _require_aware("window_end", window_end)
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")
    effective_start = max(_as_utc(now), _as_utc(window_start))
    current = _as_utc(now)
    effective_end = _as_utc(window_end)
    bounded_limit = max(1, min(limit, 10))

    with clinic_scope(session, clinic_id):
        booked_slots = (
            select(BookingAction.availability_slot_id)
            .where(
                BookingAction.clinic_id == clinic_id,
                BookingAction.availability_slot_id.is_not(None),
                BookingAction.status != BookingActionStatus.REJECTED,
            )
            .subquery()
        )
        stmt = (
            tenant_select(AvailabilitySlot)
            .where(
                AvailabilitySlot.start_at >= effective_start,
                AvailabilitySlot.start_at < effective_end,
                AvailabilitySlot.source_provider.is_not(None),
                AvailabilitySlot.business_id.is_not(None),
                AvailabilitySlot.clinician_id.is_not(None),
                AvailabilitySlot.appointment_type_id.is_not(None),
                AvailabilitySlot.fetched_at.is_not(None),
                AvailabilitySlot.fetched_at <= current,
                AvailabilitySlot.expires_at.is_not(None),
                AvailabilitySlot.expires_at > current,
                AvailabilitySlot.id.not_in(select(booked_slots.c.availability_slot_id)),
            )
            .order_by(AvailabilitySlot.start_at, AvailabilitySlot.id)
            .limit(bounded_limit)
        )
        if clinician_id:
            stmt = stmt.where(AvailabilitySlot.clinician_id == clinician_id)
        return [_summary(slot) for slot in session.execute(stmt).scalars()]


def compare_availability_preflight(
    selected: AvailabilitySlotSignature,
    observed: AvailabilitySlotSignature | None,
    *,
    now: datetime,
    already_claimed: bool = False,
) -> AvailabilityPreflightResult:
    """Compare selected and newly observed facts without provider or DB effects."""
    _require_aware("now", now)
    if observed is None:
        return _preflight(AvailabilityPreflightReason.MISSING)
    if already_claimed:
        return _preflight(AvailabilityPreflightReason.ALREADY_CLAIMED)
    comparisons = (
        (
            selected.source_provider,
            observed.source_provider,
            AvailabilityPreflightReason.PROVIDER_MISMATCH,
        ),
        (
            selected.source_ref,
            observed.source_ref,
            AvailabilityPreflightReason.SOURCE_MISMATCH,
        ),
        (
            selected.business_id,
            observed.business_id,
            AvailabilityPreflightReason.BUSINESS_MISMATCH,
        ),
        (
            selected.practitioner_id,
            observed.practitioner_id,
            AvailabilityPreflightReason.PRACTITIONER_MISMATCH,
        ),
        (
            selected.appointment_type_id,
            observed.appointment_type_id,
            AvailabilityPreflightReason.APPOINTMENT_TYPE_MISMATCH,
        ),
        (
            selected.start_at,
            observed.start_at,
            AvailabilityPreflightReason.START_MISMATCH,
        ),
        (
            selected.end_at,
            observed.end_at,
            AvailabilityPreflightReason.END_MISMATCH,
        ),
    )
    for selected_value, observed_value, reason in comparisons:
        if selected_value != observed_value:
            return _preflight(reason)
    current = _as_utc(now)
    if any(
        not signature.available
        or signature.fetched_at > current
        or signature.expires_at <= current
        for signature in (selected, observed)
    ):
        return _preflight(AvailabilityPreflightReason.STALE)
    return AvailabilityPreflightResult(True, AvailabilityPreflightReason.MATCH)


def _validate_slot_input(
    slot: AvailabilitySlotInput,
    *,
    now: datetime | None,
) -> None:
    if not slot.source_ref.strip():
        raise ValueError("slot source_ref is required")
    _require_aware("start_at", slot.start_at)
    _require_aware("end_at", slot.end_at)
    if slot.end_at <= slot.start_at:
        raise ValueError("slot end_at must be after start_at")
    authoritative_fields = (
        slot.source_provider,
        slot.business_id,
        slot.clinician_id,
        slot.appointment_type_id,
        slot.fetched_at,
        slot.expires_at,
    )
    if slot.source_provider is None:
        if any(
            value is not None
            for value in (
                slot.business_id,
                slot.appointment_type_id,
                slot.fetched_at,
                slot.expires_at,
            )
        ):
            raise ValueError("authoritative slot binding is incomplete")
        return
    if any(value is None for value in authoritative_fields):
        raise ValueError("authoritative slot binding is incomplete")
    for field in (
        "source_provider",
        "business_id",
        "clinician_id",
        "appointment_type_id",
    ):
        value = getattr(slot, field)
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 200
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("authoritative slot binding is incomplete")
    if slot.details is not None:
        raise ValueError("authoritative slot details are not permitted")
    if now is None:
        raise ValueError("now is required for authoritative availability")
    _require_aware("now", now)
    assert slot.fetched_at is not None
    assert slot.expires_at is not None
    _require_aware("fetched_at", slot.fetched_at)
    _require_aware("expires_at", slot.expires_at)
    fetched_at = _as_utc(slot.fetched_at)
    expires_at = _as_utc(slot.expires_at)
    if fetched_at > _as_utc(now):
        raise ValueError("fetched_at must not be in the future")
    if expires_at <= fetched_at:
        raise ValueError("slot expires_at must be after fetched_at")
    if expires_at - fetched_at > _MAX_FRESHNESS_DURATION:
        raise ValueError("slot freshness duration exceeds policy")


def _assign_new_slot(row: AvailabilitySlot, slot: AvailabilitySlotInput) -> None:
    row.clinician_id = slot.clinician_id
    row.appointment_id = slot.appointment_id
    if slot.source_provider is None:
        row.details = dict(slot.details or {})
        return
    row.source_provider = slot.source_provider
    row.business_id = slot.business_id
    row.appointment_type_id = slot.appointment_type_id
    row.fetched_at = _as_utc(slot.fetched_at) if slot.fetched_at else None
    row.expires_at = _as_utc(slot.expires_at) if slot.expires_at else None
    row.details = {}


def _apply_authoritative_observation(
    row: AvailabilitySlot,
    slot: AvailabilitySlotInput,
) -> None:
    incoming_binding = (
        slot.source_provider,
        slot.business_id,
        slot.clinician_id,
        slot.appointment_type_id,
    )
    if row.source_provider is None:
        existing_clinician = row.clinician_id
        if existing_clinician is not None and existing_clinician != slot.clinician_id:
            raise AvailabilityConflictError("binding_mismatch")
        if (
            _as_utc(row.start_at) != _as_utc(slot.start_at)
            or _as_utc(row.end_at) != _as_utc(slot.end_at)
        ):
            raise AvailabilityConflictError("slot_signature_mismatch")
        _assign_new_slot(row, slot)
        return
    existing_binding = (
        row.source_provider,
        row.business_id,
        row.clinician_id,
        row.appointment_type_id,
    )
    if existing_binding != incoming_binding:
        raise AvailabilityConflictError("binding_mismatch")
    if row.fetched_at is None or row.expires_at is None:
        raise AvailabilityConflictError("persisted_observation_invalid")
    assert slot.fetched_at is not None
    assert slot.expires_at is not None
    incoming_fetched_at = _as_utc(slot.fetched_at)
    existing_fetched_at = _as_utc(row.fetched_at)
    same_signature = (
        _as_utc(row.start_at) == _as_utc(slot.start_at)
        and _as_utc(row.end_at) == _as_utc(slot.end_at)
    )
    if not same_signature:
        raise AvailabilityConflictError("slot_signature_mismatch")
    if incoming_fetched_at < existing_fetched_at:
        return
    if incoming_fetched_at == existing_fetched_at:
        if _as_utc(row.expires_at) != _as_utc(slot.expires_at):
            raise AvailabilityConflictError("equal_observation_conflict")
        return
    row.fetched_at = incoming_fetched_at
    row.expires_at = _as_utc(slot.expires_at)


def _preflight(reason: AvailabilityPreflightReason) -> AvailabilityPreflightResult:
    return AvailabilityPreflightResult(False, reason)


def _require_aware(field: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _summary(slot: AvailabilitySlot) -> AvailabilitySlotSummary:
    return AvailabilitySlotSummary(
        slot_id=slot.id,
        start_at=_as_utc(slot.start_at),
        end_at=_as_utc(slot.end_at),
        clinician_id=slot.clinician_id,
        details=slot.details,
        source_ref=slot.source_ref,
        source_provider=slot.source_provider,
        business_id=slot.business_id,
        appointment_type_id=slot.appointment_type_id,
        fetched_at=_as_utc(slot.fetched_at) if slot.fetched_at else None,
        expires_at=_as_utc(slot.expires_at) if slot.expires_at else None,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)