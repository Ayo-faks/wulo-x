"""Seed or verify the live Clinic Recall Twilio inbound phone route.

This script intentionally prints only presence/count/status values. It never
prints DSNs, endpoints, credentials, or phone numbers.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session
from src.clinic_recall.config import get_database_url
from src.clinic_recall.db import configure_engine
from src.clinic_recall.enums import (
    ClinicPhoneProvider,
    ClinicPhonePurpose,
    ClinicPhoneStatus,
)
from src.clinic_recall.inbound_transport import InboundRouteError, normalize_phone_number
from src.clinic_recall.models import Clinic, ClinicPhoneNumber

REPO_ROOT = Path(__file__).resolve().parents[3]
PHONE_ENV_CANDIDATES = (
    "CLINIC_RECALL_TWILIO_INBOUND_NUMBER",
    "CLINIC_RECALL_TWILIO_PHONE_NUMBER",
    "CLINIC_RECALL_SMS_NUMBER",
    "TWILIO_INBOUND_PHONE_NUMBER",
    "TWILIO_PHONE_NUMBER",
    "TWILIO_FROM_PHONE_NUMBER",
    "TWILIO_FROM_NUMBER",
    "TWILIO_SMS_FROM_NUMBER",
)
CLINIC_ENV_CANDIDATES = (
    "CLINIC_RECALL_SEED_CLINIC_ID",
    "CLINIC_RECALL_STAFF_CLINIC_ID",
)


def main() -> int:
    _load_env_files()
    clinic_id = _first_env(CLINIC_ENV_CANDIDATES)
    phone_number = _first_env(PHONE_ENV_CANDIDATES)
    purpose = _purpose(os.getenv("CLINIC_RECALL_TWILIO_PHONE_PURPOSE") or "both")

    _print_presence("twilio_inbound_number", phone_number)
    _print_presence("database_url", _database_configured())
    if not phone_number:
        print("seed_status=missing_required_env")
        return 2

    try:
        normalized_number = normalize_phone_number(phone_number)
    except InboundRouteError:
        print("seed_status=invalid_phone_number")
        return 2

    engine = configure_engine(get_database_url(), pool_pre_ping=True)
    with Session(engine, expire_on_commit=False) as session:
        clinic_id = clinic_id or _infer_clinic_id(session, normalized_number)
        _print_presence("clinic_id", clinic_id)
        if not clinic_id:
            print("clinic_inference=ambiguous_or_missing")
            print("seed_status=missing_required_env")
            return 2
        clinic = session.get(Clinic, clinic_id)
        _print_presence("clinic_exists", clinic)
        if clinic is None:
            print("seed_status=missing_clinic")
            return 3

        route = session.execute(
            sa.select(ClinicPhoneNumber).where(
                ClinicPhoneNumber.provider == ClinicPhoneProvider.TWILIO,
                ClinicPhoneNumber.phone_number == normalized_number,
            )
        ).scalar_one_or_none()
        if route is not None and route.clinic_id != clinic_id:
            print("route_conflict=SET")
            print("seed_status=conflict")
            return 4
        if route is None:
            route = ClinicPhoneNumber(
                id=f"clinic-phone-{uuid.uuid4().hex}",
                clinic_id=clinic_id,
                phone_number=normalized_number,
                provider=ClinicPhoneProvider.TWILIO,
                purpose=purpose,
                status=ClinicPhoneStatus.ACTIVE,
                config={},
            )
            session.add(route)
        else:
            route.purpose = purpose
            route.status = ClinicPhoneStatus.ACTIVE

        session.flush()
        active_routes = session.scalar(
            sa.select(sa.func.count())
            .select_from(ClinicPhoneNumber)
            .where(ClinicPhoneNumber.clinic_id == clinic_id)
            .where(ClinicPhoneNumber.provider == ClinicPhoneProvider.TWILIO)
            .where(ClinicPhoneNumber.status == ClinicPhoneStatus.ACTIVE)
            .where(ClinicPhoneNumber.purpose.in_([ClinicPhonePurpose.INBOUND, ClinicPhonePurpose.BOTH]))
        ) or 0
        print(f"legacy_sms_number={_presence(clinic.sms_number)}")
        print(f"route_status={route.status.value}")
        print(f"route_purpose={route.purpose.value}")
        print(f"active_twilio_inbound_routes={int(active_routes)}")
        print(f"migration_0009={_presence(_migration_version(session) == '0009_inbound_messages')}")
        session.commit()
    print("seed_status=ok")
    return 0


def _load_env_files() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    for path in (REPO_ROOT / ".env", REPO_ROOT / ".env.local"):
        if path.exists():
            load_dotenv(path, override=False)
    azd_env = os.getenv("AZURE_ENV_NAME") or os.getenv("AZD_ENV_NAME")
    if azd_env:
        path = REPO_ROOT / ".azure" / azd_env / ".env"
        if path.exists():
            load_dotenv(path, override=False)
        return
    for path in sorted((REPO_ROOT / ".azure").glob("*/.env")):
        load_dotenv(path, override=False)


def _first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _purpose(value: str) -> ClinicPhonePurpose:
    try:
        purpose = ClinicPhonePurpose(value.strip().lower())
    except ValueError:
        purpose = ClinicPhonePurpose.INBOUND
    return purpose if purpose in {ClinicPhonePurpose.INBOUND, ClinicPhonePurpose.BOTH} else ClinicPhonePurpose.INBOUND


def _database_configured() -> bool:
    return bool(os.getenv("CLINIC_RECALL_DATABASE_URL") or os.getenv("POSTGRES_PASSWORD"))


def _infer_clinic_id(session: Session, normalized_number: str) -> str | None:
    sms_match = session.execute(
        sa.select(Clinic.id).where(Clinic.sms_number == normalized_number).limit(2)
    ).scalars().all()
    if len(sms_match) == 1:
        print("clinic_inference=legacy_sms_number")
        return sms_match[0]

    clinics = session.execute(sa.select(Clinic.id).order_by(Clinic.id).limit(2)).scalars().all()
    if len(clinics) == 1:
        print("clinic_inference=single_clinic")
        return clinics[0]
    return None


def _migration_version(session: Session) -> str | None:
    try:
        return session.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    except Exception:
        return None


def _presence(value: object) -> str:
    return "SET" if value else "missing"


def _print_presence(name: str, value: object) -> None:
    print(f"{name}={_presence(value)}")


if __name__ == "__main__":
    raise SystemExit(main())