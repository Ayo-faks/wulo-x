"""Closed provider adapter contracts for PR-10 rights deletion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from azure.core.exceptions import HttpResponseError
from src.clinic_recall.enums import (
    RightsResidualCategory,
    RightsTargetResource,
)
from src.clinic_recall.rights_adapters import (
    AzureBlobRightsAdapter,
    RightsAdapterDisposition,
    RightsAdapterReason,
    TwilioRightsAdapter,
)

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
ACCOUNT_SID = "AC" + "a" * 32
MESSAGE_SID = "SM" + "1" * 32
CALL_SID = "CA" + "2" * 32
RECORDING_SID = "RE" + "3" * 32
TRANSCRIPTION_SID = "TR" + "4" * 32


def _twilio(handler) -> TwilioRightsAdapter:
    return TwilioRightsAdapter(
        account_sid=ACCOUNT_SID,
        auth_token="tests-only-token",
        transport=httpx.MockTransport(handler),
        timeout_seconds=1,
        api_base_url="https://api.twilio.test",
    )


@pytest.mark.parametrize(
    ("status", "expected", "reason"),
    [
        (204, RightsAdapterDisposition.DELETED, RightsAdapterReason.PROVIDER_DELETED),
        (404, RightsAdapterDisposition.ALREADY_ABSENT, RightsAdapterReason.ALREADY_ABSENT),
        (400, RightsAdapterDisposition.CONFIGURATION_BLOCKED, RightsAdapterReason.INVALID_REQUEST),
        (401, RightsAdapterDisposition.CONFIGURATION_BLOCKED, RightsAdapterReason.AUTHENTICATION_FAILED),
        (403, RightsAdapterDisposition.CONFIGURATION_BLOCKED, RightsAdapterReason.AUTHORIZATION_FAILED),
        (500, RightsAdapterDisposition.AMBIGUOUS, RightsAdapterReason.PROVIDER_SERVER_ERROR),
    ],
)
def test_twilio_message_delete_has_closed_http_outcomes(status, expected, reason) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, request=request)

    result = _twilio(handler).delete(
        resource=RightsTargetResource.MESSAGE,
        locator=MESSAGE_SID,
        now=NOW,
    )

    assert result.disposition == expected
    assert result.reason == reason
    assert len(seen) == 1
    assert seen[0].method == "DELETE"
    assert seen[0].url.path == (
        f"/2010-04-01/Accounts/{ACCOUNT_SID}/Messages/{MESSAGE_SID}.json"
    )


def test_twilio_429_is_known_retryable_and_bounds_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "6000"}, request=request)

    result = _twilio(handler).delete(
        resource=RightsTargetResource.CALL,
        locator=CALL_SID,
        now=NOW,
    )

    assert result.disposition == RightsAdapterDisposition.RETRYABLE_KNOWN_FAILURE
    assert result.reason == RightsAdapterReason.RATE_LIMITED
    assert result.retry_at == NOW + timedelta(hours=1)


def test_twilio_timeout_is_ambiguous_and_malformed_locator_never_dispatches() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("tests-only timeout", request=request)

    adapter = _twilio(handler)
    timeout = adapter.delete(
        resource=RightsTargetResource.RECORDING,
        locator=RECORDING_SID,
        now=NOW,
    )
    malformed = adapter.delete(
        resource=RightsTargetResource.RECORDING,
        locator="https://attacker.invalid/recording",
        now=NOW,
    )

    assert timeout.disposition == RightsAdapterDisposition.AMBIGUOUS
    assert timeout.reason == RightsAdapterReason.TRANSPORT_ERROR
    assert malformed.disposition == RightsAdapterDisposition.CONFIGURATION_BLOCKED
    assert malformed.reason == RightsAdapterReason.INVALID_LOCATOR
    assert calls == 1


def test_twilio_message_verification_records_documented_backup_window() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    result = _twilio(handler).verify_absent(
        resource=RightsTargetResource.MESSAGE,
        locator=MESSAGE_SID,
        dispatched_at=NOW,
        now=NOW,
    )

    assert result.disposition == RightsAdapterDisposition.RESIDUAL
    assert result.residual_category == RightsResidualCategory.PROVIDER_BACKUP_WINDOW
    assert result.technical_until == NOW + timedelta(days=30)


def test_twilio_recording_verification_separates_media_and_metadata_residual() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith(".mp3"):
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            json={"sid": RECORDING_SID, "status": "deleted"},
            request=request,
        )

    result = _twilio(handler).verify_absent(
        resource=RightsTargetResource.RECORDING,
        locator=RECORDING_SID,
        dispatched_at=NOW,
        now=NOW,
    )

    assert result.disposition == RightsAdapterDisposition.RESIDUAL
    assert result.residual_category == RightsResidualCategory.PROVIDER_METADATA_WINDOW
    assert result.technical_until == NOW + timedelta(days=40)
    assert any(path.endswith(f"/{RECORDING_SID}.mp3") for path in seen)
    assert any(path.endswith(f"/{RECORDING_SID}.json") for path in seen)


def test_twilio_transcriptions_are_enumerated_deleted_and_verified_independently() -> None:
    list_calls = 0
    delete_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_calls
        if request.method == "GET":
            list_calls += 1
            payload = (
                {"transcriptions": [{"sid": TRANSCRIPTION_SID}], "next_page_uri": None}
                if list_calls == 1
                else {"transcriptions": [], "next_page_uri": None}
            )
            return httpx.Response(200, json=payload, request=request)
        delete_paths.append(request.url.path)
        return httpx.Response(204, request=request)

    result = _twilio(handler).delete(
        resource=RightsTargetResource.TRANSCRIPTION_COLLECTION,
        locator=RECORDING_SID,
        now=NOW,
    )

    assert result.disposition == RightsAdapterDisposition.DELETED
    assert list_calls == 2
    assert delete_paths == [
        f"/2010-04-01/Accounts/{ACCOUNT_SID}/Transcriptions/{TRANSCRIPTION_SID}.json"
    ]


def test_twilio_transcription_rate_limit_uses_injected_clock() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "15"}, request=request)

    result = _twilio(handler).delete(
        resource=RightsTargetResource.TRANSCRIPTION_COLLECTION,
        locator=RECORDING_SID,
        now=NOW,
    )

    assert result.disposition == RightsAdapterDisposition.RETRYABLE_KNOWN_FAILURE
    assert result.reason == RightsAdapterReason.RATE_LIMITED
    assert result.retry_at == NOW + timedelta(seconds=15)


def test_twilio_transcription_pagination_is_bounded_and_same_resource() -> None:
    second_sid = "TR" + "5" * 32
    listed_paths: list[str] = []
    deleted: list[str] = []
    verification = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal verification
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(204, request=request)
        listed_paths.append(str(request.url))
        if verification:
            return httpx.Response(
                200,
                json={"transcriptions": [], "next_page_uri": None},
                request=request,
            )
        if "PageToken=next" in str(request.url):
            verification = True
            return httpx.Response(
                200,
                json={"transcriptions": [{"sid": second_sid}], "next_page_uri": None},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "transcriptions": [{"sid": TRANSCRIPTION_SID}],
                "next_page_uri": (
                    f"/2010-04-01/Accounts/{ACCOUNT_SID}/Recordings/"
                    f"{RECORDING_SID}/Transcriptions.json?PageToken=next"
                ),
            },
            request=request,
        )

    result = _twilio(handler).delete(
        resource=RightsTargetResource.TRANSCRIPTION_COLLECTION,
        locator=RECORDING_SID,
        now=NOW,
    )

    assert result.disposition == RightsAdapterDisposition.DELETED
    assert any("PageToken=next" in path for path in listed_paths)
    assert set(deleted) == {
        f"/2010-04-01/Accounts/{ACCOUNT_SID}/Transcriptions/{TRANSCRIPTION_SID}.json",
        f"/2010-04-01/Accounts/{ACCOUNT_SID}/Transcriptions/{second_sid}.json",
    }


def test_twilio_transcription_pagination_rejects_external_url() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "transcriptions": [{"sid": TRANSCRIPTION_SID}],
                "next_page_uri": "https://attacker.invalid/transcriptions",
            },
            request=request,
        )

    result = _twilio(handler).delete(
        resource=RightsTargetResource.TRANSCRIPTION_COLLECTION,
        locator=RECORDING_SID,
        now=NOW,
    )

    assert result.disposition == RightsAdapterDisposition.AMBIGUOUS
    assert result.reason == RightsAdapterReason.MALFORMED_RESPONSE
    assert calls == 1


def test_twilio_old_call_requires_archive_procedure_without_delete() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(204, request=request)

    result = _twilio(handler).delete(
        resource=RightsTargetResource.CALL,
        locator=CALL_SID,
        resource_created_at=NOW - timedelta(days=400),
        now=NOW,
    )

    assert result.disposition == RightsAdapterDisposition.RESIDUAL
    assert result.residual_category == RightsResidualCategory.LEGACY_ARCHIVE_PROCEDURE
    assert calls == 0


def test_twilio_active_call_is_ended_reconciled_then_deleted() -> None:
    requests: list[tuple[str, str, bytes]] = []
    call_gets = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_gets
        requests.append((request.method, request.url.path, request.content))
        if request.method == "GET":
            call_gets += 1
            status = "in-progress" if call_gets == 1 else "completed"
            return httpx.Response(
                200,
                json={"sid": CALL_SID, "status": status},
                request=request,
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"sid": CALL_SID, "status": "completed"},
                request=request,
            )
        return httpx.Response(204, request=request)

    result = _twilio(handler).delete(
        resource=RightsTargetResource.CALL,
        locator=CALL_SID,
        now=NOW,
    )

    assert result.disposition == RightsAdapterDisposition.DELETED
    assert [method for method, _, _ in requests] == ["GET", "POST", "GET", "DELETE"]
    assert requests[1][2] == b"Status=completed"


def test_twilio_uncertain_active_call_termination_never_deletes() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"sid": CALL_SID, "status": "in-progress"},
                request=request,
            )
        raise httpx.ReadTimeout("tests-only stop timeout", request=request)

    result = _twilio(handler).delete(
        resource=RightsTargetResource.CALL,
        locator=CALL_SID,
        now=NOW,
    )

    assert result.disposition == RightsAdapterDisposition.AMBIGUOUS
    assert result.reason == RightsAdapterReason.TRANSPORT_ERROR
    assert methods == ["GET", "POST"]


@dataclass
class _BlobItem:
    name: str
    snapshot: str | None = None
    version_id: str | None = None
    deleted: bool = False
    deleted_time: datetime | None = None
    remaining_retention_days: int | None = None


class _BlobClient:
    def __init__(self, container: _Container, name: str, snapshot=None, version_id=None):
        self.container = container
        self.name = name
        self.snapshot = snapshot
        self.version_id = version_id

    def delete_blob(self, **kwargs):
        self.container.deletes.append(
            (self.name, self.snapshot, self.version_id, kwargs.get("delete_snapshots"))
        )
        error = self.container.errors.get((self.snapshot, self.version_id))
        if error is not None:
            raise error


class _Container:
    def __init__(self, listings: list[list[_BlobItem]]):
        self.listings = list(listings)
        self.deletes: list[tuple[str, str | None, str | None, str | None]] = []
        self.errors: dict[tuple[str | None, str | None], Exception] = {}
        self.includes: list[tuple[str, ...]] = []

    def list_blobs(self, *, name_starts_with: str, include):
        self.includes.append(tuple(include))
        assert name_starts_with == "clinic-a/calls/recording.mp3"
        return self.listings.pop(0)

    def get_blob_client(self, blob: str, *, snapshot=None, version_id=None):
        return _BlobClient(self, blob, snapshot=snapshot, version_id=version_id)


def _blob(container: _Container) -> AzureBlobRightsAdapter:
    return AzureBlobRightsAdapter(
        account_url="https://storage.test.blob.core.windows.net",
        container_name="call-recordings",
        clinic_id="clinic-a",
        container_client=container,
    )


def test_blob_purge_deletes_base_snapshots_versions_and_verifies_empty() -> None:
    path = "clinic-a/calls/recording.mp3"
    container = _Container(
        [
            [
                _BlobItem(path),
                _BlobItem(path, snapshot="snapshot-1"),
                _BlobItem(path, version_id="version-1"),
            ],
            [],
        ]
    )

    result = _blob(container).delete(
        resource=RightsTargetResource.BLOB_COLLECTION,
        locator=path,
        now=NOW,
    )

    assert result.disposition == RightsAdapterDisposition.DELETED
    assert (path, None, None, "include") in container.deletes
    assert (path, "snapshot-1", None, None) in container.deletes
    assert (path, None, "version-1", None) in container.deletes
    assert all(set(include) == {"deleted", "snapshots", "versions"} for include in container.includes)


def test_blob_soft_deleted_data_is_a_recoverable_residual() -> None:
    path = "clinic-a/calls/recording.mp3"
    deleted_at = NOW - timedelta(days=1)
    container = _Container(
        [[_BlobItem(path, deleted=True, deleted_time=deleted_at, remaining_retention_days=6)]]
    )

    result = _blob(container).verify_absent(
        resource=RightsTargetResource.BLOB_COLLECTION,
        locator=path,
        dispatched_at=NOW - timedelta(days=1),
        now=NOW,
    )

    assert result.disposition == RightsAdapterDisposition.RESIDUAL
    assert result.residual_category == RightsResidualCategory.BLOB_SOFT_DELETE_WINDOW
    assert result.technical_until == deleted_at + timedelta(days=7)


@pytest.mark.parametrize(
    ("status_code", "expected", "reason", "category"),
    [
        (
            403,
            RightsAdapterDisposition.CONFIGURATION_BLOCKED,
            RightsAdapterReason.BLOB_VERSION_DELETE_FORBIDDEN,
            None,
        ),
        (
            409,
            RightsAdapterDisposition.RESIDUAL,
            RightsAdapterReason.IMMUTABILITY_POLICY,
            RightsResidualCategory.LEGAL_OR_IMMUTABILITY_HOLD,
        ),
    ],
)
def test_blob_version_rbac_and_immutability_fail_closed(status_code, expected, reason, category) -> None:
    path = "clinic-a/calls/recording.mp3"
    container = _Container([[_BlobItem(path, version_id="version-1")]])
    error = HttpResponseError("tests-only Blob failure")
    error.status_code = status_code
    container.errors[(None, "version-1")] = error

    result = _blob(container).delete(
        resource=RightsTargetResource.BLOB_COLLECTION,
        locator=path,
        now=NOW,
    )

    assert result.disposition == expected
    assert result.reason == reason
    assert result.residual_category == category


def test_blob_path_is_clinic_bound_and_never_passes_an_attacker_url() -> None:
    container = _Container([])

    result = _blob(container).delete(
        resource=RightsTargetResource.BLOB_COLLECTION,
        locator="https://attacker.invalid/clinic-a/recording.mp3",
        now=NOW,
    )

    assert result.disposition == RightsAdapterDisposition.CONFIGURATION_BLOCKED
    assert result.reason == RightsAdapterReason.INVALID_LOCATOR
    assert container.includes == []


def test_blob_listing_ignores_prefix_siblings_and_rejects_empty_clinic_path() -> None:
    path = "clinic-a/calls/recording.mp3"
    container = _Container(
        [[_BlobItem(f"{path}.unexpected", version_id="sibling-version")]]
    )
    adapter = _blob(container)

    absent = adapter.delete(
        resource=RightsTargetResource.BLOB_COLLECTION,
        locator=path,
        now=NOW,
    )
    empty = adapter.delete(
        resource=RightsTargetResource.BLOB_COLLECTION,
        locator="clinic-a/",
        now=NOW,
    )

    assert absent.disposition == RightsAdapterDisposition.ALREADY_ABSENT
    assert container.deletes == []
    assert empty.disposition == RightsAdapterDisposition.CONFIGURATION_BLOCKED