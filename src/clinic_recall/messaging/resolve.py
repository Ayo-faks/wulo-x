"""Inbound message routing helpers.

Inbound SMS callbacks arrive before a clinic scope can be established because
the receiving number determines the tenant. This module intentionally returns
only the owning clinic id; all patient/job access must happen after callers
enter ``clinic_scope(session, clinic_id)``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ClinicPhoneProvider, ClinicPhonePurpose, ClinicPhoneStatus
from ..inbound_transport import InboundRouteError, normalize_phone_number
from ..models import Clinic, ClinicPhoneNumber


@dataclass(frozen=True)
class InboundSmsRoute:
    """Trusted SMS route resolved from the provider-owned receiving number."""

    clinic_id: str
    provider: ClinicPhoneProvider | None
    normalized_to_number: str
    clinic_phone_number_id: str | None
    source: str


def resolve_inbound_sms_route(
    session: Session,
    *,
    provider: ClinicPhoneProvider | str | None,
    inbound_number: str | None,
) -> InboundSmsRoute | None:
    """Return the trusted clinic route for an inbound SMS ``To`` number."""
    if inbound_number is None:
        return None
    raw_number = inbound_number.strip()
    if not raw_number:
        return None

    providers = _candidate_providers(provider)
    try:
        normalized_number = normalize_phone_number(raw_number)
    except InboundRouteError:
        normalized_number = None

    if normalized_number:
        route_query = select(ClinicPhoneNumber).where(
            ClinicPhoneNumber.phone_number == normalized_number,
            ClinicPhoneNumber.purpose.in_([ClinicPhonePurpose.INBOUND, ClinicPhonePurpose.BOTH]),
            ClinicPhoneNumber.status == ClinicPhoneStatus.ACTIVE,
        )
        if providers is not None:
            route_query = route_query.where(ClinicPhoneNumber.provider.in_(providers))
        routes = session.execute(route_query).scalars().all()
        if len(routes) == 1:
            route = routes[0]
            return InboundSmsRoute(
                clinic_id=route.clinic_id,
                provider=route.provider,
                normalized_to_number=normalized_number,
                clinic_phone_number_id=route.id,
                source="clinic_phone_number",
            )
        if len(routes) > 1:
            return None

    clinic_id = session.execute(
        select(Clinic.id).where(Clinic.sms_number == raw_number)
    ).scalar_one_or_none()
    if clinic_id is None:
        return None
    return InboundSmsRoute(
        clinic_id=clinic_id,
        provider=providers[0] if providers and len(providers) == 1 else None,
        normalized_to_number=normalized_number or raw_number,
        clinic_phone_number_id=None,
        source="clinic_sms_number",
    )


def resolve_clinic_by_inbound_number(session: Session, inbound_number: str | None) -> str | None:
    """Return the clinic id for an inbound ``To`` number, or ``None``.

    The match is exact after trimming whitespace. Phone-number normalisation is
    intentionally kept outside this lookup so provider webhook adapters pass the
    canonical E.164 value they received from ACS/Twilio.
    """
    route = resolve_inbound_sms_route(session, provider=None, inbound_number=inbound_number)
    return route.clinic_id if route else None


def _candidate_providers(
    provider: ClinicPhoneProvider | str | None,
) -> list[ClinicPhoneProvider] | None:
    if provider is None:
        return [ClinicPhoneProvider.TWILIO, ClinicPhoneProvider.ACS]
    if isinstance(provider, ClinicPhoneProvider):
        return [provider]
    return [ClinicPhoneProvider(str(provider).strip().lower())]