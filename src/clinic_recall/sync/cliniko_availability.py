"""Strict Cliniko available-time mapping into provider-neutral slot inputs."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..availability import AvailabilitySlotInput
from .cliniko_capability import EvidenceAuthority
from .cliniko_client import (
    ClinikoClient,
    ClinikoContractError,
    ClinikoPaginationError,
)

_IDENTIFIER_PATTERN = re.compile(r"[1-9][0-9]*")
_PROVIDER_NAME = "cliniko"
_PROVIDER_VERSION = "v1"
_MAX_FRESHNESS = timedelta(minutes=30)
_MAX_APPOINTMENT_DURATION = timedelta(days=1)


class ClinikoAvailabilityConfigurationError(ValueError):
    """A bounded trusted-binding or request configuration failure."""


@dataclass(frozen=True)
class ClinikoAvailabilityBinding:
    """Server-owned identity, duration, and freshness for one slot resource."""

    clinic_id: str
    business_id: str
    practitioner_id: str
    appointment_type_id: str
    appointment_duration: timedelta
    freshness_duration: timedelta
    evidence_authority: EvidenceAuthority = EvidenceAuthority.FIXTURE_VERIFIED

    def __post_init__(self) -> None:
        if (
            not isinstance(self.clinic_id, str)
            or not self.clinic_id.strip()
            or len(self.clinic_id) > 200
            or any(ord(character) < 32 for character in self.clinic_id)
        ):
            raise ClinikoAvailabilityConfigurationError("clinic_id")
        for name in ("business_id", "practitioner_id", "appointment_type_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
                raise ClinikoAvailabilityConfigurationError(name)
        if (
            not isinstance(self.appointment_duration, timedelta)
            or not timedelta(0)
            < self.appointment_duration
            < _MAX_APPOINTMENT_DURATION
        ):
            raise ClinikoAvailabilityConfigurationError("appointment_duration")
        if (
            not isinstance(self.freshness_duration, timedelta)
            or not timedelta(0) < self.freshness_duration <= _MAX_FRESHNESS
        ):
            raise ClinikoAvailabilityConfigurationError("freshness_duration")
        if self.evidence_authority not in {
            EvidenceAuthority.FIXTURE_VERIFIED,
            EvidenceAuthority.SANDBOX_READ_VERIFIED,
        }:
            raise ClinikoAvailabilityConfigurationError("evidence_authority")


class ClinikoAvailabilityProvider:
    """Materialize strict Cliniko available-time pages without persistence."""

    name = _PROVIDER_NAME

    def __init__(
        self,
        client: ClinikoClient,
        binding: ClinikoAvailabilityBinding,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._binding = binding
        self._clock = clock

    def list_slots(
        self,
        *,
        clinic_id: str,
        window_start: datetime,
        window_end: datetime,
        clinician_id: str | None = None,
    ) -> Sequence[AvailabilitySlotInput]:
        """Fetch and strictly materialize all pages for the trusted binding."""
        if clinic_id != self._binding.clinic_id:
            raise ClinikoAvailabilityConfigurationError("clinic_binding_mismatch")
        if clinician_id is not None and clinician_id != self._binding.practitioner_id:
            raise ClinikoAvailabilityConfigurationError(
                "practitioner_binding_mismatch"
            )
        start = _aware_utc(window_start, "window_start")
        end = _aware_utc(window_end, "window_end")
        if end <= start:
            raise ClinikoAvailabilityConfigurationError("availability_window")
        from_date = start.date()
        to_date = (end - timedelta(microseconds=1)).date()
        if not 0 <= (to_date - from_date).days <= 7:
            raise ClinikoAvailabilityConfigurationError("availability_window")

        fetched_at = _aware_utc(self._clock(), "clock")
        expires_at = fetched_at + self._binding.freshness_duration
        pages = 0
        item_count = 0
        expected_total: int | None = None
        next_url: str | None = None
        seen_urls: set[str] = set()
        seen_starts: set[datetime] = set()
        slots: list[AvailabilitySlotInput] = []

        while True:
            if pages >= self._client.max_pages:
                raise ClinikoPaginationError("max_pages_exceeded")
            if next_url is not None:
                if next_url in seen_urls:
                    raise ClinikoPaginationError("cyclic_next_link")
                seen_urls.add(next_url)
            page = self._client.get_available_times_page(
                business_id=self._binding.business_id,
                practitioner_id=self._binding.practitioner_id,
                appointment_type_id=self._binding.appointment_type_id,
                from_date=from_date,
                to_date=to_date,
                next_url=next_url,
            )
            pages += 1
            if expected_total is None:
                expected_total = page.total_entries
            elif page.total_entries != expected_total:
                raise ClinikoPaginationError("available_times_total_mismatch")
            item_count += len(page.items)
            if item_count > self._client.max_items:
                raise ClinikoPaginationError("max_items_exceeded")

            for payload in page.items:
                appointment_start = _appointment_start(payload)
                if not start <= appointment_start < end:
                    raise ClinikoContractError("appointment_start_window")
                if appointment_start in seen_starts:
                    raise ClinikoContractError("duplicate_available_time")
                seen_starts.add(appointment_start)
                slots.append(
                    AvailabilitySlotInput(
                        source_ref=_source_ref(
                            self._binding,
                            appointment_start=appointment_start,
                        ),
                        source_provider=_PROVIDER_NAME,
                        business_id=self._binding.business_id,
                        appointment_type_id=self._binding.appointment_type_id,
                        clinician_id=self._binding.practitioner_id,
                        start_at=appointment_start,
                        end_at=(
                            appointment_start
                            + self._binding.appointment_duration
                        ),
                        details=None,
                        fetched_at=fetched_at,
                        expires_at=expires_at,
                    )
                )

            if page.next_url is None:
                break
            next_url = page.next_url

        if item_count != expected_total:
            raise ClinikoPaginationError("incomplete_available_times")

        return tuple(sorted(slots, key=lambda slot: (slot.start_at, slot.source_ref)))


def _appointment_start(payload: dict[str, object]) -> datetime:
    if set(payload) != {"appointment_start"}:
        raise ClinikoContractError("available_time_schema")
    raw = payload.get("appointment_start")
    if not isinstance(raw, str):
        raise ClinikoContractError("appointment_start")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ClinikoContractError("appointment_start") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ClinikoContractError("appointment_start_utc")
    if parsed.utcoffset() != timedelta(0):
        raise ClinikoContractError("appointment_start_utc")
    return parsed.astimezone(UTC)


def _source_ref(
    binding: ClinikoAvailabilityBinding,
    *,
    appointment_start: datetime,
) -> str:
    payload = {
        "appointment_start": _rfc3339(appointment_start),
        "appointment_type_id": binding.appointment_type_id,
        "business_id": binding.business_id,
        "practitioner_id": binding.practitioner_id,
        "provider": _PROVIDER_NAME,
        "provider_version": _PROVIDER_VERSION,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return f"{_PROVIDER_NAME}:{_PROVIDER_VERSION}:{hashlib.sha256(encoded).hexdigest()}"


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ClinikoAvailabilityConfigurationError(field)
    return value.astimezone(UTC)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
