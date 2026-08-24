"""Exact documented Cliniko booking client and DTO contracts."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from src.clinic_recall.config import ClinikoConfig
from src.clinic_recall.sync.cliniko_booking import (
    ClinikoBookingClient,
    ExpectedAppointmentSignature,
)
from src.clinic_recall.sync.cliniko_client import (
    ClinikoAuthenticationError,
    ClinikoContractError,
    ClinikoRateLimitedError,
    ClinikoServerError,
    ClinikoTransportError,
    ClinikoValidationError,
)

FIXTURES = Path("tests/fixtures/cliniko/pr07")
FIXTURE_HASHES = {
    "create_request.json": "ca839edb8cc57a13ba079c197399cc76780dbb237c005fa99f76500d70f98dde",
    "create_response.json": "33679b0a659b20ba65dca655286d960d8d10554b3237aa8aa63d8d2d09362906",
    "error_cases.json": "274fe1e13ae54bcc5752c3d589e72921f622f4102c774f2e1a225fee7469a33b",
    "malformed_cases.json": "b270ea6cdafc93baf65ef85e63712786bd9aebe385c3f11dd817d8a16f29d680",
    "manifest.json": "3ccc2971f4646447c73a554471686e29a5b46c876e2db1a9e4e19a9d77f8cd4a",
    "read_back_variants.json": "5d7f44d6510f91d70e9f2a50ed96f68e70bcda05f3be0e5e6f76529d348dc73d",
    "reconciliation_multiple.json": "9cdb3e10dba6348e1a79afd85c976ded5cc2bb390be44d964981815eef84f3df",
    "reconciliation_one.json": "b1f62f660942820679fe652478e80de02942c4c87d37d12122bc66671c6c2a56",
    "reconciliation_zero.json": "cd06b582ce49c50f1a7bcd28f4f093057fa6811d9230ce46af034953bd69a290",
    "reschedule_request.json": "1213e078aec8f36efb3135e12bcd4ab1d16908c2af33355f0ff71717d3f4f8e0",
    "reschedule_response.json": "4375228696c98bb3c906ca426fb5043efa8b148fe013b2320769aec7da70aebf",
}


def _json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _expected() -> ExpectedAppointmentSignature:
    return ExpectedAppointmentSignature(
        patient_id="900700001",
        business_id="920700001",
        practitioner_id="930700001",
        appointment_type_id="940700001",
        starts_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
        ends_at=datetime(2026, 8, 4, 9, 30, tzinfo=UTC),
    )


def test_pr07_fixture_bundle_is_sealed_synthetic_and_contact_free() -> None:
    assert {path.name for path in FIXTURES.iterdir()} == set(FIXTURE_HASHES)
    for filename, expected_hash in FIXTURE_HASHES.items():
        assert hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest() == (
            expected_hash
        )
    manifest = _json("manifest.json")
    assert manifest["evidence_authority"] == "synthetic_contract_verified"
    assert manifest["contains_provider_data"] is False
    assert manifest["contains_real_patient_data"] is False
    assert manifest["contains_patient_contact_data"] is False
    assert manifest["contains_clinical_content"] is False


def _client(handler) -> ClinikoBookingClient:
    return ClinikoBookingClient(
        ClinikoConfig(
            enabled=True,
            api_key="synthetic-client-key-uk2",
            shard="uk2",
            user_agent="Wulo Synthetic Tests (engineering@example.test)",
            timeout_seconds=2.0,
            per_page=100,
            max_pages=2,
            max_items=10,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_create_uses_exact_method_path_body_auth_and_user_agent() -> None:
    expected_request = _json("create_request.json")
    response_payload = _json("create_response.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://api.uk2.cliniko.com/v1/individual_appointments"
        assert json.loads(request.content) == expected_request
        assert request.headers["Authorization"].startswith("Basic ")
        assert request.headers["User-Agent"] == (
            "Wulo Synthetic Tests (engineering@example.test)"
        )
        return httpx.Response(201, request=request, json=response_payload)

    observed = _client(handler).create_individual_appointment(_expected())

    assert observed.matches(_expected()) is True
    assert "900700001" not in repr(_expected())
    assert "950700001" not in repr(observed)


def test_signature_query_uses_only_documented_exact_superset_filters() -> None:
    payload = _json("reconciliation_one.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/individual_appointments"
        assert request.url.params.get_list("q[]") == [
            "appointment_type_id:=940700001",
            "business_id:=920700001",
            "patient_id:=900700001",
            "practitioner_id:=930700001",
            "starts_at:>=2026-08-04T09:00:00Z",
            "starts_at:<=2026-08-04T09:00:00Z",
            "ends_at:>=2026-08-04T09:30:00Z",
            "ends_at:<=2026-08-04T09:30:00Z",
        ]
        return httpx.Response(200, request=request, json=payload)

    candidates = _client(handler).list_signature_candidates(_expected())

    assert len(candidates) == 1
    assert candidates[0].matches(_expected()) is True


def test_exact_availability_preflight_uses_binding_path_and_strict_start() -> None:
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "available_times": [
                    {"appointment_start": "2026-08-04T09:00:00Z"}
                ],
                "total_entries": 1,
                "links": {},
            },
        )

    assert _client(handler).exact_slot_is_available(_expected()) is True
    assert len(requested) == 1
    assert requested[0].url.path == (
        "/v1/businesses/920700001/practitioners/930700001/"
        "appointment_types/940700001/available_times"
    )
    assert dict(requested[0].url.params) == {
        "from": "2026-08-04",
        "to": "2026-08-04",
        "per_page": "100",
    }


def test_availability_preflight_rejects_duplicate_or_untrusted_items() -> None:
    duplicate = {
        "available_times": [
            {"appointment_start": "2026-08-04T09:00:00Z"},
            {"appointment_start": "2026-08-04T09:00:00Z"},
        ],
        "total_entries": 2,
        "links": {},
    }

    with pytest.raises(ClinikoContractError, match="duplicate_available_time"):
        _client(
            lambda request: httpx.Response(200, request=request, json=duplicate)
        ).exact_slot_is_available(_expected())

    extra_field = {
        "available_times": [
            {
                "appointment_start": "2026-08-04T09:00:00Z",
                "provider_text": "untrusted",
            }
        ],
        "total_entries": 1,
        "links": {},
    }
    with pytest.raises(ClinikoContractError, match="available_time_schema"):
        _client(
            lambda request: httpx.Response(200, request=request, json=extra_field)
        ).exact_slot_is_available(_expected())


@pytest.mark.parametrize("variant", sorted(_json("read_back_variants.json")))
def test_every_consequential_read_back_mismatch_is_rejected(variant: str) -> None:
    payload = _json("create_response.json")
    patch = _json("read_back_variants.json")[variant]
    target = payload
    parts = patch["path"].split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = patch["value"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=payload)

    observed = _client(handler).get_individual_appointment("950700001")
    assert observed.matches(_expected()) is False


@pytest.mark.parametrize("field_name", ["cancelled_at", "archived_at", "deleted_at"])
def test_missing_lifecycle_field_fails_closed(field_name: str) -> None:
    payload = copy.deepcopy(_json("create_response.json"))
    del payload[field_name]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=payload)

    with pytest.raises(ClinikoContractError, match="appointment_lifecycle"):
        _client(handler).get_individual_appointment("950700001")


@pytest.mark.parametrize(
    ("case", "error_type"),
    [
        ("validation", ClinikoValidationError),
        ("authentication", ClinikoAuthenticationError),
        ("permission", ClinikoAuthenticationError),
        ("conflict", ClinikoValidationError),
        ("server_error", ClinikoServerError),
    ],
)
def test_create_returns_only_typed_minimized_errors(case: str, error_type) -> None:
    scenario = _json("error_cases.json")[case]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(scenario["status"], request=request, json=scenario["body"])

    with pytest.raises(error_type) as caught:
        _client(handler).create_individual_appointment(_expected())
    assert "synthetic invalid value" not in str(caught.value)


def test_rate_limit_requires_documented_reset() -> None:
    scenario = _json("error_cases.json")["rate_limited"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            scenario["status"],
            request=request,
            headers=scenario["headers"],
            json=scenario["body"],
        )

    with pytest.raises(ClinikoRateLimitedError) as caught:
        _client(handler).create_individual_appointment(_expected())
    assert caught.value.reset_at.tzinfo is UTC


def test_transport_and_malformed_success_are_bounded() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private synthetic detail", request=request)

    with pytest.raises(ClinikoTransportError, match="read_timeout") as caught:
        _client(timeout).create_individual_appointment(_expected())
    assert "private" not in str(caught.value)

    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            request=request,
            headers={"Content-Type": "application/json"},
            content=b"not-json",
        )

    with pytest.raises(ClinikoContractError, match="malformed_json"):
        _client(malformed).create_individual_appointment(_expected())


def test_off_origin_link_and_naive_datetime_fail_closed() -> None:
    payload = copy.deepcopy(_json("create_response.json"))
    payload["patient"]["links"]["self"] = (
        "https://example.test/v1/patients/900700001"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=payload)

    with pytest.raises(ClinikoContractError, match="appointment_link"):
        _client(handler).get_individual_appointment("950700001")
    with pytest.raises(ClinikoContractError, match="appointment_starts_at"):
        ExpectedAppointmentSignature(
            patient_id="900700001",
            business_id="920700001",
            practitioner_id="930700001",
            appointment_type_id="940700001",
            starts_at=datetime(2026, 8, 4, 9, 0),
            ends_at=datetime(2026, 8, 4, 9, 30, tzinfo=UTC),
        )


def test_client_exposes_no_update_or_arbitrary_request_surface() -> None:
    client = _client(lambda request: httpx.Response(500, request=request))
    assert not hasattr(client, "update_individual_appointment")
    assert not hasattr(client, "request")