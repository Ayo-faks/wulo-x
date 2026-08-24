"""Transient provider comparison and raw-factor privacy contracts for PR-11."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from src.clinic_recall.config import ClinikoConfig
from src.clinic_recall.enums import Channel
from src.clinic_recall.identity_evidence import (
    IdentityAction,
    IdentityEvidenceService,
    IdentityFactorRule,
    IdentityPolicy,
    IdentityPolicyMode,
    IdentityProviderField,
    IdentityTier,
)
from src.clinic_recall.identity_verification import (
    ClinikoIdentityProvider,
    IdentityProvider,
    IdentityVerificationCoordinator,
    TransientIdentityValues,
)
from src.clinic_recall.models import (
    BookingAction,
    Clinic,
    ExternalEffect,
    IdentityEvidence,
    IdentityFactorAttempt,
    Patient,
)
from src.clinic_recall.sync.cliniko_client import ClinikoClient, ClinikoContractError

NOW = datetime(2026, 7, 26, 11, 0, tzinfo=UTC)
RAW_NAME_CANARY = "Fictional Avery Privacycanary"


def _canonical_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _canonical_date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _policy() -> IdentityPolicy:
    return IdentityPolicy(
        version="synthetic-verification-v1",
        mode=IdentityPolicyMode.SYNTHETIC_TEST,
        approval_reference="test-only-provider-comparison",
        factors=(
            IdentityFactorRule(
                factor_type="full_name",
                independence_group="name",
                provider_field=IdentityProviderField.FULL_NAME,
                canonicalizer=_canonical_name,
                prompt="Provide the fictional full name.",
            ),
            IdentityFactorRule(
                factor_type="date_of_birth",
                independence_group="dob",
                provider_field=IdentityProviderField.DATE_OF_BIRTH,
                canonicalizer=_canonical_date,
                prompt="Provide the fictional date of birth.",
            ),
        ),
        expires_after=timedelta(minutes=4),
        max_attempts=3,
        t1_match_count=1,
        t2_match_count=2,
        date_of_birth_factor_type="date_of_birth",
        mismatch_text="The fictional details did not match.",
        uncertain_text="The fictional details were uncertain.",
        handoff_text="The fictional clinic team will follow up.",
    )


class FakeIdentityProvider(IdentityProvider):
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[IdentityProviderField, ...]]] = []
        self.values: TransientIdentityValues | None = None

    def fetch_identity_values(
        self,
        *,
        patient_source_ref: str,
        fields: tuple[IdentityProviderField, ...],
    ) -> TransientIdentityValues:
        self.calls.append((patient_source_ref, fields))
        self.values = TransientIdentityValues(
            {
                IdentityProviderField.FULL_NAME: RAW_NAME_CANARY,
                IdentityProviderField.DATE_OF_BIRTH: "1991-04-03",
            }
        )
        return self.values


class ReentrantIdentityProvider(FakeIdentityProvider):
    def __init__(self, replay) -> None:
        super().__init__()
        self._replay = replay

    def fetch_identity_values(
        self,
        *,
        patient_source_ref: str,
        fields: tuple[IdentityProviderField, ...],
    ) -> TransientIdentityValues:
        self._replay()
        return super().fetch_identity_values(
            patient_source_ref=patient_source_ref,
            fields=fields,
        )


def test_full_name_only_reaches_t1_without_disclosure_effect_or_raw_retention(
    sqlite_session,
    caplog,
) -> None:
    sqlite_session.add(Clinic(id="clinic-pr11-verify", name="Fictional Verify Clinic"))
    sqlite_session.add(
        Patient(
            id="patient-pr11-verify",
            clinic_id="clinic-pr11-verify",
            source_ref="910000111",
            name="Fictional Local Candidate",
            phone="+447700900711",
            consent_flags={"sms": True, "call": True},
            opt_out_flags={},
        )
    )
    sqlite_session.commit()
    identifiers = iter(("evidence-verify-1", "attempt-verify-1"))
    challenges = iter(("challenge-verify-1", "challenge-verify-2"))
    service = IdentityEvidenceService(
        policy=_policy(),
        clock=lambda: NOW,
        identifier_factory=lambda: next(identifiers),
        challenge_factory=lambda: next(challenges),
    )
    started = service.begin(
        sqlite_session,
        clinic_id="clinic-pr11-verify",
        session_id="session-pr11-verify",
        route_id="route-pr11-verify",
        channel=Channel.SMS,
        patient_id="patient-pr11-verify",
        route_possession=True,
    )
    sqlite_session.commit()
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    provider = FakeIdentityProvider()
    coordinator = IdentityVerificationCoordinator(
        service=service,
        policy=_policy(),
        session_factory=factory,
        provider=provider,
    )

    decision = coordinator.verify_factor(
        clinic_id="clinic-pr11-verify",
        evidence_id=started.evidence_id,
        session_id="session-pr11-verify",
        route_id="route-pr11-verify",
        channel=Channel.SMS,
        patient_id="patient-pr11-verify",
        challenge_token=started.challenge_token,
        factor_type="full_name",
        answer=RAW_NAME_CANARY,
        uncertain=False,
    )

    assert decision.tier == IdentityTier.T1
    assert provider.calls == [
        ("910000111", (IdentityProviderField.FULL_NAME,))
    ]
    assert provider.values is not None and provider.values.cleared is True
    with factory() as session:
        assert service.authorize(
            session,
            clinic_id="clinic-pr11-verify",
            evidence_id=started.evidence_id,
            session_id="session-pr11-verify",
            route_id="route-pr11-verify",
            channel=Channel.SMS,
            patient_id="patient-pr11-verify",
            action=IdentityAction.PATIENT_APPOINTMENT_READ,
        ).allowed is False
        assert service.authorize(
            session,
            clinic_id="clinic-pr11-verify",
            evidence_id=started.evidence_id,
            session_id="session-pr11-verify",
            route_id="route-pr11-verify",
            channel=Channel.SMS,
            patient_id="patient-pr11-verify",
            action=IdentityAction.BOOK_SLOT,
        ).allowed is False
        assert session.scalar(select(func.count()).select_from(BookingAction)) == 0
        assert session.scalar(select(func.count()).select_from(ExternalEffect)) == 0
        persisted = [
            row.__dict__
            for row in session.execute(select(IdentityEvidence)).scalars()
        ] + [
            row.__dict__
            for row in session.execute(select(IdentityFactorAttempt)).scalars()
        ]
    assert RAW_NAME_CANARY not in repr(persisted)
    assert RAW_NAME_CANARY not in caplog.text
    assert RAW_NAME_CANARY not in repr(decision)


def test_typed_cliniko_provider_reads_full_name_once_and_rejects_untyped_dob() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/patients/910000111"
        return httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "application/json"},
            json={
                "id": "910000111",
                "first_name": "Fictional Avery",
                "preferred_first_name": None,
                "last_name": "Privacycanary",
                "email": None,
                "patient_phone_numbers": None,
                "archived_at": None,
                "updated_at": "2026-07-26T10:00:00Z",
            },
        )

    config = ClinikoConfig(
        enabled=True,
        api_key="fixture-uk2",
        shard="uk2",
        user_agent="Wulo Synthetic Tests (engineering@example.test)",
        timeout_seconds=2.0,
        per_page=100,
        max_pages=2,
        max_items=10,
    )
    client = ClinikoClient(
        config,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider = ClinikoIdentityProvider(client)

    values = provider.fetch_identity_values(
        patient_source_ref="910000111",
        fields=(IdentityProviderField.FULL_NAME,),
    )

    assert values.get(IdentityProviderField.FULL_NAME) == RAW_NAME_CANARY
    assert RAW_NAME_CANARY not in repr(values)
    values.clear()
    assert values.cleared is True
    assert values.get(IdentityProviderField.FULL_NAME) is None
    assert len(requests) == 1

    with pytest.raises(ClinikoContractError, match="^identity_field_unavailable$"):
        provider.fetch_identity_values(
            patient_source_ref="910000111",
            fields=(IdentityProviderField.DATE_OF_BIRTH,),
        )
    assert len(requests) == 1