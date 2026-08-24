"""Sync source interface and normalised records.

A :class:`SyncSource` turns an external system (a CSV export, or later the
Cliniko API) into ``NormalizedPatient`` / ``NormalizedAppointment`` records in
our own vocabulary. The persistence layer (:mod:`clinic_recall.sync.upsert`)
then idempotently upserts those records keyed on ``(clinic_id, source_ref)``.

Keeping the interface this small means adding a new PMS later is a single new
adapter, and the upsert / detection / eligibility code never changes.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from ..enums import AppointmentStatus

# Stable namespace so generated ids are deterministic across sync runs.
_ID_NAMESPACE = uuid.UUID("5f9b9d4e-2e2a-4d8e-9b1a-0c3a6f7c1e10")


def make_id(prefix: str, clinic_id: str, source_ref: str) -> str:
    """Return a deterministic id for a ``(clinic_id, source_ref)`` pair.

    Determinism keeps new-row creation stable across re-runs; idempotency itself
    is enforced by the unique ``(clinic_id, source_ref)`` lookup in the upsert.
    """
    digest = uuid.uuid5(_ID_NAMESPACE, f"{clinic_id}:{source_ref}").hex
    return f"{prefix}-{digest}"


@dataclass(frozen=True)
class NormalizedPatient:
    """A patient in our vocabulary, ready to upsert.

    ``None`` consent or opt-out flags mean the source is not authoritative for
    that field. An explicit dictionary, including an empty one, is
    authoritative. New records still default to empty fail-closed flags.
    """

    source_ref: str
    name: str
    phone: str | None = None
    email: str | None = None
    consent_flags: dict[str, bool] | None = None
    opt_out_flags: dict[str, bool] | None = None
    contact_prefs: dict[str, object] | None = None
    # ``None`` preserves the legacy authoritative-source contract (all basic
    # patient fields may update). CSV materialization supplies an explicit set
    # so absent/empty optional contact cells do not erase existing values.
    authoritative_fields: frozenset[str] | None = None


@dataclass(frozen=True)
class NormalizedAppointment:
    """An appointment in our vocabulary, ready to upsert."""

    source_ref: str
    patient_source_ref: str
    status: AppointmentStatus
    start_at: datetime
    value: Decimal | None = None


@runtime_checkable
class SyncSource(Protocol):
    """A read-only source of clinic patients and appointments."""

    name: str

    def fetch_patients(self) -> Sequence[NormalizedPatient]:
        """Return the distinct patients to upsert."""
        ...

    def fetch_appointments(self) -> Sequence[NormalizedAppointment]:
        """Return the appointments to upsert."""
        ...
