"""Capability-separated Cliniko create and exact read-back client."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from ..config import ClinikoConfig, ClinikoConfigurationError
from .cliniko_client import (
    ClinikoClient,
    ClinikoContractError,
    ClinikoError,
    ClinikoPaginationError,
    ClinikoRateLimiter,
    ClinikoRequestBudget,
    ClinikoTransportError,
)

_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_IDENTIFIER = re.compile(r"[1-9][0-9]*\Z")


@dataclass(frozen=True)
class ExpectedAppointmentSignature:
    """Trusted exact appointment facts used for request and comparison."""

    patient_id: str = field(repr=False)
    business_id: str = field(repr=False)
    practitioner_id: str = field(repr=False)
    appointment_type_id: str = field(repr=False)
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        for value in (
            self.patient_id,
            self.business_id,
            self.practitioner_id,
            self.appointment_type_id,
        ):
            if _IDENTIFIER.fullmatch(value) is None:
                raise ClinikoContractError("invalid_identifier")
        starts_at = _utc(self.starts_at, "starts_at")
        ends_at = _utc(self.ends_at, "ends_at")
        if ends_at <= starts_at:
            raise ClinikoContractError("appointment_interval")
        object.__setattr__(self, "starts_at", starts_at)
        object.__setattr__(self, "ends_at", ends_at)

    def request_payload(self) -> dict[str, str]:
        """Return the complete documented six-field create body."""
        return {
            "appointment_type_id": self.appointment_type_id,
            "business_id": self.business_id,
            "ends_at": _rfc3339(self.ends_at),
            "patient_id": self.patient_id,
            "practitioner_id": self.practitioner_id,
            "starts_at": _rfc3339(self.starts_at),
        }


@dataclass(frozen=True)
class ObservedAppointment:
    """Strict consequential Cliniko read-back fields."""

    provider_id: str = field(repr=False)
    signature: ExpectedAppointmentSignature = field(repr=False)
    active: bool
    updated_at: datetime

    def matches(self, expected: ExpectedAppointmentSignature) -> bool:
        """Require complete equality and an active provider state."""
        return self.active and self.signature == expected

    def completion_hash(self, request_hash: str) -> str:
        """Bind verification to request, provider identity, and exact facts."""
        encoded = json.dumps(
            {
                "appointment_type_id": self.signature.appointment_type_id,
                "business_id": self.signature.business_id,
                "ends_at": _rfc3339(self.signature.ends_at),
                "patient_id": self.signature.patient_id,
                "practitioner_id": self.signature.practitioner_id,
                "provider_id": self.provider_id,
                "request_hash": request_hash,
                "starts_at": _rfc3339(self.signature.starts_at),
                "status": "active",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


class ClinikoBookingClient:
    """Perform only documented create and exact appointment reads."""

    def __init__(
        self,
        config: ClinikoConfig,
        *,
        client: httpx.Client,
        request_budget: ClinikoRequestBudget | None = None,
        rate_limiter: ClinikoRateLimiter | None = None,
        attempt_observer: Callable[[str], None] | None = None,
    ) -> None:
        if not config.enabled:
            raise ClinikoConfigurationError("CLINIC_RECALL_CLINIKO_SYNC_ENABLED")
        if config.api_key is None or config.base_url is None or config.user_agent is None:
            raise ClinikoConfigurationError("CLINIC_RECALL_CLINIKO_WRITE_CONFIG")
        budget = request_budget or ClinikoRequestBudget(config.max_pages * 2 + 4)
        limiter = rate_limiter or ClinikoRateLimiter()
        self._reader = ClinikoClient(
            config,
            client=client,
            request_budget=budget,
            rate_limiter=limiter,
            attempt_observer=attempt_observer,
        )
        self._client = client
        self._base_url = config.base_url
        self._origin = urlsplit(config.base_url)
        self._auth = httpx.BasicAuth(config.api_key, "")
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": config.user_agent,
        }
        self._timeout = httpx.Timeout(config.timeout_seconds)
        self._budget = budget
        self._limiter = limiter
        self._attempt_observer = attempt_observer

    def create_individual_appointment(
        self,
        expected: ExpectedAppointmentSignature,
    ) -> ObservedAppointment:
        """Create one non-repeating individual appointment without retry."""
        payload = self._request_json(
            "POST",
            f"{self._base_url}/individual_appointments",
            body=expected.request_payload(),
            operation_code="individual_appointment_create",
        )
        return self._observed(payload)

    def get_individual_appointment(self, identifier: str) -> ObservedAppointment:
        """Read one exact trusted appointment identity."""
        payload = self._reader.get_item(
            "individual_appointments",
            identifier,
            operation_code="individual_appointment_read_back",
        )
        return self._observed(payload)

    def list_signature_candidates(
        self,
        expected: ExpectedAppointmentSignature,
    ) -> tuple[ObservedAppointment, ...]:
        """Materialize the narrow documented reconciliation superset."""
        params = (
            ("per_page", str(self._reader.per_page)),
            ("q[]", f"appointment_type_id:={expected.appointment_type_id}"),
            ("q[]", f"business_id:={expected.business_id}"),
            ("q[]", f"patient_id:={expected.patient_id}"),
            ("q[]", f"practitioner_id:={expected.practitioner_id}"),
            ("q[]", f"starts_at:>={_rfc3339(expected.starts_at)}"),
            ("q[]", f"starts_at:<={_rfc3339(expected.starts_at)}"),
            ("q[]", f"ends_at:>={_rfc3339(expected.ends_at)}"),
            ("q[]", f"ends_at:<={_rfc3339(expected.ends_at)}"),
        )
        payloads = self._reader.get_collection(
            "individual_appointments",
            collection_key="individual_appointments",
            params=params,
        )
        return tuple(self._observed(payload) for payload in payloads)

    def exact_slot_is_available(
        self,
        expected: ExpectedAppointmentSignature,
    ) -> bool:
        """Materialize complete available-time pages and require one exact start."""
        from_date = expected.starts_at.date()
        to_date = from_date
        next_url: str | None = None
        seen_urls: set[str] = set()
        seen_starts: set[datetime] = set()
        expected_total: int | None = None
        pages = 0
        item_count = 0
        while True:
            if pages >= self._reader.max_pages:
                raise ClinikoPaginationError("max_pages_exceeded")
            if next_url is not None:
                if next_url in seen_urls:
                    raise ClinikoPaginationError("cyclic_next_link")
                seen_urls.add(next_url)
            page = self._reader.get_available_times_page(
                business_id=expected.business_id,
                practitioner_id=expected.practitioner_id,
                appointment_type_id=expected.appointment_type_id,
                from_date=from_date,
                to_date=to_date,
                next_url=next_url,
                operation_code="booking_availability_preflight",
            )
            pages += 1
            if expected_total is None:
                expected_total = page.total_entries
            elif page.total_entries != expected_total:
                raise ClinikoPaginationError("available_times_total_mismatch")
            item_count += len(page.items)
            if item_count > self._reader.max_items:
                raise ClinikoPaginationError("max_items_exceeded")
            for item in page.items:
                if set(item) != {"appointment_start"}:
                    raise ClinikoContractError("available_time_schema")
                appointment_start = _datetime(
                    item.get("appointment_start"),
                    "available_time_start",
                )
                if appointment_start in seen_starts:
                    raise ClinikoContractError("duplicate_available_time")
                seen_starts.add(appointment_start)
            if page.next_url is None:
                break
            next_url = page.next_url
        if expected_total is None or item_count != expected_total:
            raise ClinikoPaginationError("incomplete_available_times")
        return expected.starts_at in seen_starts

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        body: Mapping[str, str],
        operation_code: str,
    ) -> Mapping[str, object]:
        if method != "POST" or url != f"{self._base_url}/individual_appointments":
            raise ClinikoContractError("write_not_allowlisted")
        if self._attempt_observer is not None:
            self._attempt_observer(operation_code)
        self._budget.consume()
        self._limiter.before_request()
        try:
            with self._client.stream(
                method,
                url,
                json=dict(body),
                auth=self._auth,
                headers=self._headers,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                ClinikoClient._raise_for_status(response)
                if response.status_code != 201:
                    raise ClinikoContractError("create_status")
                content_type = response.headers.get("Content-Type", "")
                if content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise ClinikoContractError("content_type")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > _MAX_RESPONSE_BYTES:
                        raise ClinikoContractError("response_too_large")
        except ClinikoError:
            raise
        except httpx.HTTPError as exc:
            raise ClinikoTransportError(_transport_kind(exc)) from None
        try:
            payload = json.loads(bytes(content))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ClinikoContractError("malformed_json") from None
        if not isinstance(payload, dict):
            raise ClinikoContractError("root_schema")
        return payload

    def _observed(self, payload: Mapping[str, object]) -> ObservedAppointment:
        provider_id = _identifier(payload.get("id"))
        signature = ExpectedAppointmentSignature(
            patient_id=_linked_identifier(payload, "patient", "patients", self._origin),
            business_id=_linked_identifier(payload, "business", "businesses", self._origin),
            practitioner_id=_linked_identifier(
                payload,
                "practitioner",
                "practitioners",
                self._origin,
            ),
            appointment_type_id=_linked_identifier(
                payload,
                "appointment_type",
                "appointment_types",
                self._origin,
            ),
            starts_at=_datetime(payload.get("starts_at"), "starts_at"),
            ends_at=_datetime(payload.get("ends_at"), "ends_at"),
        )
        lifecycle = tuple(
            _optional_datetime(payload, field_name)
            for field_name in ("cancelled_at", "archived_at", "deleted_at")
        )
        active = all(value is None for value in lifecycle)
        return ObservedAppointment(
            provider_id=provider_id,
            signature=signature,
            active=active,
            updated_at=_datetime(payload.get("updated_at"), "updated_at"),
        )


def _linked_identifier(
    payload: Mapping[str, object],
    field_name: str,
    collection: str,
    origin,
) -> str:
    linked = payload.get(field_name)
    if not isinstance(linked, dict):
        raise ClinikoContractError("appointment_link")
    links = linked.get("links")
    if not isinstance(links, dict) or not isinstance(links.get("self"), str):
        raise ClinikoContractError("appointment_link")
    try:
        parsed = urlsplit(links["self"])
        port = parsed.port
    except ValueError:
        raise ClinikoContractError("appointment_link") from None
    match = re.fullmatch(rf"/v1/{collection}/([1-9][0-9]*)", parsed.path)
    if (
        parsed.scheme != origin.scheme
        or parsed.hostname != origin.hostname
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        raise ClinikoContractError("appointment_link")
    return match.group(1)


def _identifier(value: object) -> str:
    candidate = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if _IDENTIFIER.fullmatch(candidate) is None:
        raise ClinikoContractError("appointment_id")
    return candidate


def _datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ClinikoContractError(f"appointment_{field_name}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ClinikoContractError(f"appointment_{field_name}") from None
    return _utc(parsed, field_name)


def _optional_datetime(
    payload: Mapping[str, object],
    field_name: str,
) -> datetime | None:
    if field_name not in payload:
        raise ClinikoContractError("appointment_lifecycle")
    value = payload[field_name]
    if value is None:
        return None
    return _datetime(value, field_name)


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset() != UTC.utcoffset(value):
        raise ClinikoContractError(f"appointment_{field_name}")
    return value.astimezone(UTC)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _transport_kind(error: httpx.HTTPError) -> str:
    if isinstance(error, httpx.ReadTimeout):
        return "read_timeout"
    if isinstance(error, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(error, httpx.WriteTimeout):
        return "write_timeout"
    if isinstance(error, httpx.PoolTimeout):
        return "pool_timeout"
    if isinstance(error, httpx.ConnectError):
        return "connect_error"
    if isinstance(error, httpx.RemoteProtocolError):
        return "protocol_error"
    return "transport_error"