"""Tests for Phase 2 inbound SMS clinic resolution."""

from __future__ import annotations

from src.clinic_recall.messaging.resolve import resolve_clinic_by_inbound_number
from src.clinic_recall.models import Clinic


def test_resolve_clinic_by_inbound_number(sqlite_session):
    sqlite_session.add(
        Clinic(
            id="clinic-sms",
            name="SMS Clinic",
            sms_number="+447700900000",
            timezone="Europe/London",
            daily_caps=200,
        )
    )
    sqlite_session.flush()

    assert resolve_clinic_by_inbound_number(sqlite_session, "+447700900000") == "clinic-sms"
    assert resolve_clinic_by_inbound_number(sqlite_session, " +447700900000 ") == "clinic-sms"


def test_resolve_clinic_by_inbound_number_returns_none_for_unknown_or_empty(sqlite_session):
    sqlite_session.add(
        Clinic(
            id="clinic-sms",
            name="SMS Clinic",
            sms_number="+447700900000",
            timezone="Europe/London",
            daily_caps=200,
        )
    )
    sqlite_session.flush()

    assert resolve_clinic_by_inbound_number(sqlite_session, "+447700900999") is None
    assert resolve_clinic_by_inbound_number(sqlite_session, " ") is None
    assert resolve_clinic_by_inbound_number(sqlite_session, None) is None