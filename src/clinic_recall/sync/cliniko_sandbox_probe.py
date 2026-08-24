"""Explicitly gated, non-runtime Cliniko sandbox capability probes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from ..config import ClinikoConfig, get_cliniko_config
from .cliniko_adapter import ClinikoAppointmentRecord, ClinikoPatientRecord
from .cliniko_capability import (
    Capability,
    CapabilityProfile,
    CapabilityRecord,
    CapabilityStatus,
    EvidenceAuthority,
    canonical_profile_bytes,
    cliniko_config_fingerprint,
    fixture_capability_profile,
    schema_hash_for_fields,
    seal_profile,
)
from .cliniko_client import (
    ClinikoClient,
    ClinikoContractError,
    ClinikoError,
    ClinikoRequestBudget,
)

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_IDENTIFIER_PATTERN = re.compile(r"[1-9][0-9]*")
_UK_SHARDS = frozenset({"uk1", "uk2", "uk3"})
_MIN_PLANNED_ATTEMPTS = 12
_MAX_PLANNED_ATTEMPTS = 16
_HARD_ATTEMPT_CAP = 20

_PATIENT_FIELDS = frozenset(
    {
        "archived_at",
        "email",
        "first_name",
        "id",
        "last_name",
        "patient_phone_numbers",
        "preferred_first_name",
        "updated_at",
    }
)
_APPOINTMENT_FIELDS = frozenset(
    {
        "archived_at",
        "cancelled_at",
        "did_not_arrive",
        "ends_at",
        "id",
        "patient",
        "patient_arrived",
        "starts_at",
        "updated_at",
    }
)
_READ_CAPABILITIES = frozenset(
    {
        Capability.AUTHENTICATION,
        Capability.PATIENT_INDEX,
        Capability.PATIENT_GET,
        Capability.INDIVIDUAL_APPOINTMENT_INDEX,
        Capability.INDIVIDUAL_APPOINTMENT_GET,
        Capability.PATIENT_UPDATED_AT_FILTER,
        Capability.INDIVIDUAL_APPOINTMENT_UPDATED_AT_FILTER,
        Capability.ARCHIVED_PATIENT_INCLUSION,
        Capability.ARCHIVED_APPOINTMENT_INCLUSION,
        Capability.CANCELLED_APPOINTMENT_INCLUSION,
        Capability.NO_SHOW_INCLUSION,
        Capability.PAGINATION,
        Capability.AVAILABLE_TIMES_READ,
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "adapter_sha256",
        "appointment_id",
        "appointment_type_id",
        "approval_evidence_sha256",
        "availability_from",
        "availability_to",
        "business_id",
        "client_sha256",
        "config_fingerprint",
        "expected_shard",
        "expires_at",
        "fixture_profile_sha256",
        "patient_id",
        "planned_attempts",
        "practitioner_id",
        "schema_version",
        "source_commit",
        "synthetic_only",
        "updated_after",
    }
)


class SandboxProbeConfigurationError(ValueError):
    """A bounded local configuration failure raised before provider access."""


def _aware_utc(value: datetime, reason: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SandboxProbeConfigurationError(reason)
    return value.astimezone(UTC)


def _require_hash(value: str, pattern: re.Pattern[str], reason: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SandboxProbeConfigurationError(reason)


@dataclass(frozen=True)
class SandboxReadManifest:
    """Private, hash-bound authority and synthetic identifiers for one read run."""

    schema_version: int
    approval_evidence_sha256: str
    expected_shard: str
    synthetic_only: bool
    source_commit: str
    fixture_profile_sha256: str
    client_sha256: str
    adapter_sha256: str
    config_fingerprint: str
    planned_attempts: int
    expires_at: datetime
    patient_id: str = field(repr=False)
    appointment_id: str = field(repr=False)
    business_id: str = field(repr=False)
    practitioner_id: str = field(repr=False)
    appointment_type_id: str = field(repr=False)
    updated_after: datetime
    availability_from: date
    availability_to: date

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != 1
        ):
            raise SandboxProbeConfigurationError("manifest_schema")
        _require_hash(
            self.approval_evidence_sha256,
            _SHA256_PATTERN,
            "approval_evidence_sha256",
        )
        _require_hash(self.source_commit, _COMMIT_PATTERN, "source_commit")
        for name in (
            "fixture_profile_sha256",
            "client_sha256",
            "adapter_sha256",
            "config_fingerprint",
        ):
            _require_hash(str(getattr(self, name)), _SHA256_PATTERN, name)
        if not isinstance(self.expected_shard, str) or self.expected_shard not in _UK_SHARDS:
            raise SandboxProbeConfigurationError("expected_shard")
        if self.synthetic_only is not True:
            raise SandboxProbeConfigurationError("synthetic_only")
        if (
            not isinstance(self.planned_attempts, int)
            or isinstance(self.planned_attempts, bool)
            or not _MIN_PLANNED_ATTEMPTS
            <= self.planned_attempts
            <= _MAX_PLANNED_ATTEMPTS
        ):
            raise SandboxProbeConfigurationError("planned_attempts")
        expires_at = _aware_utc(self.expires_at, "expires_at")
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(
            self,
            "updated_after",
            _aware_utc(self.updated_after, "updated_after"),
        )
        identifiers = (
            self.patient_id,
            self.appointment_id,
            self.business_id,
            self.practitioner_id,
            self.appointment_type_id,
        )
        if any(
            not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None
            for value in identifiers
        ):
            raise SandboxProbeConfigurationError("synthetic_identifier")
        if not isinstance(self.availability_from, date) or isinstance(
            self.availability_from, datetime
        ):
            raise SandboxProbeConfigurationError("availability_from")
        if not isinstance(self.availability_to, date) or isinstance(
            self.availability_to, datetime
        ):
            raise SandboxProbeConfigurationError("availability_to")
        window_days = (self.availability_to - self.availability_from).days
        if not 0 <= window_days <= 7:
            raise SandboxProbeConfigurationError("availability_window")


def _private_mode(path: Path, *, directory: bool) -> bool:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        return False
    expected_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(
        metadata.st_mode
    )
    return expected_type and stat.S_IMODE(metadata.st_mode) & 0o077 == 0


def _require_private_parent(path: Path) -> None:
    if not _private_mode(path, directory=True):
        raise SandboxProbeConfigurationError("private_parent")


def _parse_utc(value: object, reason: str) -> datetime:
    if not isinstance(value, str):
        raise SandboxProbeConfigurationError(reason)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SandboxProbeConfigurationError(reason) from None
    return _aware_utc(parsed, reason)


def _parse_date(value: object, reason: str) -> date:
    if not isinstance(value, str):
        raise SandboxProbeConfigurationError(reason)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SandboxProbeConfigurationError(reason) from None


def load_private_manifest(path: Path, *, now: datetime) -> SandboxReadManifest:
    """Load one exact-schema manifest from a private file and directory."""
    _require_private_parent(path.parent)
    if not _private_mode(path, directory=False):
        raise SandboxProbeConfigurationError("private_file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SandboxProbeConfigurationError("manifest_json") from None
    if not isinstance(payload, dict) or set(payload) != set(_MANIFEST_FIELDS):
        raise SandboxProbeConfigurationError("manifest_schema")
    checked_now = _aware_utc(now, "now")
    manifest = SandboxReadManifest(
        schema_version=payload["schema_version"],
        approval_evidence_sha256=payload["approval_evidence_sha256"],
        expected_shard=payload["expected_shard"],
        synthetic_only=payload["synthetic_only"],
        source_commit=payload["source_commit"],
        fixture_profile_sha256=payload["fixture_profile_sha256"],
        client_sha256=payload["client_sha256"],
        adapter_sha256=payload["adapter_sha256"],
        config_fingerprint=payload["config_fingerprint"],
        planned_attempts=payload["planned_attempts"],
        expires_at=_parse_utc(payload["expires_at"], "expires_at"),
        patient_id=payload["patient_id"],
        appointment_id=payload["appointment_id"],
        business_id=payload["business_id"],
        practitioner_id=payload["practitioner_id"],
        appointment_type_id=payload["appointment_type_id"],
        updated_after=_parse_utc(payload["updated_after"], "updated_after"),
        availability_from=_parse_date(payload["availability_from"], "availability_from"),
        availability_to=_parse_date(payload["availability_to"], "availability_to"),
    )
    if manifest.expires_at <= checked_now:
        raise SandboxProbeConfigurationError("expires_at")
    return manifest


class PrivateRequestLedger:
    """Append-only, minimized write-ahead accounting for provider attempts."""

    def __init__(
        self,
        *,
        path: Path,
        planned_attempts: int,
        clock: Callable[[], datetime],
    ) -> None:
        self.path = path
        self.planned_attempts = planned_attempts
        self._clock = clock
        self._attempts = 0
        self._lock = threading.Lock()

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        planned_attempts: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> PrivateRequestLedger:
        _require_private_parent(path.parent)
        if (
            not isinstance(planned_attempts, int)
            or isinstance(planned_attempts, bool)
            or not _MIN_PLANNED_ATTEMPTS <= planned_attempts <= _MAX_PLANNED_ATTEMPTS
        ):
            raise SandboxProbeConfigurationError("planned_attempts")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError:
            raise SandboxProbeConfigurationError("ledger_create") from None
        os.close(descriptor)
        return cls(path=path, planned_attempts=planned_attempts, clock=clock)

    @property
    def attempts(self) -> int:
        return self._attempts

    def record_attempt(self, operation_code: str) -> None:
        """Persist an allowlisted GET attempt before transport invocation."""
        try:
            capability = Capability(operation_code)
        except ValueError:
            raise SandboxProbeConfigurationError("ledger_capability") from None
        if capability not in _READ_CAPABILITIES:
            raise SandboxProbeConfigurationError("ledger_capability")
        with self._lock:
            if self._attempts >= min(self.planned_attempts, _HARD_ATTEMPT_CAP):
                raise SandboxProbeConfigurationError("request_budget")
            attempted_at = _aware_utc(self._clock(), "ledger_clock")
            sequence = self._attempts + 1
            payload = {
                "attempted_at": attempted_at.isoformat().replace("+00:00", "Z"),
                "capability": capability.value,
                "method": "GET",
                "sequence": sequence,
            }
            encoded = (
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                + "\n"
            ).encode("ascii")
            flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.path, flags)
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise OSError
                remaining = memoryview(encoded)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError
                    remaining = remaining[written:]
                os.fsync(descriptor)
            except OSError:
                raise SandboxProbeConfigurationError("ledger_write") from None
            finally:
                if "descriptor" in locals():
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            self._attempts = sequence


@dataclass(frozen=True)
class SandboxReadProbeResult:
    """A minimized profile and truthful completion state."""

    profile: CapabilityProfile
    complete: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class _Observation:
    status: CapabilityStatus
    reason_code: str
    field_paths: tuple[str, ...] = ()


def _filtered_fields(
    payload: Mapping[str, object],
    allowed: frozenset[str],
) -> tuple[str, ...]:
    return tuple(sorted(set(payload) & set(allowed)))


def _collection_fields(
    collection_key: str,
    items: Sequence[Mapping[str, object]],
    allowed: frozenset[str],
) -> tuple[str, ...]:
    fields = {collection_key, "links", "total_entries"}
    for item in items:
        fields.update(f"{collection_key}.{name}" for name in set(item) & set(allowed))
    return tuple(sorted(fields))


def _validate_available_times(items: Sequence[Mapping[str, object]]) -> None:
    for item in items:
        if set(item) - {"appointment_start"}:
            raise ClinikoContractError("available_time_schema")
        value = item.get("appointment_start")
        if not isinstance(value, str):
            raise ClinikoContractError("available_time_schema")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ClinikoContractError("available_time_schema") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ClinikoContractError("available_time_schema")


def _updated_filter(value: datetime) -> str:
    return f"updated_at:>{value.astimezone(UTC).isoformat().replace('+00:00', 'Z')}"


def run_sandbox_read_probe(
    *,
    manifest: SandboxReadManifest,
    config: ClinikoConfig,
    http_client: httpx.Client,
    ledger: PrivateRequestLedger,
    checked_at: datetime,
) -> SandboxReadProbeResult:
    """Run the fixed GET-only sandbox manifest through an injected transport."""
    current = _aware_utc(checked_at, "checked_at")
    if current >= manifest.expires_at:
        raise SandboxProbeConfigurationError("expires_at")
    if not config.enabled or config.shard != manifest.expected_shard:
        raise SandboxProbeConfigurationError("expected_shard")
    if cliniko_config_fingerprint(config) != manifest.config_fingerprint:
        raise SandboxProbeConfigurationError("config_fingerprint")
    if ledger.planned_attempts != manifest.planned_attempts or ledger.attempts != 0:
        raise SandboxProbeConfigurationError("ledger_state")

    client = ClinikoClient(
        config,
        client=http_client,
        request_budget=ClinikoRequestBudget(max_attempts=manifest.planned_attempts),
        attempt_observer=ledger.record_attempt,
    )
    observations: dict[Capability, _Observation] = {}
    inconclusive: set[str] = set()
    per_page = (("per_page", "1"),)

    patient_index = client.get_collection_page(
        "patients",
        collection_key="patients",
        params=per_page,
        operation_code=Capability.PATIENT_INDEX.value,
    )
    for payload in patient_index.items:
        ClinikoPatientRecord.from_payload(payload)
    observations[Capability.AUTHENTICATION] = _Observation(
        CapabilityStatus.SUPPORTED,
        "sandbox_observed",
    )
    observations[Capability.PATIENT_INDEX] = _Observation(
        CapabilityStatus.SUPPORTED,
        "sandbox_observed",
        _collection_fields("patients", patient_index.items, _PATIENT_FIELDS),
    )

    patient_payload = client.get_item(
        "patients",
        manifest.patient_id,
        operation_code=Capability.PATIENT_GET.value,
    )
    ClinikoPatientRecord.from_payload(patient_payload)
    observations[Capability.PATIENT_GET] = _Observation(
        CapabilityStatus.SUPPORTED,
        "sandbox_observed",
        _filtered_fields(patient_payload, _PATIENT_FIELDS),
    )

    patient_updated = client.get_collection_page(
        "patients",
        collection_key="patients",
        params=per_page + (("q[]", _updated_filter(manifest.updated_after)),),
        operation_code=Capability.PATIENT_UPDATED_AT_FILTER.value,
    )
    for payload in patient_updated.items:
        ClinikoPatientRecord.from_payload(payload)
    observations[Capability.PATIENT_UPDATED_AT_FILTER] = _Observation(
        CapabilityStatus.SUPPORTED,
        "sandbox_observed",
        _collection_fields("patients", patient_updated.items, _PATIENT_FIELDS),
    )

    archived_patients = client.get_collection_page(
        "patients",
        collection_key="patients",
        params=per_page + (("q[]", "archived_at:*"),),
        operation_code=Capability.ARCHIVED_PATIENT_INCLUSION.value,
    )
    archived_patient_records = tuple(
        ClinikoPatientRecord.from_payload(payload) for payload in archived_patients.items
    )
    archived_patient_seen = any(record.archived_at is not None for record in archived_patient_records)
    if not archived_patient_seen:
        inconclusive.add("archived_patient_not_observed")
    observations[Capability.ARCHIVED_PATIENT_INCLUSION] = _Observation(
        CapabilityStatus.SUPPORTED if archived_patient_seen else CapabilityStatus.INCONCLUSIVE,
        "sandbox_observed" if archived_patient_seen else "archived_patient_not_observed",
        _collection_fields("patients", archived_patients.items, _PATIENT_FIELDS),
    )

    appointment_index = client.get_collection_page(
        "individual_appointments",
        collection_key="individual_appointments",
        params=per_page,
        operation_code=Capability.INDIVIDUAL_APPOINTMENT_INDEX.value,
    )
    for payload in appointment_index.items:
        ClinikoAppointmentRecord.from_payload(payload, base_url=client.base_url)
    observations[Capability.INDIVIDUAL_APPOINTMENT_INDEX] = _Observation(
        CapabilityStatus.SUPPORTED,
        "sandbox_observed",
        _collection_fields(
            "individual_appointments",
            appointment_index.items,
            _APPOINTMENT_FIELDS,
        ),
    )

    appointment_payload = client.get_item(
        "individual_appointments",
        manifest.appointment_id,
        operation_code=Capability.INDIVIDUAL_APPOINTMENT_GET.value,
    )
    ClinikoAppointmentRecord.from_payload(appointment_payload, base_url=client.base_url)
    observations[Capability.INDIVIDUAL_APPOINTMENT_GET] = _Observation(
        CapabilityStatus.SUPPORTED,
        "sandbox_observed",
        _filtered_fields(appointment_payload, _APPOINTMENT_FIELDS),
    )

    appointment_updated = client.get_collection_page(
        "individual_appointments",
        collection_key="individual_appointments",
        params=per_page + (("q[]", _updated_filter(manifest.updated_after)),),
        operation_code=Capability.INDIVIDUAL_APPOINTMENT_UPDATED_AT_FILTER.value,
    )
    for payload in appointment_updated.items:
        ClinikoAppointmentRecord.from_payload(payload, base_url=client.base_url)
    observations[Capability.INDIVIDUAL_APPOINTMENT_UPDATED_AT_FILTER] = _Observation(
        CapabilityStatus.SUPPORTED,
        "sandbox_observed",
        _collection_fields(
            "individual_appointments",
            appointment_updated.items,
            _APPOINTMENT_FIELDS,
        ),
    )

    lifecycle_queries = (
        (
            Capability.ARCHIVED_APPOINTMENT_INCLUSION,
            "archived_at:*",
            "archived_appointment_not_observed",
            lambda record: record.archived_at is not None,
        ),
        (
            Capability.CANCELLED_APPOINTMENT_INCLUSION,
            "cancelled_at:*",
            "cancelled_appointment_not_observed",
            lambda record: record.cancelled_at is not None,
        ),
        (
            Capability.NO_SHOW_INCLUSION,
            "did_not_arrive:=true",
            "no_show_not_observed",
            lambda record: record.did_not_arrive is True,
        ),
    )
    for capability, query, missing_reason, predicate in lifecycle_queries:
        page = client.get_collection_page(
            "individual_appointments",
            collection_key="individual_appointments",
            params=per_page + (("q[]", query),),
            operation_code=capability.value,
        )
        records = tuple(
            ClinikoAppointmentRecord.from_payload(payload, base_url=client.base_url)
            for payload in page.items
        )
        observed = any(predicate(record) for record in records)
        if not observed:
            inconclusive.add(missing_reason)
        observations[capability] = _Observation(
            CapabilityStatus.SUPPORTED if observed else CapabilityStatus.INCONCLUSIVE,
            "sandbox_observed" if observed else missing_reason,
            _collection_fields(
                "individual_appointments",
                page.items,
                _APPOINTMENT_FIELDS,
            ),
        )

    available_times = client.get_available_times_page(
        business_id=manifest.business_id,
        practitioner_id=manifest.practitioner_id,
        appointment_type_id=manifest.appointment_type_id,
        from_date=manifest.availability_from,
        to_date=manifest.availability_to,
        operation_code=Capability.AVAILABLE_TIMES_READ.value,
    )
    _validate_available_times(available_times.items)
    available_time_seen = bool(available_times.items)
    if not available_time_seen:
        inconclusive.add("available_time_not_observed")
    available_fields = {"available_times", "links", "total_entries"}
    if available_time_seen:
        available_fields.add("available_times.appointment_start")
    observations[Capability.AVAILABLE_TIMES_READ] = _Observation(
        CapabilityStatus.SUPPORTED
        if available_time_seen
        else CapabilityStatus.INCONCLUSIVE,
        "sandbox_observed" if available_time_seen else "available_time_not_observed",
        tuple(sorted(available_fields)),
    )

    next_url = patient_index.next_url or appointment_index.next_url
    if next_url is None:
        inconclusive.add("pagination_not_observed")
        observations[Capability.PAGINATION] = _Observation(
            CapabilityStatus.INCONCLUSIVE,
            "pagination_not_observed",
        )
    else:
        resource = "patients" if patient_index.next_url is not None else "individual_appointments"
        collection_key = resource
        next_page = client.get_collection_page(
            resource,
            collection_key=collection_key,
            next_url=next_url,
            operation_code=Capability.PAGINATION.value,
        )
        allowed = _PATIENT_FIELDS if resource == "patients" else _APPOINTMENT_FIELDS
        if resource == "patients":
            for payload in next_page.items:
                ClinikoPatientRecord.from_payload(payload)
        else:
            for payload in next_page.items:
                ClinikoAppointmentRecord.from_payload(payload, base_url=client.base_url)
        observations[Capability.PAGINATION] = _Observation(
            CapabilityStatus.SUPPORTED,
            "sandbox_observed",
            _collection_fields(collection_key, next_page.items, allowed),
        )

    fixture_profile = fixture_capability_profile(
        generated_at=current,
        source_commit=manifest.source_commit,
        client_sha256=manifest.client_sha256,
        adapter_sha256=manifest.adapter_sha256,
        config_fingerprint=manifest.config_fingerprint,
    )
    expires_at = min(manifest.expires_at, current + timedelta(days=30))
    records: list[CapabilityRecord] = []
    for record in fixture_profile.records:
        observation = observations.get(record.capability)
        if observation is None:
            records.append(record)
            continue
        records.append(
            replace(
                record,
                authority=EvidenceAuthority.SANDBOX_READ_VERIFIED,
                status=observation.status,
                reason_code=observation.reason_code,
                status_class="2xx",
                response_field_paths=(
                    observation.field_paths
                    if observation.status is CapabilityStatus.SUPPORTED
                    else ()
                ),
                schema_sha256=(
                    schema_hash_for_fields(observation.field_paths)
                    if observation.status is CapabilityStatus.SUPPORTED
                    else None
                ),
                checked_at=current,
                expires_at=expires_at,
                revalidation_triggers=(
                    "approval_expired",
                    "client_changed",
                    "config_changed",
                    "provider_contract_changed",
                ),
            )
        )
    profile = CapabilityProfile(
        schema_version=1,
        generated_at=current,
        source_commit=manifest.source_commit,
        client_sha256=manifest.client_sha256,
        adapter_sha256=manifest.adapter_sha256,
        config_fingerprint=manifest.config_fingerprint,
        approval_evidence_sha256=manifest.approval_evidence_sha256,
        attempted_request_count=ledger.attempts,
        previous_profile_sha256=manifest.fixture_profile_sha256,
        records=tuple(records),
    )
    return SandboxReadProbeResult(
        profile=profile,
        complete=not inconclusive and _READ_CAPABILITIES <= set(observations),
        reason_codes=tuple(sorted(inconclusive)),
    )


def _reserve_private_profile(path: Path) -> None:
    _require_private_parent(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise SandboxProbeConfigurationError("profile_create") from None
    finally:
        if "descriptor" in locals():
            try:
                os.close(descriptor)
            except OSError:
                pass


def _write_private_profile(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise OSError
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(descriptor)
    except OSError:
        raise SandboxProbeConfigurationError("profile_write") from None
    finally:
        if "descriptor" in locals():
            try:
                os.close(descriptor)
            except OSError:
                pass


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(64 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise SandboxProbeConfigurationError("source_identity") from None
    return digest.hexdigest()


def verify_current_source_identity(manifest: SandboxReadManifest) -> None:
    """Bind an approved manifest to the current Git head and owning sources."""
    repository_root = Path(__file__).resolve().parents[3]
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise SandboxProbeConfigurationError("source_identity") from None
    if completed.stdout.strip() != manifest.source_commit:
        raise SandboxProbeConfigurationError("source_commit")
    if _file_sha256(Path(__file__).with_name("cliniko_client.py")) != manifest.client_sha256:
        raise SandboxProbeConfigurationError("client_sha256")
    if _file_sha256(Path(__file__).with_name("cliniko_adapter.py")) != manifest.adapter_sha256:
        raise SandboxProbeConfigurationError("adapter_sha256")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], httpx.Client] = httpx.Client,
    config_loader: Callable[[], ClinikoConfig] = get_cliniko_config,
    source_identity_validator: Callable[
        [SandboxReadManifest], None
    ] = verify_current_source_identity,
    clock: Callable[[], datetime] = _utc_now,
) -> int:
    """Run an explicitly confirmed live read probe; never defaults to provider I/O."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("sandbox-read",))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--profile-output", required=True, type=Path)
    parser.add_argument("--approval-sha256", required=True)
    parser.add_argument("--confirm-sandbox-read", required=True, action="store_true")
    args = parser.parse_args(argv)
    now = clock()
    try:
        manifest = load_private_manifest(args.manifest, now=now)
        if args.approval_sha256 != manifest.approval_evidence_sha256:
            raise SandboxProbeConfigurationError("approval_evidence_sha256")
        config = config_loader()
        source_identity_validator(manifest)
        _reserve_private_profile(args.profile_output)
        ledger = PrivateRequestLedger.create(
            args.ledger,
            planned_attempts=manifest.planned_attempts,
        )
        with client_factory() as http_client:
            result = run_sandbox_read_probe(
                manifest=manifest,
                config=config,
                http_client=http_client,
                ledger=ledger,
                checked_at=now,
            )
        sealed = seal_profile(result.profile)
        _write_private_profile(
            args.profile_output,
            canonical_profile_bytes(result.profile),
        )
        print(
            json.dumps(
                {
                    "attempted_request_count": result.profile.attempted_request_count,
                    "complete": result.complete,
                    "profile_sha256": sealed.sha256,
                    "reason_codes": list(result.reason_codes),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0 if result.complete else 2
    except (SandboxProbeConfigurationError, ClinikoError) as error:
        print(
            json.dumps(
                {"complete": False, "reason_code": str(error)},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {"complete": False, "reason_code": "unexpected_failure"},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())