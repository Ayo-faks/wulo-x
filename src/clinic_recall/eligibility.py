"""Deterministic eligibility filtering (FR-06).

A pure function that decides whether a detected candidate may be contacted on a
given channel, applying the safety/consent gates the PRD (FR-06, FR-09) and
``SECURITY.md`` mandate. There is no model judgement: each rule is explicit and
fail-closed, and the first failing rule yields a deterministic skip reason for
the audit trail.

Rule order (safety- and legality-first):

1. Permanent opt-out for the channel        -> ``OPTED_OUT``
2. Missing per-channel consent (fail closed) -> ``NO_CONSENT``
3. Not contactable (no valid phone/email)    -> ``NOT_CONTACTABLE``
4. Per-patient frequency cap exceeded        -> ``FREQUENCY_CAP``
5. Per-clinic daily volume cap reached        -> ``DAILY_CAP``
6. Inside patient quiet hours                 -> ``QUIET_HOURS``
7. Outside clinic contact window              -> ``OUTSIDE_CONTACT_HOURS``
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .enums import Channel, SkipReason
from .types import ClinicConfig, ContactHistory, PatientView
from .validation import is_valid_e164, is_valid_email


@dataclass(frozen=True)
class EligibilityResult:
    """Outcome of evaluating a candidate against the eligibility rules."""

    eligible: bool
    channel: Channel
    skip_reason: SkipReason | None = None


def _is_contactable(patient: PatientView, channel: Channel) -> bool:
    """Whether the patient has a valid address for the given channel."""
    if channel in (Channel.SMS, Channel.CALL):
        return is_valid_e164(patient.phone)
    if channel == Channel.EMAIL:
        return is_valid_email(patient.email)
    return False


def _within_window(moment: time, start: time, end: time) -> bool:
    """Whether ``moment`` falls in ``[start, end)``, handling windows that wrap
    past midnight (e.g. a 21:00-07:00 quiet period)."""
    if start <= end:
        return start <= moment < end
    # Wrapping window: inside if at/after start OR before end.
    return moment >= start or moment < end


def evaluate(
    patient: PatientView,
    config: ClinicConfig,
    history: ContactHistory,
    now: datetime,
    channel: Channel = Channel.SMS,
) -> EligibilityResult:
    """Decide whether ``patient`` may be contacted now on ``channel``.

    Args:
        patient: Patient facts (consent, opt-out, contact details, quiet hours).
        config: Owning clinic's caps and contact window.
        history: Rolling per-patient and per-clinic contact counters.
        now: Timezone-aware "current" instant (UTC).
        channel: The intended first-contact channel (defaults to SMS).

    Returns:
        An :class:`EligibilityResult`. ``eligible`` is ``True`` only when every
        rule passes; otherwise ``skip_reason`` names the first failing rule.

    Raises:
        ValueError: If ``now`` is naive (timezone-unaware).
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware (got naive datetime)")

    key = channel.value

    # 1. Permanent opt-out (highest precedence, irreversible).
    if patient.opt_out_flags.get(key, False):
        return EligibilityResult(False, channel, SkipReason.OPTED_OUT)

    # 2. Consent must be explicitly present (fail closed when missing).
    if patient.consent_flags.get(key, False) is not True:
        return EligibilityResult(False, channel, SkipReason.NO_CONSENT)

    # 3. Contactability.
    if not _is_contactable(patient, channel):
        return EligibilityResult(False, channel, SkipReason.NOT_CONTACTABLE)

    # 4. Per-patient frequency cap.
    if history.patient_contacts_last_7d >= config.per_patient_weekly_cap:
        return EligibilityResult(False, channel, SkipReason.FREQUENCY_CAP)

    # 5. Per-clinic daily volume cap.
    if history.clinic_contacts_today >= config.daily_cap:
        return EligibilityResult(False, channel, SkipReason.DAILY_CAP)

    # 6/7. Timing: evaluate in clinic-local time.
    local_now = now.astimezone(ZoneInfo(config.timezone)).timetz().replace(tzinfo=None)

    if patient.quiet_hours is not None:
        q_start, q_end = patient.quiet_hours
        if _within_window(local_now, q_start, q_end):
            return EligibilityResult(False, channel, SkipReason.QUIET_HOURS)

    c_start, c_end = config.contact_window
    if not _within_window(local_now, c_start, c_end):
        return EligibilityResult(False, channel, SkipReason.OUTSIDE_CONTACT_HOURS)

    return EligibilityResult(True, channel, None)
