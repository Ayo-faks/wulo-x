"""Rule-by-rule tests for deterministic eligibility filtering (FR-06)."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest
from src.clinic_recall.eligibility import EligibilityResult, _within_window, evaluate
from src.clinic_recall.enums import Channel, SkipReason
from src.clinic_recall.types import ClinicConfig, ContactHistory, PatientView

# 12:00 UTC == 13:00 Europe/London (BST) -> inside the 08:00-20:00 window.
NOW = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
# 23:00 UTC == 00:00 Europe/London -> outside the window.
NOW_OUTSIDE = datetime(2026, 6, 26, 23, 0, tzinfo=UTC)
CFG = ClinicConfig(clinic_id="clinic-test", timezone="Europe/London")


def _patient(**overrides) -> PatientView:
    base = dict(
        patient_id="patient-1",
        clinic_id="clinic-test",
        phone="+447700900001",
        email="patient@example.com",
        consent_flags={"sms": True, "email": True, "call": True},
        opt_out_flags={},
        quiet_hours=None,
    )
    base.update(overrides)
    return PatientView(**base)


def test_fully_eligible_patient_passes():
    result = evaluate(_patient(), CFG, ContactHistory(), NOW)
    assert result == EligibilityResult(True, Channel.SMS, None)


def test_opt_out_takes_precedence_over_everything():
    # Opted out AND no consent AND outside hours: opt-out still wins.
    patient = _patient(opt_out_flags={"sms": True}, consent_flags={})
    result = evaluate(patient, CFG, ContactHistory(), NOW_OUTSIDE)
    assert result.skip_reason == SkipReason.OPTED_OUT
    assert result.eligible is False


def test_missing_consent_is_fail_closed():
    assert evaluate(_patient(consent_flags={}), CFG, ContactHistory(), NOW).skip_reason == (
        SkipReason.NO_CONSENT
    )


def test_explicit_false_consent_blocks():
    patient = _patient(consent_flags={"sms": False})
    assert evaluate(patient, CFG, ContactHistory(), NOW).skip_reason == SkipReason.NO_CONSENT


@pytest.mark.parametrize("phone", [None, "", "12345", "447700900001", "+44 7700 900001"])
def test_invalid_phone_is_not_contactable_for_sms(phone):
    patient = _patient(phone=phone)
    result = evaluate(patient, CFG, ContactHistory(), NOW, channel=Channel.SMS)
    assert result.skip_reason == SkipReason.NOT_CONTACTABLE


def test_call_channel_requires_valid_phone():
    patient = _patient(phone=None)
    result = evaluate(patient, CFG, ContactHistory(), NOW, channel=Channel.CALL)
    assert result.skip_reason == SkipReason.NOT_CONTACTABLE


def test_email_channel_requires_valid_email():
    patient = _patient(email=None)
    result = evaluate(patient, CFG, ContactHistory(), NOW, channel=Channel.EMAIL)
    assert result.skip_reason == SkipReason.NOT_CONTACTABLE
    # A valid email passes on the email channel.
    assert evaluate(_patient(), CFG, ContactHistory(), NOW, channel=Channel.EMAIL).eligible


def test_frequency_cap_blocks_at_threshold():
    history = ContactHistory(patient_contacts_last_7d=CFG.per_patient_weekly_cap)
    assert evaluate(_patient(), CFG, history, NOW).skip_reason == SkipReason.FREQUENCY_CAP
    # One below the cap is still eligible.
    ok = ContactHistory(patient_contacts_last_7d=CFG.per_patient_weekly_cap - 1)
    assert evaluate(_patient(), CFG, ok, NOW).eligible


def test_daily_clinic_cap_blocks_at_threshold():
    history = ContactHistory(clinic_contacts_today=CFG.daily_cap)
    assert evaluate(_patient(), CFG, history, NOW).skip_reason == SkipReason.DAILY_CAP


def test_outside_contact_hours_blocks():
    result = evaluate(_patient(), CFG, ContactHistory(), NOW_OUTSIDE)
    assert result.skip_reason == SkipReason.OUTSIDE_CONTACT_HOURS


def test_patient_quiet_hours_block_even_inside_clinic_window():
    # 13:00 local falls inside this patient's 12:00-15:00 quiet window.
    patient = _patient(quiet_hours=(time(12, 0), time(15, 0)))
    result = evaluate(patient, CFG, ContactHistory(), NOW)
    assert result.skip_reason == SkipReason.QUIET_HOURS


def test_naive_now_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate(_patient(), CFG, ContactHistory(), datetime(2026, 6, 26, 12, 0))


@pytest.mark.parametrize(
    ("moment", "start", "end", "expected"),
    [
        (time(13, 0), time(8, 0), time(20, 0), True),
        (time(8, 0), time(8, 0), time(20, 0), True),  # inclusive start
        (time(20, 0), time(8, 0), time(20, 0), False),  # exclusive end
        (time(7, 59), time(8, 0), time(20, 0), False),
        # Wrapping window (quiet hours 21:00-07:00).
        (time(23, 0), time(21, 0), time(7, 0), True),
        (time(3, 0), time(21, 0), time(7, 0), True),
        (time(12, 0), time(21, 0), time(7, 0), False),
    ],
)
def test_within_window_handles_wrapping(moment, start, end, expected):
    assert _within_window(moment, start, end) is expected
