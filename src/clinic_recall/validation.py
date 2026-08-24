"""Shared input validators (security-sensitive).

These helpers are used by both the eligibility gate (contactability) and the
CSV ingestion schema. Per ``SECURITY.md``, external input is untrusted: phone
and email are validated against strict patterns, and free-text fields are
guarded against spreadsheet formula injection before they are ever written or
re-exported.
"""

from __future__ import annotations

import re

# E.164: a leading '+', a non-zero country digit, then up to 14 more digits.
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

# Pragmatic email check (not full RFC 5322): one '@', a dot in the domain, no
# whitespace. Stricter validation is the email provider's job at send time.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Leading characters that spreadsheet software interprets as a formula. A value
# beginning with any of these is treated as potentially malicious CSV input.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def is_valid_e164(phone: str | None) -> bool:
    """Return ``True`` if ``phone`` is a syntactically valid E.164 number."""
    if not phone:
        return False
    return _E164_RE.match(phone) is not None


def is_valid_email(email: str | None) -> bool:
    """Return ``True`` if ``email`` looks like a deliverable address."""
    if not email:
        return False
    return _EMAIL_RE.match(email) is not None


def has_formula_injection(value: str | None) -> bool:
    """Return ``True`` if ``value`` starts with a spreadsheet-formula trigger."""
    if not value:
        return False
    return value.startswith(_FORMULA_PREFIXES)


def sanitize_csv_text(value: str | None) -> str | None:
    """Neutralise CSV/formula-injection by prefixing a single quote.

    Returns the value unchanged when it is empty or safe. When it begins with a
    formula trigger, a leading apostrophe is prepended so spreadsheet software
    treats it as literal text rather than a formula.
    """
    if value is None:
        return None
    if has_formula_injection(value):
        return "'" + value
    return value
