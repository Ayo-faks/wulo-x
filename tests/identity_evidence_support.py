"""Explicit synthetic PR-11 policy helpers for local tests only."""

from __future__ import annotations

from datetime import date, timedelta

from src.clinic_recall.enums import Channel
from src.clinic_recall.identity_evidence import (
    IdentityAuthorizationContext,
    IdentityEvidenceService,
    IdentityFactorRule,
    IdentityPolicy,
    IdentityPolicyMode,
    IdentityProviderField,
)


def synthetic_identity_policy() -> IdentityPolicy:
    return IdentityPolicy(
        version="synthetic-test-policy-v1",
        mode=IdentityPolicyMode.SYNTHETIC_TEST,
        approval_reference="tests-only-explicit-policy",
        factors=(
            IdentityFactorRule(
                factor_type="full_name",
                independence_group="name",
                provider_field=IdentityProviderField.FULL_NAME,
                canonicalizer=lambda value: " ".join(value.split()).casefold(),
                prompt="Provide the fictional full name.",
            ),
            IdentityFactorRule(
                factor_type="date_of_birth",
                independence_group="dob",
                provider_field=IdentityProviderField.DATE_OF_BIRTH,
                canonicalizer=lambda value: date.fromisoformat(value).isoformat(),
                prompt="Provide the fictional date of birth.",
            ),
        ),
        expires_after=timedelta(minutes=5),
        max_attempts=3,
        t1_match_count=1,
        t2_match_count=2,
        date_of_birth_factor_type="date_of_birth",
        mismatch_text="The fictional details did not match.",
        uncertain_text="The fictional details were uncertain.",
        handoff_text="The fictional clinic team will follow up.",
    )


def grant_synthetic_t2(
    session,
    *,
    clinic_id: str,
    patient_id: str,
    channel: Channel,
    now,
    suffix: str,
) -> tuple[IdentityEvidenceService, IdentityAuthorizationContext]:
    identifiers = iter(
        (
            f"identity-evidence-{suffix}",
            f"identity-attempt-{suffix}-1",
            f"identity-attempt-{suffix}-2",
        )
    )
    challenges = iter(
        (
            f"identity-challenge-{suffix}-1",
            f"identity-challenge-{suffix}-2",
        )
    )
    service = IdentityEvidenceService(
        policy=synthetic_identity_policy(),
        clock=lambda: now,
        identifier_factory=lambda: next(identifiers),
        challenge_factory=lambda: next(challenges),
    )
    session_id = f"identity-session-{suffix}"
    route_id = f"identity-route-{suffix}"
    started = service.begin(
        session,
        clinic_id=clinic_id,
        session_id=session_id,
        route_id=route_id,
        channel=channel,
        patient_id=patient_id,
        route_possession=True,
    )
    first = service.record_factor_result(
        session,
        clinic_id=clinic_id,
        evidence_id=started.evidence_id,
        session_id=session_id,
        route_id=route_id,
        channel=channel,
        patient_id=patient_id,
        challenge_token=started.challenge_token,
        factor_type="full_name",
        matched=True,
        uncertain=False,
    )
    service.record_factor_result(
        session,
        clinic_id=clinic_id,
        evidence_id=started.evidence_id,
        session_id=session_id,
        route_id=route_id,
        channel=channel,
        patient_id=patient_id,
        challenge_token=first.challenge_token,
        factor_type="date_of_birth",
        matched=True,
        uncertain=False,
    )
    return service, IdentityAuthorizationContext(
        evidence_id=str(started.evidence_id),
        session_id=session_id,
        route_id=route_id,
        channel=channel,
    )