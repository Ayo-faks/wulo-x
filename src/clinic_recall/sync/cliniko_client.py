"""Strict read-only HTTP client for the Cliniko API."""

from __future__ import annotations

import json
import re
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import ClinikoConfig, ClinikoConfigurationError

_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_RESOURCE_PATTERN = re.compile(r"[a-z_]+(?:/[A-Za-z0-9_-]+)?")
_IDENTIFIER_PATTERN = re.compile(r"[1-9][0-9]*")
_ITEM_RESOURCES = frozenset({"patients", "individual_appointments"})


class ClinikoError(RuntimeError):
    """Base class for bounded Cliniko failures."""


class ClinikoAuthenticationError(ClinikoError):
    """Cliniko rejected the API user's credentials or permissions."""


class ClinikoValidationError(ClinikoError):
    """Cliniko definitively rejected the supplied request."""


class ClinikoNotFoundError(ClinikoError):
    """Cliniko could not find the requested resource."""


class ClinikoRateLimitedError(ClinikoError):
    """Cliniko rate-limited the API user until an aware UTC instant."""

    def __init__(self, reset_at: datetime) -> None:
        super().__init__("rate_limited")
        self.reset_at = reset_at


class ClinikoServerError(ClinikoError):
    """Cliniko returned a server-side status class."""

    def __init__(self) -> None:
        super().__init__("server_error")
        self.status_class = "5xx"


class ClinikoTransportError(ClinikoError):
    """A minimized transport failure that retains no original exception."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class ClinikoContractError(ClinikoError):
    """A bounded response or request-contract failure."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class ClinikoPaginationError(ClinikoError):
    """A bounded unsafe or incomplete pagination failure."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass
class ClinikoRequestBudget:
    """Count every attempted request before transport invocation."""

    max_attempts: int
    attempts: int = 0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")

    def consume(self) -> None:
        """Reserve one attempt or fail before provider I/O."""
        if self.attempts >= self.max_attempts:
            raise ClinikoContractError("request_budget_exhausted")
        self.attempts += 1


@dataclass(frozen=True)
class ClinikoCollectionPage:
    """One validated page retained transiently for capability probing."""

    items: tuple[dict[str, object], ...]
    next_url: str | None
    total_entries: int


class ClinikoRateLimiter:
    """Keep a client below Cliniko's documented per-user rate ceiling."""

    def __init__(
        self,
        *,
        max_requests: int = 180,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_requests < 1 or window_seconds <= 0:
            raise ValueError("invalid rate limiter bounds")
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._attempted_at: deque[float] = deque()

    def before_request(self) -> None:
        """Wait only for local rate headroom, then record the attempt."""
        now = self._clock()
        self._discard_expired(now)
        if len(self._attempted_at) >= self._max_requests:
            delay = self._attempted_at[0] + self._window_seconds - now
            if delay > 0:
                self._sleeper(delay)
            now = self._clock()
            self._discard_expired(now)
            if len(self._attempted_at) >= self._max_requests:
                raise ClinikoContractError("local_rate_limit_exhausted")
        self._attempted_at.append(now)

    def _discard_expired(self, now: float) -> None:
        threshold = now - self._window_seconds
        while self._attempted_at and self._attempted_at[0] <= threshold:
            self._attempted_at.popleft()


class ClinikoClient:
    """Perform bounded Cliniko GET requests through an injected client."""

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
        if config.api_key is None:
            raise ClinikoConfigurationError("CLINIC_RECALL_CLINIKO_API_KEY")
        if config.base_url is None:
            raise ClinikoConfigurationError("CLINIC_RECALL_CLINIKO_SHARD")
        if config.user_agent is None:
            raise ClinikoConfigurationError("CLINIC_RECALL_CLINIKO_USER_AGENT")

        self._client = client
        self._base_url = config.base_url
        self._origin = urlsplit(self._base_url)
        self._auth = httpx.BasicAuth(config.api_key, "")
        self._headers = {
            "Accept": "application/json",
            "User-Agent": config.user_agent,
        }
        self._timeout = httpx.Timeout(
            connect=config.timeout_seconds,
            read=config.timeout_seconds,
            write=config.timeout_seconds,
            pool=config.timeout_seconds,
        )
        self._per_page = config.per_page
        self._max_pages = config.max_pages
        self._max_items = config.max_items
        self._request_budget = request_budget or ClinikoRequestBudget(
            max_attempts=config.max_pages * 2
        )
        self._rate_limiter = rate_limiter or ClinikoRateLimiter()
        self._attempt_observer = attempt_observer

    @property
    def base_url(self) -> str:
        """Return the validated non-secret API base."""
        return self._base_url

    @property
    def per_page(self) -> int:
        """Return the validated collection page size."""
        return self._per_page

    @property
    def max_pages(self) -> int:
        """Return the configured complete-collection page bound."""
        return self._max_pages

    @property
    def max_items(self) -> int:
        """Return the configured complete-collection item bound."""
        return self._max_items

    def get_collection(
        self,
        resource: str,
        *,
        collection_key: str,
        params: Sequence[tuple[str, str]] = (),
    ) -> tuple[dict[str, object], ...]:
        """Fetch one complete, bounded paginated collection."""
        if _RESOURCE_PATTERN.fullmatch(resource) is None or "/" in resource:
            raise ClinikoContractError("invalid_resource")
        if _RESOURCE_PATTERN.fullmatch(collection_key) is None or "/" in collection_key:
            raise ClinikoContractError("invalid_collection_key")

        current_url = f"{self._base_url}/{resource}"
        expected_path = f"/v1/{resource}"
        current_params: Sequence[tuple[str, str]] | None = tuple(params)
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        items: list[dict[str, object]] = []
        pages = 0

        while True:
            if current_url in seen_urls:
                raise ClinikoPaginationError("cyclic_next_link")
            seen_urls.add(current_url)
            payload = self._get_json(
                current_url,
                params=current_params,
                operation_code=resource,
            )
            pages += 1

            page_items, next_url = self._collection_page(payload, collection_key)
            if len(items) + len(page_items) > self._max_items:
                raise ClinikoPaginationError("max_items_exceeded")
            for item in page_items:
                identifier = item.get("id")
                if isinstance(identifier, (str, int)) and not isinstance(identifier, bool):
                    canonical_id = str(identifier)
                    if canonical_id in seen_ids:
                        raise ClinikoPaginationError("duplicate_item")
                    seen_ids.add(canonical_id)
                items.append(item)

            if next_url is None:
                return tuple(items)
            safe_next = self._validate_next_url(next_url, expected_path=expected_path)
            if safe_next in seen_urls:
                raise ClinikoPaginationError("cyclic_next_link")
            if pages >= self._max_pages:
                raise ClinikoPaginationError("max_pages_exceeded")
            current_url = safe_next
            current_params = None

    def get_collection_page(
        self,
        resource: str,
        *,
        collection_key: str,
        params: Sequence[tuple[str, str]] = (),
        next_url: str | None = None,
        operation_code: str | None = None,
    ) -> ClinikoCollectionPage:
        """Fetch exactly one validated collection page without auto-pagination."""
        if _RESOURCE_PATTERN.fullmatch(resource) is None or "/" in resource:
            raise ClinikoContractError("invalid_resource")
        if _RESOURCE_PATTERN.fullmatch(collection_key) is None or "/" in collection_key:
            raise ClinikoContractError("invalid_collection_key")
        expected_path = f"/v1/{resource}"
        url = f"{self._base_url}/{resource}"
        request_params: Sequence[tuple[str, str]] | None = tuple(params)
        if next_url is not None:
            url = self._validate_next_url(next_url, expected_path=expected_path)
            request_params = None
        payload = self._get_json(
            url,
            params=request_params,
            operation_code=operation_code or resource,
        )
        page_items, discovered_next = self._collection_page(payload, collection_key)
        if len(page_items) > self._max_items:
            raise ClinikoPaginationError("max_items_exceeded")
        self._reject_duplicate_ids(page_items)
        safe_next = (
            self._validate_next_url(discovered_next, expected_path=expected_path)
            if discovered_next is not None
            else None
        )
        return ClinikoCollectionPage(
            items=tuple(page_items),
            next_url=safe_next,
            total_entries=self._total_entries(payload),
        )

    def get_item(
        self,
        resource: str,
        identifier: str,
        *,
        params: Sequence[tuple[str, str]] = (),
        operation_code: str | None = None,
    ) -> dict[str, object]:
        """Fetch one patient or individual appointment by numeric identifier."""
        if resource not in _ITEM_RESOURCES:
            raise ClinikoContractError("invalid_resource")
        if _IDENTIFIER_PATTERN.fullmatch(identifier) is None:
            raise ClinikoContractError("invalid_identifier")
        payload = self._get_json(
            f"{self._base_url}/{resource}/{identifier}",
            params=tuple(params),
            operation_code=operation_code or f"{resource}_get",
        )
        return dict(payload)

    def get_available_times_page(
        self,
        *,
        business_id: str,
        practitioner_id: str,
        appointment_type_id: str,
        from_date: date,
        to_date: date,
        next_url: str | None = None,
        operation_code: str = "available_times_read",
    ) -> ClinikoCollectionPage:
        """Fetch one documented available-times page without persisting slots."""
        identifiers = (business_id, practitioner_id, appointment_type_id)
        if any(_IDENTIFIER_PATTERN.fullmatch(value) is None for value in identifiers):
            raise ClinikoContractError("invalid_identifier")
        window_days = (to_date - from_date).days
        if not 0 <= window_days <= 7:
            raise ClinikoContractError("availability_window")
        path = (
            f"/v1/businesses/{business_id}/practitioners/{practitioner_id}"
            f"/appointment_types/{appointment_type_id}/available_times"
        )
        url = f"{self._origin.scheme}://{self._origin.netloc}{path}"
        params: Sequence[tuple[str, str]] | None = (
            ("from", from_date.isoformat()),
            ("to", to_date.isoformat()),
            ("per_page", str(self._per_page)),
        )
        if next_url is not None:
            url = self._validate_next_url(next_url, expected_path=path)
            params = None
        payload = self._get_json(
            url,
            params=params,
            operation_code=operation_code,
        )
        page_items, next_url = self._collection_page(payload, "available_times")
        if len(page_items) > self._max_items:
            raise ClinikoPaginationError("max_items_exceeded")
        safe_next = (
            self._validate_next_url(next_url, expected_path=path)
            if next_url is not None
            else None
        )
        return ClinikoCollectionPage(
            items=tuple(page_items),
            next_url=safe_next,
            total_entries=self._total_entries(payload),
        )

    def _get_json(
        self,
        url: str,
        *,
        params: Sequence[tuple[str, str]] | None,
        operation_code: str,
    ) -> Mapping[str, Any]:
        if self._attempt_observer is not None:
            self._attempt_observer(operation_code)
        self._request_budget.consume()
        self._rate_limiter.before_request()
        try:
            with self._client.stream(
                "GET",
                url,
                params=params,
                auth=self._auth,
                headers=self._headers,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                self._raise_for_status(response)
                content_type = response.headers.get("Content-Type", "")
                if content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise ClinikoContractError("content_type")
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        if int(content_length) > _MAX_RESPONSE_BYTES:
                            raise ClinikoContractError("response_too_large")
                    except ValueError:
                        raise ClinikoContractError("content_length") from None

                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise ClinikoContractError("response_too_large")
        except ClinikoError:
            raise
        except httpx.HTTPError as exc:
            raise ClinikoTransportError(_transport_kind(exc)) from None

        try:
            payload = json.loads(bytes(body))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ClinikoContractError("malformed_json") from None
        if not isinstance(payload, dict):
            raise ClinikoContractError("root_schema")
        return payload

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if 300 <= status < 400:
            raise ClinikoContractError("redirect")
        if status in {401, 403}:
            raise ClinikoAuthenticationError("authentication")
        if status == 404:
            raise ClinikoNotFoundError("not_found")
        if status == 429:
            reset_raw = response.headers.get("X-RateLimit-Reset")
            if reset_raw is None:
                raise ClinikoContractError("rate_limit_reset_missing")
            try:
                reset_at = datetime.fromtimestamp(int(reset_raw), UTC)
            except (OverflowError, TypeError, ValueError):
                raise ClinikoContractError("rate_limit_reset_invalid") from None
            raise ClinikoRateLimitedError(reset_at)
        if 400 <= status < 500:
            raise ClinikoValidationError("validation")
        if 500 <= status < 600:
            raise ClinikoServerError()

    @staticmethod
    def _collection_page(
        payload: Mapping[str, Any],
        collection_key: str,
    ) -> tuple[list[dict[str, object]], str | None]:
        raw_items = payload.get(collection_key)
        if not isinstance(raw_items, list):
            raise ClinikoContractError("missing_collection")
        items: list[dict[str, object]] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ClinikoContractError("collection_item")
            items.append(raw_item)

        ClinikoClient._total_entries(payload)
        links = payload.get("links")
        if not isinstance(links, dict):
            raise ClinikoContractError("missing_links")
        next_url = links.get("next")
        if next_url is not None and not isinstance(next_url, str):
            raise ClinikoContractError("next_link")
        return items, next_url

    @staticmethod
    def _total_entries(payload: Mapping[str, Any]) -> int:
        total_entries = payload.get("total_entries")
        if (
            not isinstance(total_entries, int)
            or isinstance(total_entries, bool)
            or total_entries < 0
        ):
            raise ClinikoContractError("total_entries")
        return total_entries

    @staticmethod
    def _reject_duplicate_ids(items: Sequence[Mapping[str, object]]) -> None:
        seen: set[str] = set()
        for item in items:
            identifier = item.get("id")
            if isinstance(identifier, (str, int)) and not isinstance(identifier, bool):
                canonical = str(identifier)
                if canonical in seen:
                    raise ClinikoPaginationError("duplicate_item")
                seen.add(canonical)

    def _validate_next_url(self, value: str, *, expected_path: str) -> str:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ClinikoPaginationError("unsafe_next_link") from None
        if (
            parsed.scheme != self._origin.scheme
            or parsed.hostname != self._origin.hostname
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.path != expected_path
            or "\\" in parsed.path
            or any(ord(character) < 32 for character in value)
        ):
            raise ClinikoPaginationError("unsafe_next_link")
        return value


def _transport_kind(error: httpx.HTTPError) -> str:
    if isinstance(error, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(error, httpx.ReadTimeout):
        return "read_timeout"
    if isinstance(error, httpx.WriteTimeout):
        return "write_timeout"
    if isinstance(error, httpx.PoolTimeout):
        return "pool_timeout"
    if isinstance(error, httpx.ConnectError):
        return "connect_error"
    if isinstance(error, httpx.ReadError):
        return "read_error"
    if isinstance(error, httpx.WriteError):
        return "write_error"
    if isinstance(error, httpx.RemoteProtocolError):
        return "protocol_error"
    return "transport_error"