"""Database configuration for the Clinic Recall data plane.

Single source of truth for the PostgreSQL connection URL. Mirrors the
``src/cosmosdb/config.py`` pattern: defaults live here, environment variables
(populated from the Terraform ``POSTGRES_*`` outputs / Key Vault) override them.

Resolution order for the runtime URL:

1. ``CLINIC_RECALL_DATABASE_URL`` - the general application SQLAlchemy URL or
    libpq conninfo string.
2. The discrete ``POSTGRES_HOST`` / ``POSTGRES_DATABASE_NAME`` /
   ``POSTGRES_ADMIN_LOGIN`` / ``POSTGRES_PASSWORD`` variables.

The password is never logged and never assembled into log lines by this module.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import quote_plus

# The canonical default database (matches the Phase 0 Terraform default).
DEFAULT_DATABASE_NAME = "clinic_recall_spike"
DEFAULT_ADMIN_LOGIN = "clinicrecalladmin"
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 5432

# SQLAlchemy driver: psycopg (v3).
_DRIVER = "postgresql+psycopg"

_CLINIKO_SHARDS = frozenset({"uk1", "uk2", "uk3"})
_CLINIKO_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_CLINIKO_USER_AGENT_PATTERN = re.compile(
    r"[^()\r\n]{1,128} \([^()\s@]+@[^()\s@]+\.[^()\s@]+\)"
)


class ClinikoConfigurationError(ValueError):
    """Identify one invalid Cliniko setting without retaining its value."""


@dataclass(frozen=True)
class ClinikoConfig:
    """Validated, default-off configuration for the UK Cliniko pilot."""

    enabled: bool
    api_key: str | None = field(default=None, repr=False)
    shard: str | None = None
    user_agent: str | None = field(default=None, repr=False)
    timeout_seconds: float = 10.0
    per_page: int = 100
    max_pages: int = 20
    max_items: int = 2_000

    @property
    def base_url(self) -> str | None:
        """Return the exact derived API base for an enabled configuration."""
        if self.shard is None:
            return None
        return f"https://api.{self.shard}.cliniko.com/v1"


def _env(name: str) -> str | None:
    """Return a stripped, non-empty environment value or ``None``."""
    value = os.getenv(name)
    if value:
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _cliniko_bounded_int(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ClinikoConfigurationError(name) from None
    if not minimum <= value <= maximum:
        raise ClinikoConfigurationError(name)
    return value


def _cliniko_timeout_seconds() -> float:
    name = "CLINIC_RECALL_CLINIKO_TIMEOUT_SECONDS"
    raw = _env(name)
    if raw is None:
        return 10.0
    try:
        value = float(raw)
    except ValueError:
        raise ClinikoConfigurationError(name) from None
    if not math.isfinite(value) or not 1.0 <= value <= 30.0:
        raise ClinikoConfigurationError(name)
    return value


def get_cliniko_config() -> ClinikoConfig:
    """Load the default-off Cliniko configuration for one UK shard."""
    enabled_raw = _env("CLINIC_RECALL_CLINIKO_SYNC_ENABLED") or ""
    if enabled_raw.lower() not in _CLINIKO_TRUE_VALUES:
        return ClinikoConfig(enabled=False)
    return build_cliniko_config(
        api_key=_env("CLINIC_RECALL_CLINIKO_API_KEY"),
        shard=_env("CLINIC_RECALL_CLINIKO_SHARD"),
        user_agent=_env("CLINIC_RECALL_CLINIKO_USER_AGENT"),
        timeout_seconds=_cliniko_timeout_seconds(),
        per_page=_cliniko_bounded_int(
            "CLINIC_RECALL_CLINIKO_PER_PAGE",
            default=100,
            minimum=1,
            maximum=100,
        ),
        max_pages=_cliniko_bounded_int(
            "CLINIC_RECALL_CLINIKO_MAX_PAGES",
            default=20,
            minimum=1,
            maximum=100,
        ),
        max_items=_cliniko_bounded_int(
            "CLINIC_RECALL_CLINIKO_MAX_ITEMS",
            default=2_000,
            minimum=1,
            maximum=10_000,
        ),
    )


def build_cliniko_config(
    *,
    api_key: str | None,
    shard: str | None,
    user_agent: str | None,
    timeout_seconds: float,
    per_page: int,
    max_pages: int,
    max_items: int,
) -> ClinikoConfig:
    """Validate explicit Cliniko inputs without persisting their values."""
    api_key_name = "CLINIC_RECALL_CLINIKO_API_KEY"
    if not isinstance(api_key, str) or not api_key.strip():
        raise ClinikoConfigurationError(api_key_name)
    normalized_key = api_key.strip()
    shard_name = "CLINIC_RECALL_CLINIKO_SHARD"
    if not isinstance(shard, str) or shard not in _CLINIKO_SHARDS:
        raise ClinikoConfigurationError(shard_name)
    if normalized_key.rpartition("-")[2] != shard:
        raise ClinikoConfigurationError(api_key_name)
    user_agent_name = "CLINIC_RECALL_CLINIKO_USER_AGENT"
    if (
        not isinstance(user_agent, str)
        or _CLINIKO_USER_AGENT_PATTERN.fullmatch(user_agent.strip()) is None
    ):
        raise ClinikoConfigurationError(user_agent_name)
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or not 1.0 <= timeout_seconds <= 30.0
    ):
        raise ClinikoConfigurationError("CLINIC_RECALL_CLINIKO_TIMEOUT_SECONDS")
    bounds = (
        ("CLINIC_RECALL_CLINIKO_PER_PAGE", per_page, 100),
        ("CLINIC_RECALL_CLINIKO_MAX_PAGES", max_pages, 100),
        ("CLINIC_RECALL_CLINIKO_MAX_ITEMS", max_items, 10_000),
    )
    for name, value, maximum in bounds:
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
            raise ClinikoConfigurationError(name)
    return ClinikoConfig(
        enabled=True,
        api_key=normalized_key,
        shard=shard,
        user_agent=user_agent.strip(),
        timeout_seconds=timeout_seconds,
        per_page=per_page,
        max_pages=max_pages,
        max_items=max_items,
    )


def _build_url(
    *,
    host: str,
    database: str,
    user: str,
    password: str,
    port: str,
    sslmode: str,
) -> str:
    return (
        f"{_DRIVER}://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{database}?sslmode={sslmode}"
    )


def _url_from_conninfo(conninfo: str) -> str:
    try:
        from psycopg.conninfo import conninfo_to_dict
    except Exception as exc:  # pragma: no cover - psycopg is a runtime dependency
        raise RuntimeError("psycopg is required to parse PostgreSQL conninfo") from exc

    parts = conninfo_to_dict(conninfo)
    password = parts.get("password")
    if not password:
        raise RuntimeError(
            "PostgreSQL conninfo must include password or use a SQLAlchemy URL."
        )
    return _build_url(
        host=parts.get("host") or DEFAULT_HOST,
        database=parts.get("dbname") or parts.get("database") or DEFAULT_DATABASE_NAME,
        user=parts.get("user") or DEFAULT_ADMIN_LOGIN,
        password=password,
        port=str(parts.get("port") or DEFAULT_PORT),
        sslmode=parts.get("sslmode") or "require",
    )


def get_database_url() -> str:
    """Build the runtime SQLAlchemy database URL.

    Returns:
        A SQLAlchemy URL string using the psycopg v3 driver.

    Raises:
        RuntimeError: If no password is available when assembling the URL from
            discrete parts (fail closed rather than connect anonymously).
    """
    explicit = _env("CLINIC_RECALL_DATABASE_URL")
    if explicit:
        if "://" not in explicit and "=" in explicit:
            return _url_from_conninfo(explicit)
        return explicit

    host = _env("POSTGRES_HOST") or DEFAULT_HOST
    database = _env("POSTGRES_DATABASE_NAME") or DEFAULT_DATABASE_NAME
    user = _env("POSTGRES_ADMIN_LOGIN") or DEFAULT_ADMIN_LOGIN
    password = _env("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError(
            "POSTGRES_PASSWORD (or CLINIC_RECALL_DATABASE_URL) must be set to "
            "build the database URL."
        )
    port = _env("POSTGRES_PORT") or str(DEFAULT_PORT)
    # Azure PostgreSQL Flexible Server requires TLS.
    sslmode = _env("POSTGRES_SSLMODE") or "require"
    return _build_url(
        host=host,
        database=database,
        user=user,
        password=password,
        port=port,
        sslmode=sslmode,
    )


def get_privacy_database_url() -> str:
    """Return the dedicated ordinary-role URL for rights and retention work."""
    explicit = _env("CLINIC_RECALL_PRIVACY_DATABASE_URL")
    if not explicit:
        raise RuntimeError(
            "CLINIC_RECALL_PRIVACY_DATABASE_URL must be set for privacy work"
        )
    if "://" not in explicit and "=" in explicit:
        return _url_from_conninfo(explicit)
    return explicit


def get_test_dsn() -> str | None:
    """Return the opt-in test database URL, or ``None`` if unset.

    The ``postgres``-marked tests connect here. When unset they skip, so the
    suite stays green in environments without a PostgreSQL server.
    """
    return _env("CLINIC_RECALL_TEST_DSN")


def get_rights_subject_keyring():
    """Load versioned rights-tombstone HMAC keys without exposing their values."""
    from .rights import SubjectKey, SubjectKeyring

    version = _env("CLINIC_RECALL_RIGHTS_HMAC_KEY_VERSION")
    secret = _env("CLINIC_RECALL_RIGHTS_HMAC_KEY")
    if not version or not secret:
        raise RuntimeError(
            "CLINIC_RECALL_RIGHTS_HMAC_KEY_VERSION and "
            "CLINIC_RECALL_RIGHTS_HMAC_KEY must be set"
        )

    previous_raw = _env("CLINIC_RECALL_RIGHTS_HMAC_PREVIOUS_KEYS_JSON")
    previous: list[SubjectKey] = []
    if previous_raw:
        try:
            parsed = json.loads(previous_raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "CLINIC_RECALL_RIGHTS_HMAC_PREVIOUS_KEYS_JSON must be valid JSON"
            ) from exc
        if not isinstance(parsed, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in parsed.items()
        ):
            raise RuntimeError(
                "CLINIC_RECALL_RIGHTS_HMAC_PREVIOUS_KEYS_JSON must be an object "
                "mapping key versions to secrets"
            )
        previous = [
            SubjectKey(version=key_version, secret=key_secret.encode("utf-8"))
            for key_version, key_secret in sorted(parsed.items())
        ]
    return SubjectKeyring(
        current=SubjectKey(version=version, secret=secret.encode("utf-8")),
        previous=tuple(previous),
    )


def get_rights_policy():
    """Load explicit, versioned rights authority without legal defaults."""
    from .rights import RightsPolicy

    version = _env("CLINIC_RECALL_RIGHTS_POLICY_VERSION")
    approval_hash = _env("CLINIC_RECALL_RIGHTS_APPROVAL_EVIDENCE_SHA256")
    due_seconds_raw = _env("CLINIC_RECALL_RIGHTS_REQUEST_DUE_SECONDS")
    if not version or not approval_hash or not due_seconds_raw:
        raise RuntimeError(
            "CLINIC_RECALL_RIGHTS_POLICY_VERSION, "
            "CLINIC_RECALL_RIGHTS_APPROVAL_EVIDENCE_SHA256, and "
            "CLINIC_RECALL_RIGHTS_REQUEST_DUE_SECONDS must be set"
        )
    try:
        due_seconds = int(due_seconds_raw)
        return RightsPolicy(
            version=version,
            approval_evidence_hash=approval_hash,
            request_due_after=timedelta(seconds=due_seconds),
        )
    except ValueError as exc:
        raise RuntimeError("Clinic Recall rights policy configuration is invalid") from exc


def get_retention_policy():
    """Load explicit, versioned retention authority without legal defaults."""
    from .retention import RetentionPolicy

    names = {
        "version": "CLINIC_RECALL_RETENTION_POLICY_VERSION",
        "approval_evidence_hash": (
            "CLINIC_RECALL_RETENTION_APPROVAL_EVIDENCE_SHA256"
        ),
        "approved_at": "CLINIC_RECALL_RETENTION_POLICY_APPROVED_AT",
        "effective_at": "CLINIC_RECALL_RETENTION_POLICY_EFFECTIVE_AT",
        "expires_at": "CLINIC_RECALL_RETENTION_POLICY_EXPIRES_AT",
        "retain_for_seconds": "CLINIC_RECALL_RETENTION_RETAIN_FOR_SECONDS",
        "request_due_seconds": "CLINIC_RECALL_RETENTION_REQUEST_DUE_SECONDS",
    }
    values = {field: _env(name) for field, name in names.items()}
    missing = [name for field, name in names.items() if not values[field]]
    if missing:
        raise RuntimeError(
            "Clinic Recall retention policy configuration is incomplete: "
            + ", ".join(sorted(missing))
        )
    try:
        return RetentionPolicy(
            version=str(values["version"]),
            approval_evidence_hash=str(values["approval_evidence_hash"]),
            approved_at=_parse_aware_datetime(str(values["approved_at"])),
            effective_at=_parse_aware_datetime(str(values["effective_at"])),
            expires_at=_parse_aware_datetime(str(values["expires_at"])),
            retain_for=timedelta(seconds=int(str(values["retain_for_seconds"]))),
            request_due_after=timedelta(
                seconds=int(str(values["request_due_seconds"]))
            ),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "Clinic Recall retention policy configuration is invalid"
        ) from exc


def get_rights_residual_approvals():
    """Load closed, versioned residual approvals without policy defaults."""
    from .enums import RightsResidualCategory
    from .rights import ResidualApproval

    raw = _env("CLINIC_RECALL_RIGHTS_RESIDUAL_APPROVALS_JSON")
    if not raw:
        raise RuntimeError(
            "CLINIC_RECALL_RIGHTS_RESIDUAL_APPROVALS_JSON must be set"
        )
    expected_fields = {
        "policy_version",
        "approval_evidence_sha256",
        "due_at",
        "completion_eligible",
    }
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("residual approvals must be a non-empty object")
        approvals = {}
        for category_value, value in parsed.items():
            if not isinstance(category_value, str) or not isinstance(value, dict):
                raise ValueError("residual approval entry is invalid")
            if set(value) != expected_fields:
                raise ValueError("residual approval fields are invalid")
            if not isinstance(value["completion_eligible"], bool):
                raise ValueError("completion_eligible must be a boolean")
            category = RightsResidualCategory(category_value)
            approvals[category] = ResidualApproval(
                category=category,
                policy_version=str(value["policy_version"]),
                approval_evidence_hash=str(value["approval_evidence_sha256"]),
                due_at=_parse_aware_datetime(str(value["due_at"])),
                completion_eligible=value["completion_eligible"],
            )
        return approvals
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "Clinic Recall residual approval configuration is invalid"
        ) from exc


def _parse_aware_datetime(value: str):
    from datetime import UTC, datetime

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("retention policy timestamps must be timezone-aware")
    return parsed.astimezone(UTC)


# --------------------------------------------------------------------------- #
# PR-08 controlled CSV import (default off; no invented consent authority)
# --------------------------------------------------------------------------- #

_CSV_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

# The server-owned attestation statement staff acknowledge. Versioned; the
# hash binds each approval to the exact wording in force.
CSV_ATTESTATION_STATEMENT = (
    "I confirm this file was exported from the selected clinic system at the "
    "stated time, that the clinic is the data controller for every record in "
    "it, and that any consent values it carries were collected by the clinic."
)
CSV_ATTESTATION_VERSION = "csv-attest-v1"


def csv_import_enabled() -> bool:
    """Runtime switch for the controlled CSV import feature (default off)."""
    return (os.environ.get("CLINIC_RECALL_CSV_IMPORT_ENABLED") or "").strip().lower() in (
        _CSV_TRUE_VALUES
    )


def csv_matching_enabled() -> bool:
    """Runtime switch for provider source matching/link creation (default off)."""
    return (os.environ.get("CLINIC_RECALL_CSV_MATCHING_ENABLED") or "").strip().lower() in (
        _CSV_TRUE_VALUES
    )


def get_csv_import_policy():
    """Build the versioned CSV import policy from trusted configuration.

    Without ``CLINIC_RECALL_CSV_CONSENT_MAX_AGE_DAYS`` (a controller-approved
    evidence-age policy) ``max_evidence_age`` stays ``None`` and no positive
    consent can be granted; imports proceed with consent unknown.
    """
    import hashlib
    from datetime import timedelta

    from .enums import SourceSystem
    from .sync.csv_consent import CsvImportPolicy

    max_age_raw = (_env("CLINIC_RECALL_CSV_CONSENT_MAX_AGE_DAYS") or "").strip()
    max_age = None
    if max_age_raw:
        days = int(max_age_raw)
        if days <= 0:
            raise RuntimeError("CLINIC_RECALL_CSV_CONSENT_MAX_AGE_DAYS must be positive")
        max_age = timedelta(days=days)
    ttl_raw = (_env("CLINIC_RECALL_CSV_PREVIEW_TTL_MINUTES") or "30").strip()
    ttl_minutes = int(ttl_raw)
    if ttl_minutes <= 0 or ttl_minutes > 24 * 60:
        raise RuntimeError(
            "CLINIC_RECALL_CSV_PREVIEW_TTL_MINUTES must be between 1 and 1440"
        )
    source_values = tuple(
        value.strip().lower()
        for value in (
            _env("CLINIC_RECALL_CSV_ALLOWED_SOURCE_SYSTEMS") or "csv"
        ).split(",")
        if value.strip()
    )
    try:
        allowed_sources = tuple(dict.fromkeys(SourceSystem(value) for value in source_values))
    except ValueError as exc:
        raise RuntimeError(
            "CLINIC_RECALL_CSV_ALLOWED_SOURCE_SYSTEMS contains an unknown source"
        ) from exc
    if not allowed_sources:
        raise RuntimeError(
            "CLINIC_RECALL_CSV_ALLOWED_SOURCE_SYSTEMS must not be empty"
        )
    return CsvImportPolicy(
        version=(_env("CLINIC_RECALL_CSV_CONSENT_POLICY_VERSION") or "csv-consent-v0-unapproved"),
        statement_hash=hashlib.sha256(CSV_ATTESTATION_STATEMENT.encode("utf-8")).hexdigest(),
        attestation_versions=(CSV_ATTESTATION_VERSION,),
        channels=("sms", "email", "call"),
        max_evidence_age=max_age,
        preview_ttl=timedelta(minutes=ttl_minutes),
        allowed_source_systems=allowed_sources,
    )
