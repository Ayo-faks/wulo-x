"""Cliniko DTO parsing and side-effect-free materialized sync source."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from ..enums import AppointmentStatus
from .base import NormalizedAppointment, NormalizedPatient
from .cliniko_client import ClinikoClient, ClinikoContractError

# Cliniko encodes appointment state across booking fields rather than one status
# string. The live adapter should apply these rules in order:
#
#   1. cancelled_at is set                 -> CANCELLED
#   2. did_not_arrive is true              -> NO_SHOW
#   3. start_at in the past and attended   -> COMPLETED
#   4. otherwise                           -> SCHEDULED
#
# CLINIKO_STATUS_MAP covers any explicit textual status that may also appear.
CLINIKO_STATUS_MAP: dict[str, AppointmentStatus] = {
    "booked": AppointmentStatus.SCHEDULED,
    "arrived": AppointmentStatus.COMPLETED,
    "completed": AppointmentStatus.COMPLETED,
    "did_not_arrive": AppointmentStatus.NO_SHOW,
    "cancelled": AppointmentStatus.CANCELLED,
}

_ID_PATTERN = re.compile(r"[1-9][0-9]*")
_EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def _identifier(value: object, reason_code: str) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str) and _ID_PATTERN.fullmatch(value) is not None:
        return value
    raise ClinikoContractError(reason_code)


def _required_string(payload: Mapping[str, object], name: str, reason_code: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise ClinikoContractError(reason_code)
    stripped = value.strip()
    if not stripped or len(stripped) > 255 or any(ord(char) < 32 for char in stripped):
        raise ClinikoContractError(reason_code)
    return stripped


def _optional_string(
    payload: Mapping[str, object],
    name: str,
    reason_code: str,
) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ClinikoContractError(reason_code)
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > 255 or any(ord(char) < 32 for char in stripped):
        raise ClinikoContractError(reason_code)
    return stripped


def _datetime_value(value: object, reason_code: str, *, optional: bool) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ClinikoContractError(reason_code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ClinikoContractError(reason_code) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ClinikoContractError(reason_code)
    return parsed.astimezone(UTC)


def _optional_bool(payload: Mapping[str, object], name: str, reason_code: str) -> bool | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ClinikoContractError(reason_code)
    return value


def _patient_phone(payload: Mapping[str, object]) -> str | None:
    raw_numbers = payload.get("patient_phone_numbers")
    if raw_numbers is None:
        return None
    if not isinstance(raw_numbers, list):
        raise ClinikoContractError("patient_phone_numbers")
    for raw_number in raw_numbers:
        if not isinstance(raw_number, dict):
            raise ClinikoContractError("patient_phone_numbers")
        normalized = raw_number.get("normalized_number")
        if normalized is None or normalized == "":
            continue
        if not isinstance(normalized, str):
            raise ClinikoContractError("patient_phone_numbers")
        candidate = normalized.strip()
        digits = candidate[1:] if candidate.startswith("+") else candidate
        if not digits.isdigit() or not 7 <= len(digits) <= 15 or digits.startswith("0"):
            raise ClinikoContractError("patient_phone_numbers")
        return f"+{digits}"
    return None


def _linked_patient_id(payload: Mapping[str, object], base_url: str) -> str:
    patient = payload.get("patient")
    if not isinstance(patient, dict):
        raise ClinikoContractError("appointment_patient")
    links = patient.get("links")
    if not isinstance(links, dict):
        raise ClinikoContractError("appointment_patient")
    self_url = links.get("self")
    if not isinstance(self_url, str):
        raise ClinikoContractError("appointment_patient")
    try:
        parsed = urlsplit(self_url)
        expected = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        raise ClinikoContractError("appointment_patient") from None
    match = re.fullmatch(r"/v1/patients/([1-9][0-9]*)", parsed.path)
    if (
        parsed.scheme != expected.scheme
        or parsed.hostname != expected.hostname
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        raise ClinikoContractError("appointment_patient")
    return match.group(1)


@dataclass(frozen=True)
class ClinikoPatientRecord:
    """Documented patient fields needed for normalization and Gate M evidence."""

    source_ref: str
    first_name: str = field(repr=False)
    preferred_first_name: str | None = field(repr=False)
    last_name: str = field(repr=False)
    phone: str | None = field(repr=False)
    email: str | None = field(repr=False)
    archived_at: datetime | None
    updated_at: datetime

    @classmethod
    def from_payload(cls, payload: object) -> ClinikoPatientRecord:
        if not isinstance(payload, dict):
            raise ClinikoContractError("patient_schema")
        email = _optional_string(payload, "email", "patient_email")
        if email is not None and _EMAIL_PATTERN.fullmatch(email) is None:
            raise ClinikoContractError("patient_email")
        updated_at = _datetime_value(
            payload.get("updated_at"), "patient_updated_at", optional=False
        )
        assert updated_at is not None
        return cls(
            source_ref=_identifier(payload.get("id"), "patient_id"),
            first_name=_required_string(payload, "first_name", "patient_first_name"),
            preferred_first_name=_optional_string(
                payload, "preferred_first_name", "patient_preferred_first_name"
            ),
            last_name=_required_string(payload, "last_name", "patient_last_name"),
            phone=_patient_phone(payload),
            email=email.lower() if email is not None else None,
            archived_at=_datetime_value(
                payload.get("archived_at"), "patient_archived_at", optional=True
            ),
            updated_at=updated_at,
        )

    def normalize(self) -> NormalizedPatient:
        """Map contact fields without inferring consent from provider marketing flags."""
        given_name = self.preferred_first_name or self.first_name
        return NormalizedPatient(
            source_ref=self.source_ref,
            name=f"{given_name} {self.last_name}",
            phone=self.phone,
            email=self.email,
            consent_flags=None,
            opt_out_flags=None,
        )


@dataclass(frozen=True)
class ClinikoAppointmentRecord:
    """Documented individual-appointment fields needed for normalization."""

    source_ref: str
    patient_source_ref: str
    starts_at: datetime
    ends_at: datetime
    cancelled_at: datetime | None
    did_not_arrive: bool | None
    patient_arrived: bool | None
    archived_at: datetime | None
    updated_at: datetime

    @classmethod
    def from_payload(
        cls,
        payload: object,
        *,
        base_url: str,
    ) -> ClinikoAppointmentRecord:
        if not isinstance(payload, dict):
            raise ClinikoContractError("appointment_schema")
        starts_at = _datetime_value(
            payload.get("starts_at"), "appointment_starts_at", optional=False
        )
        ends_at = _datetime_value(
            payload.get("ends_at"), "appointment_ends_at", optional=False
        )
        updated_at = _datetime_value(
            payload.get("updated_at"), "appointment_updated_at", optional=False
        )
        assert starts_at is not None and ends_at is not None and updated_at is not None
        if ends_at <= starts_at:
            raise ClinikoContractError("appointment_interval")
        return cls(
            source_ref=_identifier(payload.get("id"), "appointment_id"),
            patient_source_ref=_linked_patient_id(payload, base_url),
            starts_at=starts_at,
            ends_at=ends_at,
            cancelled_at=_datetime_value(
                payload.get("cancelled_at"), "appointment_cancelled_at", optional=True
            ),
            did_not_arrive=_optional_bool(
                payload, "did_not_arrive", "appointment_did_not_arrive"
            ),
            patient_arrived=_optional_bool(
                payload, "patient_arrived", "appointment_patient_arrived"
            ),
            archived_at=_datetime_value(
                payload.get("archived_at"), "appointment_archived_at", optional=True
            ),
            updated_at=updated_at,
        )

    def normalize(self, *, now: datetime) -> NormalizedAppointment:
        """Apply deterministic state precedence without inventing archive semantics."""
        if self.cancelled_at is not None:
            status = AppointmentStatus.CANCELLED
        elif self.did_not_arrive is True:
            status = AppointmentStatus.NO_SHOW
        elif self.patient_arrived is True and self.starts_at <= now:
            status = AppointmentStatus.COMPLETED
        else:
            status = AppointmentStatus.SCHEDULED
        return NormalizedAppointment(
            source_ref=self.source_ref,
            patient_source_ref=self.patient_source_ref,
            status=status,
            start_at=self.starts_at,
            value=None,
        )


@dataclass(frozen=True)
class ClinikoSyncQuery:
    """Documented incremental read filter; lifecycle reconciliation is Gate M."""

    updated_after: datetime | None = None


@dataclass(frozen=True)
class ClinikoSyncSource:
    """Immutable in-memory Cliniko snapshot; its fetch methods perform no I/O."""

    name = "cliniko"
    _patients: tuple[NormalizedPatient, ...] = field(repr=False)
    _appointments: tuple[NormalizedAppointment, ...] = field(repr=False)

    def fetch_patients(self) -> tuple[NormalizedPatient, ...]:
        """Return the fully validated patient snapshot."""
        return self._patients

    def fetch_appointments(self) -> tuple[NormalizedAppointment, ...]:
        """Return the fully validated appointment snapshot."""
        return self._appointments


def materialize_cliniko_source(
    client: ClinikoClient,
    *,
    query: ClinikoSyncQuery | None = None,
    now: datetime | None = None,
) -> ClinikoSyncSource:
    """Fetch and validate both resources before creating a SyncSource snapshot."""
    effective_query = query or ClinikoSyncQuery()
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ClinikoContractError("now")
    current = current.astimezone(UTC)

    params: list[tuple[str, str]] = [("per_page", str(client.per_page))]
    if effective_query.updated_after is not None:
        updated_after = effective_query.updated_after
        if updated_after.tzinfo is None or updated_after.utcoffset() is None:
            raise ClinikoContractError("updated_after")
        cursor = updated_after.astimezone(UTC).isoformat().replace("+00:00", "Z")
        params.append(("q[]", f"updated_at:>{cursor}"))

    patient_payloads = client.get_collection(
        "patients",
        collection_key="patients",
        params=params,
    )
    appointment_payloads = client.get_collection(
        "individual_appointments",
        collection_key="individual_appointments",
        params=params,
    )
    patient_records = tuple(
        ClinikoPatientRecord.from_payload(payload) for payload in patient_payloads
    )
    appointment_records = tuple(
        ClinikoAppointmentRecord.from_payload(payload, base_url=client.base_url)
        for payload in appointment_payloads
    )
    return ClinikoSyncSource(
        _patients=tuple(record.normalize() for record in patient_records),
        _appointments=tuple(record.normalize(now=current) for record in appointment_records),
    )
