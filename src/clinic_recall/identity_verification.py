"""Transient provider-backed identity factor comparison."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from .db import clinic_scope, tenant_select
from .enums import Channel
from .identity_evidence import (
    IdentityDecision,
    IdentityEvidenceService,
    IdentityPolicy,
    IdentityProviderField,
)
from .models import Patient
from .rights import assert_patient_writable
from .sync.cliniko_adapter import ClinikoPatientRecord
from .sync.cliniko_client import ClinikoClient, ClinikoContractError


class TransientIdentityValues:
    """Short-lived provider values that render only as a redacted marker."""

    __slots__ = ("_values", "cleared")

    def __init__(self, values: Mapping[IdentityProviderField, str]) -> None:
        self._values = dict(values)
        self.cleared = False

    def get(self, field: IdentityProviderField) -> str | None:
        if self.cleared:
            return None
        return self._values.get(field)

    def clear(self) -> None:
        self._values.clear()
        self.cleared = True

    def __repr__(self) -> str:
        return "TransientIdentityValues(<redacted>)"


class IdentityProvider(Protocol):
    """Fetch only policy-approved fields for one server-resolved candidate."""

    def fetch_identity_values(
        self,
        *,
        patient_source_ref: str,
        fields: tuple[IdentityProviderField, ...],
    ) -> TransientIdentityValues: ...


class ClinikoIdentityProvider:
    """Read established typed patient fields from one exact Cliniko patient."""

    def __init__(self, client: ClinikoClient) -> None:
        self._client = client

    def fetch_identity_values(
        self,
        *,
        patient_source_ref: str,
        fields: tuple[IdentityProviderField, ...],
    ) -> TransientIdentityValues:
        requested = tuple(dict.fromkeys(fields))
        if not requested or len(requested) != len(fields):
            raise ClinikoContractError("identity_fields")
        if any(field != IdentityProviderField.FULL_NAME for field in requested):
            raise ClinikoContractError("identity_field_unavailable")
        payload = self._client.get_item(
            "patients",
            patient_source_ref,
            operation_code="identity_verification_read",
        )
        patient = ClinikoPatientRecord.from_payload(payload)
        return TransientIdentityValues(
            {IdentityProviderField.FULL_NAME: patient.normalize().name}
        )


class IdentityVerificationCoordinator:
    """Compare one factor outside DB scope, then persist minimized evidence."""

    def __init__(
        self,
        *,
        service: IdentityEvidenceService,
        policy: IdentityPolicy,
        session_factory: sessionmaker[Session],
        provider: IdentityProvider,
    ) -> None:
        self._service = service
        self._policy = policy
        self._session_factory = session_factory
        self._provider = provider

    def verify_factor(
        self,
        *,
        clinic_id: str,
        evidence_id: str | None,
        session_id: str,
        route_id: str,
        channel: Channel,
        patient_id: str,
        challenge_token: str | None,
        factor_type: str,
        answer: str,
        uncertain: bool,
    ) -> IdentityDecision:
        """Perform exactly one approved comparison and retain no raw values."""
        factor = self._policy.factor(factor_type)
        if factor is None:
            with self._session_factory() as session, session.begin():
                return self._service.preflight_factor_attempt(
                    session,
                    clinic_id=clinic_id,
                    evidence_id=evidence_id,
                    session_id=session_id,
                    route_id=route_id,
                    channel=channel,
                    patient_id=patient_id,
                    challenge_token=challenge_token,
                    factor_type=factor_type,
                )

        with self._session_factory() as session, session.begin():
            preflight = self._service.preflight_factor_attempt(
                session,
                clinic_id=clinic_id,
                evidence_id=evidence_id,
                session_id=session_id,
                route_id=route_id,
                channel=channel,
                patient_id=patient_id,
                challenge_token=challenge_token,
                factor_type=factor_type,
            )
            if not preflight.allowed:
                return preflight
            with clinic_scope(session, clinic_id):
                patient = session.execute(
                    tenant_select(Patient).where(Patient.id == patient_id)
                ).scalar_one_or_none()
                if patient is None:
                    raise ValueError("identity_candidate_missing")
                assert_patient_writable(session, clinic_id, patient_id)
                patient_source_ref = patient.source_ref

        provider_values: TransientIdentityValues | None = None
        matched = False
        comparison_uncertain = uncertain
        raw_answer: str | None = answer
        raw_provider_value: str | None = None
        try:
            if not comparison_uncertain:
                provider_values = self._provider.fetch_identity_values(
                    patient_source_ref=patient_source_ref,
                    fields=(factor.provider_field,),
                )
                raw_provider_value = provider_values.get(factor.provider_field)
                if raw_provider_value is None:
                    comparison_uncertain = True
                else:
                    try:
                        matched = factor.canonicalizer(raw_answer) == factor.canonicalizer(
                            raw_provider_value
                        )
                    except (TypeError, ValueError):
                        comparison_uncertain = True
                        matched = False
        except Exception:  # Provider errors are sanitized into uncertain evidence.
            comparison_uncertain = True
            matched = False
        finally:
            raw_answer = None
            raw_provider_value = None
            patient_source_ref = ""
            if provider_values is not None:
                provider_values.clear()

        with self._session_factory() as session, session.begin():
            return self._service.record_factor_result(
                session,
                clinic_id=clinic_id,
                evidence_id=evidence_id,
                session_id=session_id,
                route_id=route_id,
                channel=channel,
                patient_id=patient_id,
                challenge_token=challenge_token,
                factor_type=factor_type,
                matched=matched,
                uncertain=comparison_uncertain,
            )


__all__ = [
    "ClinikoIdentityProvider",
    "IdentityProvider",
    "IdentityVerificationCoordinator",
    "TransientIdentityValues",
]