"""Validated, untrusted-input schema for CSV ingestion.

Per ``SECURITY.md`` every CSV row is untrusted. This Pydantic model is the
validation boundary: it strips whitespace, enforces a tz-aware ``start_at``,
coerces phone/email to ``None`` when malformed (so a bad value can never be
mistaken for a contactable address), parses booleans safely, and neutralises
spreadsheet formula injection in free-text fields. Unknown columns are ignored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from .enums import AppointmentStatus
from .validation import is_valid_e164, is_valid_email, sanitize_csv_text

_BOOL_TRUE = {"1", "true", "t", "yes", "y", "on"}
_BOOL_FALSE = {"0", "false", "f", "no", "n", "off", ""}

_BOOL_FIELDS = (
    "consent_sms",
    "consent_email",
    "consent_call",
    "opt_out_sms",
    "opt_out_email",
    "opt_out_call",
)

# The required header columns a CSV must provide.
REQUIRED_COLUMNS = (
    "appointment_source_ref",
    "patient_source_ref",
    "patient_name",
    "status",
    "start_at",
)


class CsvAppointmentRow(BaseModel):
    """One validated appointment row (with inline patient details)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    appointment_source_ref: str
    patient_source_ref: str
    patient_name: str
    status: AppointmentStatus
    start_at: datetime
    patient_phone: str | None = None
    patient_email: str | None = None
    value: Decimal | None = None
    consent_sms: bool = False
    consent_email: bool = False
    consent_call: bool = False
    opt_out_sms: bool = False
    opt_out_email: bool = False
    opt_out_call: bool = False

    @field_validator("appointment_source_ref", "patient_source_ref", "patient_name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @field_validator("patient_name")
    @classmethod
    def _defang_name(cls, value: str) -> str:
        # Neutralise CSV/formula injection in the free-text name.
        return sanitize_csv_text(value) or value

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("start_at")
    @classmethod
    def _require_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("start_at must include a timezone offset (e.g. ...Z or +00:00)")
        return value.astimezone(UTC)

    @field_validator("patient_phone")
    @classmethod
    def _coerce_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        return candidate if is_valid_e164(candidate) else None

    @field_validator("patient_email")
    @classmethod
    def _coerce_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        return candidate if is_valid_email(candidate) else None

    @field_validator("value", mode="before")
    @classmethod
    def _empty_value_is_none(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and value.strip() == ""):
            return None
        return value

    @field_validator(*_BOOL_FIELDS, mode="before")
    @classmethod
    def _parse_bool(cls, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        text = str(value).strip().lower()
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
        raise ValueError(f"invalid boolean value: {value!r}")

    @property
    def consent_flags(self) -> dict[str, bool]:
        """Per-channel consent as a flag dict."""
        return {"sms": self.consent_sms, "email": self.consent_email, "call": self.consent_call}

    @property
    def opt_out_flags(self) -> dict[str, bool]:
        """Per-channel opt-out as a flag dict."""
        return {"sms": self.opt_out_sms, "email": self.opt_out_email, "call": self.opt_out_call}
