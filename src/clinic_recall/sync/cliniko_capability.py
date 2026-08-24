"""Minimized, immutable Cliniko capability evidence snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ..config import ClinikoConfig

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_FIELD_PATH_PATTERN = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_REASON_CODES = frozenset(
    {
        "ambiguous_write",
        "budget_exhausted",
        "cleanup_failed",
        "contract_mismatch",
        "documented_contract",
        "endpoint_absent",
        "fixture_contract",
        "archived_appointment_not_observed",
        "archived_patient_not_observed",
        "available_time_not_observed",
        "cancelled_appointment_not_observed",
        "no_show_not_observed",
        "not_approved",
        "not_tested",
        "pagination_not_observed",
        "permission_denied",
        "provider_unavailable",
        "readback_mismatch",
        "sandbox_observed",
        "unsupported_documented",
    }
)
_REVALIDATION_TRIGGERS = frozenset(
    {
        "adapter_changed",
        "approval_expired",
        "client_changed",
        "config_changed",
        "provider_contract_changed",
        "scheduled",
    }
)


class Capability(StrEnum):
    AUTHENTICATION = "authentication"
    PATIENT_INDEX = "patient_index"
    PATIENT_GET = "patient_get"
    INDIVIDUAL_APPOINTMENT_INDEX = "individual_appointment_index"
    INDIVIDUAL_APPOINTMENT_GET = "individual_appointment_get"
    PATIENT_UPDATED_AT_FILTER = "patient_updated_at_filter"
    INDIVIDUAL_APPOINTMENT_UPDATED_AT_FILTER = (
        "individual_appointment_updated_at_filter"
    )
    ARCHIVED_PATIENT_INCLUSION = "archived_patient_inclusion"
    ARCHIVED_APPOINTMENT_INCLUSION = "archived_appointment_inclusion"
    CANCELLED_APPOINTMENT_INCLUSION = "cancelled_appointment_inclusion"
    NO_SHOW_INCLUSION = "no_show_inclusion"
    PAGINATION = "pagination"
    AVAILABLE_TIMES_READ = "available_times_read"
    APPOINTMENT_CREATE = "appointment_create"
    APPOINTMENT_UPDATE = "appointment_update"
    APPOINTMENT_READ_BACK = "appointment_read_back"
    ERROR_400 = "error_400"
    ERROR_401 = "error_401"
    ERROR_403 = "error_403"
    ERROR_404 = "error_404"
    ERROR_409 = "error_409"
    ERROR_422 = "error_422"
    ERROR_429 = "error_429"
    ERROR_5XX = "error_5xx"
    MALFORMED_RESPONSE = "malformed_response"
    TRANSPORT_FAILURE = "transport_failure"


REQUIRED_CAPABILITIES = frozenset(Capability)


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PATCH = "PATCH"


class EvidenceAuthority(StrEnum):
    DOCUMENTED = "documented"
    FIXTURE_VERIFIED = "fixture_verified"
    SANDBOX_READ_VERIFIED = "sandbox_read_verified"
    SANDBOX_WRITE_VERIFIED = "sandbox_write_verified"


class CapabilityStatus(StrEnum):
    NOT_TESTED = "not_tested"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    INCONCLUSIVE = "inconclusive"
    AMBIGUOUS = "ambiguous"


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _require_sha256(value: str | None, name: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be lowercase SHA-256")


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CapabilityRecord:
    """One minimized capability observation with explicit evidence authority."""

    capability: Capability
    method: HttpMethod
    authority: EvidenceAuthority
    status: CapabilityStatus
    reason_code: str
    status_class: str | None
    response_field_paths: tuple[str, ...]
    schema_sha256: str | None
    checked_at: datetime
    expires_at: datetime | None
    revalidation_triggers: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.reason_code not in _REASON_CODES:
            raise ValueError("reason_code must be allowlisted")
        if self.status_class not in {None, "2xx", "3xx", "4xx", "5xx"}:
            raise ValueError("status_class must be bounded")
        field_paths = tuple(sorted(set(self.response_field_paths)))
        if any(_FIELD_PATH_PATTERN.fullmatch(path) is None for path in field_paths):
            raise ValueError("response_field_paths must contain schema names only")
        object.__setattr__(self, "response_field_paths", field_paths)
        if field_paths:
            _require_sha256(self.schema_sha256, "schema_sha256")
        elif self.schema_sha256 is not None:
            raise ValueError("schema_sha256 requires response_field_paths")
        checked_at = _aware_utc(self.checked_at, "checked_at")
        object.__setattr__(self, "checked_at", checked_at)
        if self.expires_at is not None:
            expires_at = _aware_utc(self.expires_at, "expires_at")
            if expires_at <= checked_at:
                raise ValueError("expires_at must follow checked_at")
            object.__setattr__(self, "expires_at", expires_at)
        triggers = tuple(sorted(set(self.revalidation_triggers)))
        if any(trigger not in _REVALIDATION_TRIGGERS for trigger in triggers):
            raise ValueError("revalidation_triggers must be allowlisted")
        object.__setattr__(self, "revalidation_triggers", triggers)
        if (
            self.authority is EvidenceAuthority.SANDBOX_READ_VERIFIED
            and self.method is not HttpMethod.GET
        ):
            raise ValueError("sandbox read authority is restricted to GET")


@dataclass(frozen=True)
class CapabilityProfile:
    """A complete immutable snapshot for one source/config/client identity."""

    schema_version: int
    generated_at: datetime
    source_commit: str
    client_sha256: str
    adapter_sha256: str
    config_fingerprint: str
    approval_evidence_sha256: str | None
    attempted_request_count: int
    previous_profile_sha256: str | None
    records: tuple[CapabilityRecord, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported capability profile schema version")
        object.__setattr__(self, "generated_at", _aware_utc(self.generated_at, "generated_at"))
        if _COMMIT_PATTERN.fullmatch(self.source_commit) is None:
            raise ValueError("source_commit must be lowercase Git SHA-1")
        _require_sha256(self.client_sha256, "client_sha256")
        _require_sha256(self.adapter_sha256, "adapter_sha256")
        _require_sha256(self.config_fingerprint, "config_fingerprint")
        _require_sha256(
            self.approval_evidence_sha256,
            "approval_evidence_sha256",
            optional=True,
        )
        _require_sha256(
            self.previous_profile_sha256,
            "previous_profile_sha256",
            optional=True,
        )
        if self.attempted_request_count < 0:
            raise ValueError("attempted_request_count must not be negative")
        capabilities = [record.capability for record in self.records]
        if len(capabilities) != len(REQUIRED_CAPABILITIES) or set(capabilities) != set(
            REQUIRED_CAPABILITIES
        ):
            raise ValueError("capability set must contain every required capability once")
        has_sandbox_evidence = any(
            record.authority
            in {
                EvidenceAuthority.SANDBOX_READ_VERIFIED,
                EvidenceAuthority.SANDBOX_WRITE_VERIFIED,
            }
            for record in self.records
        )
        if has_sandbox_evidence and self.approval_evidence_sha256 is None:
            raise ValueError("sandbox evidence requires approval_evidence_sha256")

    @property
    def authority(self) -> str:
        """Return the common authority or ``mixed`` for a layered snapshot."""
        authorities = {record.authority.value for record in self.records}
        return next(iter(authorities)) if len(authorities) == 1 else "mixed"


@dataclass(frozen=True)
class SealedCapabilityProfile:
    """Canonical profile bytes plus their external evidence identity."""

    canonical_bytes: bytes
    sha256: str


def _record_payload(record: CapabilityRecord) -> dict[str, Any]:
    return {
        "authority": record.authority.value,
        "capability": record.capability.value,
        "checked_at": _rfc3339(record.checked_at),
        "expires_at": _rfc3339(record.expires_at) if record.expires_at else None,
        "method": record.method.value,
        "reason_code": record.reason_code,
        "response_field_paths": list(record.response_field_paths),
        "revalidation_triggers": list(record.revalidation_triggers),
        "schema_sha256": record.schema_sha256,
        "status": record.status.value,
        "status_class": record.status_class,
    }


def profile_payload(profile: CapabilityProfile) -> dict[str, Any]:
    """Return the complete allowlisted payload without a self-referential hash."""
    return {
        "adapter_sha256": profile.adapter_sha256,
        "approval_evidence_sha256": profile.approval_evidence_sha256,
        "attempted_request_count": profile.attempted_request_count,
        "authority": profile.authority,
        "client_sha256": profile.client_sha256,
        "config_fingerprint": profile.config_fingerprint,
        "generated_at": _rfc3339(profile.generated_at),
        "previous_profile_sha256": profile.previous_profile_sha256,
        "records": [
            _record_payload(record)
            for record in sorted(profile.records, key=lambda item: item.capability.value)
        ],
        "schema_version": profile.schema_version,
        "source_commit": profile.source_commit,
    }


def canonical_profile_bytes(profile: CapabilityProfile) -> bytes:
    """Serialize a profile deterministically for SHA-256 evidence identity."""
    return json.dumps(
        profile_payload(profile),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def seal_profile(profile: CapabilityProfile) -> SealedCapabilityProfile:
    """Return canonical bytes and their SHA-256 without mutating the profile."""
    canonical = canonical_profile_bytes(profile)
    return SealedCapabilityProfile(
        canonical_bytes=canonical,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def cliniko_config_fingerprint(config: ClinikoConfig) -> str:
    """Hash only non-secret routing and resource-bound configuration."""
    payload = {
        "enabled": config.enabled,
        "max_items": config.max_items,
        "max_pages": config.max_pages,
        "per_page": config.per_page,
        "shard": config.shard,
        "timeout_seconds": config.timeout_seconds,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_METHODS: dict[Capability, HttpMethod] = {
    capability: HttpMethod.GET for capability in Capability
}
_METHODS.update(
    {
        Capability.APPOINTMENT_CREATE: HttpMethod.POST,
        Capability.APPOINTMENT_UPDATE: HttpMethod.PATCH,
    }
)
_ERROR_STATUS_CLASS: dict[Capability, str] = {
    Capability.ERROR_400: "4xx",
    Capability.ERROR_401: "4xx",
    Capability.ERROR_403: "4xx",
    Capability.ERROR_404: "4xx",
    Capability.ERROR_409: "4xx",
    Capability.ERROR_422: "4xx",
    Capability.ERROR_429: "4xx",
    Capability.ERROR_5XX: "5xx",
}
_RESPONSE_FIELDS: dict[Capability, tuple[str, ...]] = {
    Capability.PATIENT_INDEX: ("links", "patients", "total_entries"),
    Capability.PATIENT_GET: (
        "archived_at",
        "email",
        "first_name",
        "id",
        "last_name",
        "patient_phone_numbers",
        "preferred_first_name",
        "updated_at",
    ),
    Capability.INDIVIDUAL_APPOINTMENT_INDEX: (
        "individual_appointments",
        "links",
        "total_entries",
    ),
    Capability.INDIVIDUAL_APPOINTMENT_GET: (
        "archived_at",
        "cancelled_at",
        "did_not_arrive",
        "ends_at",
        "id",
        "patient",
        "patient_arrived",
        "starts_at",
        "updated_at",
    ),
    Capability.AVAILABLE_TIMES_READ: (
        "available_times",
        "available_times.appointment_start",
        "links",
        "total_entries",
    ),
}
_UNEXERCISED_FIXTURE_CAPABILITIES = frozenset(
    {
        Capability.APPOINTMENT_CREATE,
        Capability.APPOINTMENT_UPDATE,
        Capability.APPOINTMENT_READ_BACK,
    }
)


def schema_hash_for_fields(fields: Sequence[str]) -> str | None:
    """Return the canonical schema hash for allowlisted response field paths."""
    if not fields:
        return None
    encoded = json.dumps(
        sorted(fields), separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fixture_capability_profile(
    *,
    generated_at: datetime,
    source_commit: str,
    client_sha256: str,
    adapter_sha256: str,
    config_fingerprint: str,
) -> CapabilityProfile:
    """Build a complete local-only profile from documented fixture contracts."""
    checked_at = _aware_utc(generated_at, "generated_at")
    records = tuple(
        CapabilityRecord(
            capability=capability,
            method=_METHODS[capability],
            authority=EvidenceAuthority.FIXTURE_VERIFIED,
            status=(
                CapabilityStatus.NOT_TESTED
                if capability in _UNEXERCISED_FIXTURE_CAPABILITIES
                else CapabilityStatus.SUPPORTED
            ),
            reason_code=(
                "not_tested"
                if capability in _UNEXERCISED_FIXTURE_CAPABILITIES
                else "fixture_contract"
            ),
            status_class=(
                None
                if capability in _UNEXERCISED_FIXTURE_CAPABILITIES
                else _ERROR_STATUS_CLASS.get(capability, "2xx")
            ),
            response_field_paths=_RESPONSE_FIELDS.get(capability, ()),
            schema_sha256=schema_hash_for_fields(_RESPONSE_FIELDS.get(capability, ())),
            checked_at=checked_at,
            expires_at=checked_at + timedelta(days=30),
            revalidation_triggers=("client_changed", "provider_contract_changed"),
        )
        for capability in sorted(Capability, key=lambda item: item.value)
    )
    return CapabilityProfile(
        schema_version=1,
        generated_at=checked_at,
        source_commit=source_commit,
        client_sha256=client_sha256,
        adapter_sha256=adapter_sha256,
        config_fingerprint=config_fingerprint,
        approval_evidence_sha256=None,
        attempted_request_count=0,
        previous_profile_sha256=None,
        records=records,
    )


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError("checked-at must be RFC3339") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("checked-at must be timezone-aware")
    return parsed.astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    """Emit a fixture-only canonical profile; this command performs no I/O."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", default="fixture", choices=("fixture",))
    parser.add_argument("--checked-at", required=True, type=_parse_utc)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--client-sha256", required=True)
    parser.add_argument("--adapter-sha256", required=True)
    parser.add_argument("--config-fingerprint", required=True)
    args = parser.parse_args(argv)
    profile = fixture_capability_profile(
        generated_at=args.checked_at,
        source_commit=args.source_commit,
        client_sha256=args.client_sha256,
        adapter_sha256=args.adapter_sha256,
        config_fingerprint=args.config_fingerprint,
    )
    print(canonical_profile_bytes(profile).decode("ascii"))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())