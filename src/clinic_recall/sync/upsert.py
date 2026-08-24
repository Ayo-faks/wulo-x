"""Idempotent persistence of normalised sync records.

Upserts patients and appointments under a clinic scope, keyed on
``(clinic_id, source_ref)`` so re-running a sync never creates duplicates. The
unique constraints on those columns are the hard guarantee; the per-key lookup
here is the portable (PostgreSQL and SQLite) implementation. Each run writes one
``sync_upsert`` audit record (SR-05) with a hash of the run's counts.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import clinic_scope
from ..enums import AuditAction, SourceLinkState, SourceSystem
from ..models import Appointment, AuditLog, Patient, PatientSourceLink
from ..rights import SubjectKeyring, assert_source_writable
from .base import SyncSource, make_id


class FlagMerge(StrEnum):
    """How a source's consent/opt-out flags merge into existing patients.

    ``REPLACE`` is the legacy authoritative-source behavior (a provider sync
    owns the whole dict). ``MONOTONIC`` is the CSV-import authority: consent
    may only be added, opt-out may only strengthen, and absent or ``false``
    values never clear existing evidence.
    """

    REPLACE = "replace"
    MONOTONIC = "monotonic"


def _merge_monotonic(
    existing: dict[str, bool] | None, incoming: dict[str, bool]
) -> dict[str, bool]:
    """Per-channel monotonic OR: ``true`` sticks, nothing is ever cleared."""
    merged = dict(existing or {})
    for channel, value in incoming.items():
        merged[channel] = bool(merged.get(channel)) or bool(value)
    return merged


def find_source_patient(
    session: Session,
    clinic_id: str,
    source_ref: str,
    source_system: SourceSystem | None,
) -> Patient | None:
    """Resolve an active provider alias before the legacy primary reference."""
    if source_system is not None:
        linked = session.execute(
            select(Patient)
            .join(
                PatientSourceLink,
                (PatientSourceLink.clinic_id == Patient.clinic_id)
                & (PatientSourceLink.patient_id == Patient.id),
            )
            .where(
                Patient.clinic_id == clinic_id,
                PatientSourceLink.provider == source_system,
                PatientSourceLink.source_ref == source_ref,
                PatientSourceLink.state == SourceLinkState.ACTIVE,
            )
        ).scalar_one_or_none()
        if linked is not None:
            return linked
    return session.execute(
        select(Patient).where(
            Patient.clinic_id == clinic_id,
            Patient.source_ref == source_ref,
        )
    ).scalar_one_or_none()


class SyncIntegrityError(ValueError):
    """Raised when an appointment references a patient absent from the source."""


@dataclass
class SyncResult:
    """Counts from a single sync run."""

    patients_inserted: int = 0
    patients_updated: int = 0
    appointments_inserted: int = 0
    appointments_updated: int = 0

    @property
    def total(self) -> int:
        """Total rows written (inserted or updated)."""
        return (
            self.patients_inserted
            + self.patients_updated
            + self.appointments_inserted
            + self.appointments_updated
        )


def _hash_payload(payload: dict[str, object]) -> str:
    """SHA-256 of a JSON-stable payload (no PII stored in the clear)."""
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def upsert_source(
    session: Session,
    clinic_id: str,
    source: SyncSource,
    *,
    keyring: SubjectKeyring | None = None,
    flag_merge: FlagMerge = FlagMerge.REPLACE,
    source_system: SourceSystem | None = None,
) -> SyncResult:
    """Idempotently upsert a source's patients and appointments for one clinic.

    Args:
        session: An open SQLAlchemy session (the caller commits).
        clinic_id: The owning clinic; all writes are scoped to it.
        source: The sync source to read normalised records from.
        keyring: Rights tombstone keys; loaded from configuration when omitted.
        flag_merge: Consent/opt-out merge authority (see :class:`FlagMerge`).

    Returns:
        A :class:`SyncResult` with insert/update counts.

    Raises:
        SyncIntegrityError: If an appointment references an unknown patient.
    """
    if keyring is None:
        from ..config import get_rights_subject_keyring

        keyring = get_rights_subject_keyring()
    incoming_patients = tuple(source.fetch_patients())
    incoming_appointments = tuple(source.fetch_appointments())
    result = SyncResult()
    with clinic_scope(session, clinic_id):
        patient_id_by_ref: dict[str, str] = {}
        for incoming in incoming_patients:
            assert_source_writable(
                session,
                clinic_id,
                incoming.source_ref,
                keyring,
            )
            existing = find_source_patient(
                session, clinic_id, incoming.source_ref, source_system
            )
            if existing is not None:
                authoritative = incoming.authoritative_fields
                if authoritative is None or "name" in authoritative:
                    existing.name = incoming.name
                if authoritative is None or "phone" in authoritative:
                    existing.phone = incoming.phone
                if authoritative is None or "email" in authoritative:
                    existing.email = incoming.email
                if incoming.consent_flags is not None:
                    existing.consent_flags = (
                        _merge_monotonic(existing.consent_flags, incoming.consent_flags)
                        if flag_merge is FlagMerge.MONOTONIC
                        else incoming.consent_flags
                    )
                if incoming.opt_out_flags is not None:
                    existing.opt_out_flags = (
                        _merge_monotonic(existing.opt_out_flags, incoming.opt_out_flags)
                        if flag_merge is FlagMerge.MONOTONIC
                        else incoming.opt_out_flags
                    )
                if incoming.contact_prefs is not None:
                    existing.contact_prefs = incoming.contact_prefs
                patient_id_by_ref[incoming.source_ref] = existing.id
                result.patients_updated += 1
            else:
                patient_id = make_id("pat", clinic_id, incoming.source_ref)
                session.add(
                    Patient(
                        id=patient_id,
                        clinic_id=clinic_id,
                        source_ref=incoming.source_ref,
                        name=incoming.name,
                        phone=incoming.phone,
                        email=incoming.email,
                        consent_flags=(
                            incoming.consent_flags
                            if incoming.consent_flags is not None
                            else {}
                        ),
                        opt_out_flags=(
                            incoming.opt_out_flags
                            if incoming.opt_out_flags is not None
                            else {}
                        ),
                        contact_prefs=incoming.contact_prefs,
                    )
                )
                patient_id_by_ref[incoming.source_ref] = patient_id
                result.patients_inserted += 1
        session.flush()

        for incoming_appt in incoming_appointments:
            appt_patient_id = patient_id_by_ref.get(incoming_appt.patient_source_ref)
            if appt_patient_id is None:
                found_patient = find_source_patient(
                    session,
                    clinic_id,
                    incoming_appt.patient_source_ref,
                    source_system,
                )
                if found_patient is None:
                    raise SyncIntegrityError(
                        f"appointment {incoming_appt.source_ref!r} references unknown "
                        f"patient {incoming_appt.patient_source_ref!r}"
                    )
                appt_patient_id = found_patient.id

            existing_appt = session.execute(
                select(Appointment).where(
                    Appointment.clinic_id == clinic_id,
                    Appointment.source_ref == incoming_appt.source_ref,
                )
            ).scalar_one_or_none()
            if existing_appt is not None:
                existing_appt.patient_id = appt_patient_id
                existing_appt.status = incoming_appt.status
                existing_appt.start_at = incoming_appt.start_at
                existing_appt.value = incoming_appt.value
                result.appointments_updated += 1
            else:
                session.add(
                    Appointment(
                        id=make_id("appt", clinic_id, incoming_appt.source_ref),
                        clinic_id=clinic_id,
                        patient_id=appt_patient_id,
                        source_ref=incoming_appt.source_ref,
                        status=incoming_appt.status,
                        start_at=incoming_appt.start_at,
                        value=incoming_appt.value,
                    )
                )
                result.appointments_inserted += 1

        session.add(
            AuditLog(
                id=f"audit-{uuid.uuid4().hex}",
                clinic_id=clinic_id,
                actor=f"sync:{source.name}",
                action=AuditAction.SYNC_UPSERT,
                entity_ref=clinic_id,
                payload_hash=_hash_payload(asdict(result)),
            )
        )
        session.flush()
    return result
