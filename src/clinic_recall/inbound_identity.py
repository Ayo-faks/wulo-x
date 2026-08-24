"""Best-effort caller matching for inbound calls without PHI readback."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .inbound_transport import hash_phone_number_for_clinic
from .models import Patient
from .rights import SubjectFrozenError, assert_patient_writable


@dataclass(frozen=True)
class InboundIdentityMatch:
    """Patient-minimized caller match summary for model-facing tools."""

    status: str
    match_count: int
    patient_id: str | None = None


def find_possible_patient_match(
    session: Session, clinic_id: str, caller_number_hash: str | None
) -> InboundIdentityMatch:
    """Match caller hash to patient phones without exposing patient identifiers."""
    match = resolve_single_inbound_patient_id(session, clinic_id, caller_number_hash)
    if match.status != "single_match":
        return InboundIdentityMatch(status=match.status, match_count=match.match_count)
    return InboundIdentityMatch(status=match.status, match_count=match.match_count, patient_id=match.patient_id)


def resolve_single_inbound_patient_id(
    session: Session, clinic_id: str, caller_number_hash: str | None
) -> InboundIdentityMatch:
    """Resolve a single trusted patient id from a caller hash for server-side use."""
    if not caller_number_hash:
        return InboundIdentityMatch(status="no_caller_number", match_count=0)
    patient_ids: list[str] = []
    with clinic_scope(session, clinic_id):
        patients = session.execute(tenant_select(Patient)).scalars()
        for patient in patients:
            if hash_phone_number_for_clinic(patient.phone, clinic_id) == caller_number_hash:
                try:
                    assert_patient_writable(session, clinic_id, patient.id)
                except SubjectFrozenError:
                    continue
                patient_ids.append(patient.id)
    if not patient_ids:
        return InboundIdentityMatch(status="no_match", match_count=0)
    if len(patient_ids) == 1:
        return InboundIdentityMatch(status="single_match", match_count=1, patient_id=patient_ids[0])
    return InboundIdentityMatch(status="multiple_matches", match_count=len(patient_ids))