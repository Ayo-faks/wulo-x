"""Closed, minimized provider adapters for durable privacy-rights effects."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import quote, urlparse

import httpx
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from .enums import RightsResidualCategory, RightsTargetResource

_SID_PATTERNS = {
    RightsTargetResource.MESSAGE: re.compile(r"(?:SM|MM)[0-9a-fA-F]{32}\Z"),
    RightsTargetResource.CALL: re.compile(r"CA[0-9a-fA-F]{32}\Z"),
    RightsTargetResource.RECORDING: re.compile(r"RE[0-9a-fA-F]{32}\Z"),
    RightsTargetResource.TRANSCRIPTION_COLLECTION: re.compile(
        r"RE[0-9a-fA-F]{32}\Z"
    ),
}
_TRANSCRIPTION_SID = re.compile(r"TR[0-9a-fA-F]{32}\Z")
_TWILIO_CALL_ARCHIVE_BOUNDARY = timedelta(days=395)
_MESSAGE_BACKUP_WINDOW = timedelta(days=30)
_RECORDING_METADATA_WINDOW = timedelta(days=40)
_MAX_RETRY_AFTER = timedelta(hours=1)
_ACTIVE_CALL_STATUSES = frozenset({"queued", "initiated", "ringing", "in-progress"})
_TERMINAL_CALL_STATUSES = frozenset(
    {"completed", "busy", "failed", "no-answer", "canceled"}
)


class RightsAdapterDisposition(StrEnum):
    """Closed outcomes from a destructive provider attempt or reconciliation."""

    DELETED = "deleted"
    ALREADY_ABSENT = "already_absent"
    RETRYABLE_KNOWN_FAILURE = "retryable_known_failure"
    AMBIGUOUS = "ambiguous"
    RESIDUAL = "residual"
    UNSUPPORTED = "unsupported"
    CONFIGURATION_BLOCKED = "configuration_blocked"


class RightsAdapterReason(StrEnum):
    """Allowlisted result reasons that retain no provider body or locator."""

    PROVIDER_DELETED = "provider_deleted"
    ALREADY_ABSENT = "already_absent"
    RESOURCE_PRESENT = "resource_present"
    RATE_LIMITED = "rate_limited"
    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORIZATION_FAILED = "authorization_failed"
    PROVIDER_SERVER_ERROR = "provider_server_error"
    TRANSPORT_ERROR = "transport_error"
    MALFORMED_RESPONSE = "malformed_response"
    INVALID_LOCATOR = "invalid_locator"
    UNSUPPORTED_RESOURCE = "unsupported_resource"
    PROVIDER_BACKUP_WINDOW = "provider_backup_window"
    PROVIDER_METADATA_WINDOW = "provider_metadata_window"
    LEGACY_ARCHIVE_PROCEDURE = "legacy_archive_procedure"
    BLOB_SOFT_DELETE_WINDOW = "blob_soft_delete_window"
    BLOB_VERSION_DELETE_FORBIDDEN = "blob_version_delete_forbidden"
    IMMUTABILITY_POLICY = "immutability_policy"


@dataclass(frozen=True)
class RightsAdapterResult:
    """Minimized evidence returned to the durable rights worker."""

    disposition: RightsAdapterDisposition
    reason: RightsAdapterReason
    retry_at: datetime | None = None
    residual_category: RightsResidualCategory | None = None
    technical_until: datetime | None = None


class RightsAdapter(Protocol):
    """Provider-neutral destructive and reconciliation contract."""

    def delete(
        self,
        *,
        resource: RightsTargetResource,
        locator: str,
        now: datetime,
        resource_created_at: datetime | None = None,
    ) -> RightsAdapterResult: ...

    def verify_absent(
        self,
        *,
        resource: RightsTargetResource,
        locator: str,
        dispatched_at: datetime,
        now: datetime,
    ) -> RightsAdapterResult: ...


class TwilioRightsAdapter:
    """Exact-SID Twilio deletion and absence-verification adapter."""

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 10.0,
        api_base_url: str = "https://api.twilio.com",
    ) -> None:
        account_sid = account_sid.strip()
        if not re.fullmatch(r"AC[0-9a-fA-F]{32}", account_sid):
            raise ValueError("Twilio account SID is invalid")
        parsed = urlparse(api_base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("Twilio API base URL must be an HTTPS origin")
        if not auth_token:
            raise ValueError("Twilio auth token is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._api_base_url = api_base_url.rstrip("/")

    def delete(
        self,
        *,
        resource: RightsTargetResource,
        locator: str,
        now: datetime,
        resource_created_at: datetime | None = None,
    ) -> RightsAdapterResult:
        now = _aware_utc(now, "now")
        if not _valid_locator(resource, locator):
            return _blocked(RightsAdapterReason.INVALID_LOCATOR)
        if resource == RightsTargetResource.CALL and resource_created_at is not None:
            created_at = _aware_utc(resource_created_at, "resource_created_at")
            if now - created_at > _TWILIO_CALL_ARCHIVE_BOUNDARY:
                return RightsAdapterResult(
                    disposition=RightsAdapterDisposition.RESIDUAL,
                    reason=RightsAdapterReason.LEGACY_ARCHIVE_PROCEDURE,
                    residual_category=RightsResidualCategory.LEGACY_ARCHIVE_PROCEDURE,
                )
        if resource == RightsTargetResource.TRANSCRIPTION_COLLECTION:
            return self._purge_transcriptions(locator, now)
        if resource == RightsTargetResource.CALL:
            return self._delete_call(locator, now)
        path = self._resource_path(resource, locator)
        if path is None:
            return _unsupported()
        response = self._request("DELETE", path)
        return _classify_twilio_delete(response, now)

    def _delete_call(self, locator: str, now: datetime) -> RightsAdapterResult:
        path = self._resource_path(RightsTargetResource.CALL, locator)
        if path is None:  # pragma: no cover - closed enum mapping
            return _unsupported()
        current = self._call_status(path, locator, now)
        if isinstance(current, RightsAdapterResult):
            return current
        if current in _ACTIVE_CALL_STATUSES:
            stopped = self._request(
                "POST",
                path,
                data={"Status": "completed"},
            )
            if stopped is None:
                return _ambiguous(RightsAdapterReason.TRANSPORT_ERROR)
            if stopped.status_code not in {200, 201}:
                return _classify_twilio_delete(stopped, now)
            reconciled = self._call_status(path, locator, now)
            if isinstance(reconciled, RightsAdapterResult):
                return reconciled
            if reconciled in _ACTIVE_CALL_STATUSES:
                return RightsAdapterResult(
                    RightsAdapterDisposition.RETRYABLE_KNOWN_FAILURE,
                    RightsAdapterReason.RESOURCE_PRESENT,
                    retry_at=now + timedelta(minutes=1),
                )
            current = reconciled
        if current not in _TERMINAL_CALL_STATUSES:
            return _ambiguous(RightsAdapterReason.MALFORMED_RESPONSE)
        return _classify_twilio_delete(self._request("DELETE", path), now)

    def _call_status(
        self,
        path: str,
        locator: str,
        now: datetime,
    ) -> str | RightsAdapterResult:
        response = self._request("GET", path)
        if response is None:
            return _ambiguous(RightsAdapterReason.TRANSPORT_ERROR)
        if response.status_code != 200:
            return _classify_twilio_get(response, now)
        try:
            payload = response.json()
        except ValueError:
            return _ambiguous(RightsAdapterReason.MALFORMED_RESPONSE)
        if not isinstance(payload, dict) or payload.get("sid") != locator:
            return _ambiguous(RightsAdapterReason.MALFORMED_RESPONSE)
        status = payload.get("status")
        if not isinstance(status, str):
            return _ambiguous(RightsAdapterReason.MALFORMED_RESPONSE)
        return status.strip().lower()

    def verify_absent(
        self,
        *,
        resource: RightsTargetResource,
        locator: str,
        dispatched_at: datetime,
        now: datetime,
    ) -> RightsAdapterResult:
        now = _aware_utc(now, "now")
        dispatched_at = _aware_utc(dispatched_at, "dispatched_at")
        if not _valid_locator(resource, locator):
            return _blocked(RightsAdapterReason.INVALID_LOCATOR)
        if resource == RightsTargetResource.RECORDING:
            return self._verify_recording(locator, dispatched_at, now)
        if resource == RightsTargetResource.TRANSCRIPTION_COLLECTION:
            return self._verify_transcriptions(locator, now)
        path = self._resource_path(resource, locator)
        if path is None:
            return _unsupported()
        response = self._request("GET", path)
        classified = _classify_twilio_get(response, now)
        if (
            resource == RightsTargetResource.MESSAGE
            and classified.disposition == RightsAdapterDisposition.ALREADY_ABSENT
        ):
            technical_until = dispatched_at + _MESSAGE_BACKUP_WINDOW
            if now < technical_until:
                return RightsAdapterResult(
                    disposition=RightsAdapterDisposition.RESIDUAL,
                    reason=RightsAdapterReason.PROVIDER_BACKUP_WINDOW,
                    residual_category=RightsResidualCategory.PROVIDER_BACKUP_WINDOW,
                    technical_until=technical_until,
                )
        return classified

    def _verify_recording(
        self,
        locator: str,
        dispatched_at: datetime,
        now: datetime,
    ) -> RightsAdapterResult:
        media = self._request(
            "GET",
            f"/2010-04-01/Accounts/{self._account_sid}/Recordings/{locator}.mp3",
        )
        metadata = self._request(
            "GET",
            f"/2010-04-01/Accounts/{self._account_sid}/Recordings/{locator}.json",
            params={"IncludeSoftDeleted": "true"},
        )
        media_result = _classify_twilio_get(media, now)
        if media_result.disposition not in {
            RightsAdapterDisposition.ALREADY_ABSENT,
            RightsAdapterDisposition.DELETED,
        }:
            return media_result
        if metadata is None:
            return _ambiguous(RightsAdapterReason.TRANSPORT_ERROR)
        if metadata.status_code == 404:
            return _absent()
        if metadata.status_code >= 500:
            return _ambiguous(RightsAdapterReason.PROVIDER_SERVER_ERROR)
        if metadata.status_code in {401, 403}:
            return _classify_twilio_delete(metadata, now)
        try:
            payload = metadata.json()
        except ValueError:
            return _ambiguous(RightsAdapterReason.MALFORMED_RESPONSE)
        if not isinstance(payload, dict) or payload.get("sid") != locator:
            return _ambiguous(RightsAdapterReason.MALFORMED_RESPONSE)
        if payload.get("status") != "deleted":
            return _ambiguous(RightsAdapterReason.RESOURCE_PRESENT)
        technical_until = dispatched_at + _RECORDING_METADATA_WINDOW
        if now < technical_until:
            return RightsAdapterResult(
                disposition=RightsAdapterDisposition.RESIDUAL,
                reason=RightsAdapterReason.PROVIDER_METADATA_WINDOW,
                residual_category=RightsResidualCategory.PROVIDER_METADATA_WINDOW,
                technical_until=technical_until,
            )
        return _absent()

    def _purge_transcriptions(self, recording_sid: str, now: datetime) -> RightsAdapterResult:
        listed = self._list_transcriptions(recording_sid, now)
        if isinstance(listed, RightsAdapterResult):
            return listed
        for transcription_sid in listed:
            response = self._request(
                "DELETE",
                f"/2010-04-01/Accounts/{self._account_sid}/Transcriptions/"
                f"{transcription_sid}.json",
            )
            result = _classify_twilio_delete(response, now)
            if result.disposition not in {
                RightsAdapterDisposition.DELETED,
                RightsAdapterDisposition.ALREADY_ABSENT,
            }:
                return result
        verified = self._verify_transcriptions(recording_sid, now)
        if verified.disposition == RightsAdapterDisposition.ALREADY_ABSENT:
            return RightsAdapterResult(
                RightsAdapterDisposition.DELETED,
                RightsAdapterReason.PROVIDER_DELETED,
            )
        return verified

    def _verify_transcriptions(
        self,
        recording_sid: str,
        now: datetime,
    ) -> RightsAdapterResult:
        listed = self._list_transcriptions(recording_sid, now)
        if isinstance(listed, RightsAdapterResult):
            return listed
        return _absent() if not listed else _ambiguous(RightsAdapterReason.RESOURCE_PRESENT)

    def _list_transcriptions(
        self,
        recording_sid: str,
        now: datetime,
    ) -> list[str] | RightsAdapterResult:
        collection_path = (
            f"/2010-04-01/Accounts/{self._account_sid}/Recordings/"
            f"{recording_sid}/Transcriptions.json"
        )
        path = collection_path
        seen_pages: set[str] = set()
        transcription_sids: list[str] = []
        seen_sids: set[str] = set()
        for _ in range(100):
            if path in seen_pages:
                return _ambiguous(RightsAdapterReason.MALFORMED_RESPONSE)
            seen_pages.add(path)
            response = self._request("GET", path)
            if response is None:
                return _ambiguous(RightsAdapterReason.TRANSPORT_ERROR)
            if response.status_code == 404:
                return []
            if response.status_code in {401, 403, 429} or response.status_code >= 500:
                return _classify_twilio_delete(response, now)
            if not 200 <= response.status_code < 300:
                return _blocked(RightsAdapterReason.INVALID_REQUEST)
            try:
                payload = response.json()
            except ValueError:
                return _ambiguous(RightsAdapterReason.MALFORMED_RESPONSE)
            rows = payload.get("transcriptions") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                return _ambiguous(RightsAdapterReason.MALFORMED_RESPONSE)
            for row in rows:
                sid = row.get("sid") if isinstance(row, dict) else None
                if not isinstance(sid, str) or not _TRANSCRIPTION_SID.fullmatch(sid):
                    return _ambiguous(RightsAdapterReason.MALFORMED_RESPONSE)
                if sid not in seen_sids:
                    seen_sids.add(sid)
                    transcription_sids.append(sid)
            next_page = payload.get("next_page_uri")
            if next_page in {None, ""}:
                return transcription_sids
            if not isinstance(next_page, str):
                return _ambiguous(RightsAdapterReason.MALFORMED_RESPONSE)
            parsed = urlparse(next_page)
            if (
                parsed.scheme
                or parsed.netloc
                or parsed.fragment
                or parsed.path != collection_path
            ):
                return _ambiguous(RightsAdapterReason.MALFORMED_RESPONSE)
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        return _ambiguous(RightsAdapterReason.MALFORMED_RESPONSE)

    def _resource_path(
        self,
        resource: RightsTargetResource,
        locator: str,
    ) -> str | None:
        plural = {
            RightsTargetResource.MESSAGE: "Messages",
            RightsTargetResource.CALL: "Calls",
            RightsTargetResource.RECORDING: "Recordings",
        }.get(resource)
        if plural is None:
            return None
        return (
            f"/2010-04-01/Accounts/{quote(self._account_sid, safe='')}/"
            f"{plural}/{quote(locator, safe='')}.json"
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response | None:
        try:
            with httpx.Client(
                base_url=self._api_base_url,
                auth=(self._account_sid, self._auth_token),
                transport=self._transport,
                timeout=self._timeout_seconds,
            ) as client:
                return client.request(method, path, params=params, data=data)
        except httpx.HTTPError:
            return None


class _BlobClient(Protocol):
    def delete_blob(self, **kwargs: Any) -> Any: ...


class _ContainerClient(Protocol):
    def list_blobs(self, *, name_starts_with: str, include: list[str]) -> Any: ...

    def get_blob_client(
        self,
        blob: str,
        *,
        snapshot: str | None = None,
        version_id: str | None = None,
    ) -> _BlobClient: ...


class AzureBlobRightsAdapter:
    """Policy-aware base/snapshot/version Blob purge and reconciliation."""

    def __init__(
        self,
        *,
        account_url: str,
        container_name: str,
        clinic_id: str,
        container_client: _ContainerClient | None = None,
        credential: Any = None,
    ) -> None:
        parsed = urlparse(account_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("Blob account URL must be an HTTPS origin")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?", container_name):
            raise ValueError("Blob container name is invalid")
        if not clinic_id.strip() or "/" in clinic_id:
            raise ValueError("clinic_id is invalid")
        self._clinic_id = clinic_id
        if container_client is not None:
            self._container = container_client
        else:
            if credential is None:
                from azure.identity import DefaultAzureCredential

                credential = DefaultAzureCredential()
            from azure.storage.blob import BlobServiceClient

            service = BlobServiceClient(account_url=account_url, credential=credential)
            self._container = service.get_container_client(container_name)

    def delete(
        self,
        *,
        resource: RightsTargetResource,
        locator: str,
        now: datetime,
        resource_created_at: datetime | None = None,
    ) -> RightsAdapterResult:
        now = _aware_utc(now, "now")
        if resource != RightsTargetResource.BLOB_COLLECTION:
            return _unsupported()
        if not self._valid_path(locator):
            return _blocked(RightsAdapterReason.INVALID_LOCATOR)
        try:
            items = self._list(locator)
        except HttpResponseError as exc:
            return _classify_blob_error(exc)
        if not items:
            return _absent()
        snapshots: set[str] = set()
        versions: set[str] = set()
        has_base = False
        for item in items:
            snapshot = getattr(item, "snapshot", None)
            version_id = getattr(item, "version_id", None)
            deleted = bool(getattr(item, "deleted", False))
            if snapshot:
                snapshots.add(str(snapshot))
            elif version_id:
                versions.add(str(version_id))
            elif not deleted:
                has_base = True
        try:
            for snapshot in sorted(snapshots):
                self._container.get_blob_client(
                    locator,
                    snapshot=snapshot,
                ).delete_blob()
            for version_id in sorted(versions):
                self._container.get_blob_client(
                    locator,
                    version_id=version_id,
                ).delete_blob()
            if has_base:
                self._container.get_blob_client(locator).delete_blob(
                    delete_snapshots="include"
                )
        except ResourceNotFoundError:
            pass
        except HttpResponseError as exc:
            return _classify_blob_error(exc)
        verified = self.verify_absent(
            resource=resource,
            locator=locator,
            dispatched_at=now,
            now=now,
        )
        if verified.disposition == RightsAdapterDisposition.ALREADY_ABSENT:
            return RightsAdapterResult(
                RightsAdapterDisposition.DELETED,
                RightsAdapterReason.PROVIDER_DELETED,
            )
        return verified

    def verify_absent(
        self,
        *,
        resource: RightsTargetResource,
        locator: str,
        dispatched_at: datetime,
        now: datetime,
    ) -> RightsAdapterResult:
        now = _aware_utc(now, "now")
        _aware_utc(dispatched_at, "dispatched_at")
        if resource != RightsTargetResource.BLOB_COLLECTION:
            return _unsupported()
        if not self._valid_path(locator):
            return _blocked(RightsAdapterReason.INVALID_LOCATOR)
        try:
            items = self._list(locator)
        except HttpResponseError as exc:
            return _classify_blob_error(exc)
        if not items:
            return _absent()
        recoverable = [item for item in items if bool(getattr(item, "deleted", False))]
        active = [item for item in items if not bool(getattr(item, "deleted", False))]
        if active:
            return RightsAdapterResult(
                RightsAdapterDisposition.RETRYABLE_KNOWN_FAILURE,
                RightsAdapterReason.RESOURCE_PRESENT,
            )
        technical_until: datetime | None = None
        for item in recoverable:
            deleted_time = getattr(item, "deleted_time", None)
            remaining_days = getattr(item, "remaining_retention_days", None)
            if not isinstance(deleted_time, datetime) or not isinstance(
                remaining_days,
                int,
            ):
                return _ambiguous(RightsAdapterReason.MALFORMED_RESPONSE)
            deleted_time = _aware_utc(deleted_time, "deleted_time")
            expiry = deleted_time + timedelta(days=remaining_days + 1)
            technical_until = max(technical_until or expiry, expiry)
        if technical_until is not None and now < technical_until:
            return RightsAdapterResult(
                disposition=RightsAdapterDisposition.RESIDUAL,
                reason=RightsAdapterReason.BLOB_SOFT_DELETE_WINDOW,
                residual_category=RightsResidualCategory.BLOB_SOFT_DELETE_WINDOW,
                technical_until=technical_until,
            )
        return _absent()

    def _list(self, locator: str) -> list[Any]:
        return [
            item
            for item in self._container.list_blobs(
                name_starts_with=locator,
                include=["deleted", "snapshots", "versions"],
            )
            if getattr(item, "name", None) == locator
        ]

    def _valid_path(self, locator: str) -> bool:
        prefix = f"{self._clinic_id}/"
        return (
            locator.startswith(prefix)
            and len(locator) > len(prefix)
            and ".." not in locator.split("/")
            and not locator.startswith("/")
            and "://" not in locator
            and "?" not in locator
            and "#" not in locator
        )


def _classify_twilio_delete(
    response: httpx.Response | None,
    now: datetime,
) -> RightsAdapterResult:
    if response is None:
        return _ambiguous(RightsAdapterReason.TRANSPORT_ERROR)
    status = response.status_code
    if status == 204:
        return RightsAdapterResult(
            RightsAdapterDisposition.DELETED,
            RightsAdapterReason.PROVIDER_DELETED,
        )
    if status == 404:
        return _absent()
    if status == 429:
        raw = response.headers.get("Retry-After", "")
        try:
            seconds = max(1, int(raw))
        except ValueError:
            seconds = 60
        retry_at = now + min(timedelta(seconds=seconds), _MAX_RETRY_AFTER)
        return RightsAdapterResult(
            RightsAdapterDisposition.RETRYABLE_KNOWN_FAILURE,
            RightsAdapterReason.RATE_LIMITED,
            retry_at=retry_at,
        )
    if status == 400:
        return _blocked(RightsAdapterReason.INVALID_REQUEST)
    if status == 401:
        return _blocked(RightsAdapterReason.AUTHENTICATION_FAILED)
    if status == 403:
        return _blocked(RightsAdapterReason.AUTHORIZATION_FAILED)
    if status >= 500:
        return _ambiguous(RightsAdapterReason.PROVIDER_SERVER_ERROR)
    return _ambiguous(RightsAdapterReason.MALFORMED_RESPONSE)


def _classify_twilio_get(
    response: httpx.Response | None,
    now: datetime,
) -> RightsAdapterResult:
    if response is None:
        return _ambiguous(RightsAdapterReason.TRANSPORT_ERROR)
    if response.status_code == 404:
        return _absent()
    if response.status_code == 200:
        return RightsAdapterResult(
            RightsAdapterDisposition.RETRYABLE_KNOWN_FAILURE,
            RightsAdapterReason.RESOURCE_PRESENT,
        )
    return _classify_twilio_delete(response, now)


def _classify_blob_error(exc: HttpResponseError) -> RightsAdapterResult:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    error_code = str(getattr(exc, "error_code", "") or "").lower()
    if status == 404:
        return _absent()
    if status == 403:
        return _blocked(RightsAdapterReason.BLOB_VERSION_DELETE_FORBIDDEN)
    if status == 409 or "immutab" in error_code or "legalhold" in error_code:
        return RightsAdapterResult(
            RightsAdapterDisposition.RESIDUAL,
            RightsAdapterReason.IMMUTABILITY_POLICY,
            residual_category=RightsResidualCategory.LEGAL_OR_IMMUTABILITY_HOLD,
        )
    if status == 429:
        return RightsAdapterResult(
            RightsAdapterDisposition.RETRYABLE_KNOWN_FAILURE,
            RightsAdapterReason.RATE_LIMITED,
        )
    if status is not None and status >= 500:
        return _ambiguous(RightsAdapterReason.PROVIDER_SERVER_ERROR)
    return _ambiguous(RightsAdapterReason.TRANSPORT_ERROR)


def _valid_locator(resource: RightsTargetResource, locator: str) -> bool:
    pattern = _SID_PATTERNS.get(resource)
    return bool(pattern and pattern.fullmatch(locator))


def _blocked(reason: RightsAdapterReason) -> RightsAdapterResult:
    return RightsAdapterResult(RightsAdapterDisposition.CONFIGURATION_BLOCKED, reason)


def _unsupported() -> RightsAdapterResult:
    return RightsAdapterResult(
        RightsAdapterDisposition.UNSUPPORTED,
        RightsAdapterReason.UNSUPPORTED_RESOURCE,
    )


def _ambiguous(reason: RightsAdapterReason) -> RightsAdapterResult:
    return RightsAdapterResult(RightsAdapterDisposition.AMBIGUOUS, reason)


def _absent() -> RightsAdapterResult:
    return RightsAdapterResult(
        RightsAdapterDisposition.ALREADY_ABSENT,
        RightsAdapterReason.ALREADY_ABSENT,
    )


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)