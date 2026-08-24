import base64
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from src.clinic_recall import sync as sync_package
from src.clinic_recall.config import (
    ClinikoConfig,
    ClinikoConfigurationError,
    build_cliniko_config,
    get_cliniko_config,
)
from src.clinic_recall.enums import AppointmentStatus
from src.clinic_recall.sync.base import SyncSource
from src.clinic_recall.sync.cliniko_adapter import (
    ClinikoAppointmentRecord,
    ClinikoPatientRecord,
    ClinikoSyncQuery,
    materialize_cliniko_source,
)
from src.clinic_recall.sync.cliniko_client import (
    ClinikoAuthenticationError,
    ClinikoClient,
    ClinikoContractError,
    ClinikoNotFoundError,
    ClinikoPaginationError,
    ClinikoRateLimitedError,
    ClinikoRateLimiter,
    ClinikoRequestBudget,
    ClinikoServerError,
    ClinikoTransportError,
    ClinikoValidationError,
)

_CLINIKO_ENVIRONMENT_NAMES = (
    "CLINIC_RECALL_CLINIKO_SYNC_ENABLED",
    "CLINIC_RECALL_CLINIKO_API_KEY",
    "CLINIC_RECALL_CLINIKO_SHARD",
    "CLINIC_RECALL_CLINIKO_USER_AGENT",
    "CLINIC_RECALL_CLINIKO_TIMEOUT_SECONDS",
    "CLINIC_RECALL_CLINIKO_PER_PAGE",
    "CLINIC_RECALL_CLINIKO_MAX_PAGES",
    "CLINIC_RECALL_CLINIKO_MAX_ITEMS",
)


def _clear_cliniko_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _CLINIKO_ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)


def _enable_cliniko(monkeypatch: pytest.MonkeyPatch, *, shard: str = "uk2") -> str:
    api_key = f"synthetic-cliniko-api-key-{shard}"
    monkeypatch.setenv("CLINIC_RECALL_CLINIKO_SYNC_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_CLINIKO_API_KEY", api_key)
    monkeypatch.setenv("CLINIC_RECALL_CLINIKO_SHARD", shard)
    monkeypatch.setenv(
        "CLINIC_RECALL_CLINIKO_USER_AGENT",
        "Clinic Recall Tests (cliniko-tests@example.invalid)",
    )
    return api_key


def test_cliniko_config_defaults_off_for_missing_or_unknown_switch(monkeypatch) -> None:
    _clear_cliniko_environment(monkeypatch)

    assert get_cliniko_config().enabled is False

    monkeypatch.setenv("CLINIC_RECALL_CLINIKO_SYNC_ENABLED", "unknown")
    assert get_cliniko_config().enabled is False


def test_cliniko_config_enabled_requires_api_key_without_exposing_it(monkeypatch) -> None:
    _clear_cliniko_environment(monkeypatch)
    monkeypatch.setenv("CLINIC_RECALL_CLINIKO_SYNC_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_CLINIKO_SHARD", "uk1")
    monkeypatch.setenv(
        "CLINIC_RECALL_CLINIKO_USER_AGENT",
        "Clinic Recall Tests (cliniko-tests@example.invalid)",
    )

    with pytest.raises(
        ClinikoConfigurationError,
        match="^CLINIC_RECALL_CLINIKO_API_KEY$",
    ):
        get_cliniko_config()


@pytest.mark.parametrize("shard", ["uk1", "uk2", "uk3"])
def test_cliniko_config_derives_exact_uk_origin_and_hides_secret(
    monkeypatch,
    shard: str,
) -> None:
    _clear_cliniko_environment(monkeypatch)
    api_key = _enable_cliniko(monkeypatch, shard=shard)

    config = get_cliniko_config()

    assert config.enabled is True
    assert config.base_url == f"https://api.{shard}.cliniko.com/v1"
    assert api_key not in repr(config)


@pytest.mark.parametrize("shard", ["au1", "eu1", "uk4", "UK1", "https://api.uk1.cliniko.com"])
def test_cliniko_config_rejects_non_allowlisted_shard(monkeypatch, shard: str) -> None:
    _clear_cliniko_environment(monkeypatch)
    api_key = _enable_cliniko(monkeypatch, shard=shard)

    with pytest.raises(ClinikoConfigurationError) as exc_info:
        get_cliniko_config()

    assert str(exc_info.value) == "CLINIC_RECALL_CLINIKO_SHARD"
    assert api_key not in str(exc_info.value)
    assert api_key not in repr(exc_info.value)


@pytest.mark.parametrize(
    "api_key",
    ["synthetic-suffixless-key", "synthetic-key-uk1", "synthetic-key-eu1"],
)
def test_cliniko_config_rejects_suffixless_or_mismatched_key(
    monkeypatch,
    api_key: str,
) -> None:
    _clear_cliniko_environment(monkeypatch)
    _enable_cliniko(monkeypatch, shard="uk2")
    monkeypatch.setenv("CLINIC_RECALL_CLINIKO_API_KEY", api_key)

    with pytest.raises(ClinikoConfigurationError) as exc_info:
        get_cliniko_config()

    assert str(exc_info.value) == "CLINIC_RECALL_CLINIKO_API_KEY"
    assert api_key not in repr(exc_info.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CLINIC_RECALL_CLINIKO_USER_AGENT", "Clinic Recall"),
        ("CLINIC_RECALL_CLINIKO_TIMEOUT_SECONDS", "0"),
        ("CLINIC_RECALL_CLINIKO_TIMEOUT_SECONDS", "31"),
        ("CLINIC_RECALL_CLINIKO_PER_PAGE", "0"),
        ("CLINIC_RECALL_CLINIKO_PER_PAGE", "101"),
        ("CLINIC_RECALL_CLINIKO_MAX_PAGES", "0"),
        ("CLINIC_RECALL_CLINIKO_MAX_ITEMS", "0"),
        ("CLINIC_RECALL_CLINIKO_MAX_ITEMS", "not-a-number"),
    ],
)
def test_cliniko_config_rejects_invalid_required_or_bounded_setting(
    monkeypatch,
    name: str,
    value: str,
) -> None:
    _clear_cliniko_environment(monkeypatch)
    api_key = _enable_cliniko(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(ClinikoConfigurationError) as exc_info:
        get_cliniko_config()

    assert str(exc_info.value) == name
    assert api_key not in repr(exc_info.value)


@pytest.mark.parametrize(
    ("field", "value", "expected_name"),
    [
        ("api_key", 1, "CLINIC_RECALL_CLINIKO_API_KEY"),
        ("shard", ["uk2"], "CLINIC_RECALL_CLINIKO_SHARD"),
        ("user_agent", 1, "CLINIC_RECALL_CLINIKO_USER_AGENT"),
        ("timeout_seconds", True, "CLINIC_RECALL_CLINIKO_TIMEOUT_SECONDS"),
        ("per_page", True, "CLINIC_RECALL_CLINIKO_PER_PAGE"),
    ],
)
def test_explicit_cliniko_config_rejects_hostile_input_types(
    field: str,
    value: object,
    expected_name: str,
) -> None:
    inputs: dict[str, object] = {
        "api_key": "synthetic-key-uk2",
        "shard": "uk2",
        "user_agent": "Clinic Recall Tests (cliniko-tests@example.invalid)",
        "timeout_seconds": 5,
        "per_page": 1,
        "max_pages": 2,
        "max_items": 4,
    }
    inputs[field] = value
    with pytest.raises(ClinikoConfigurationError, match=f"^{expected_name}$"):
        build_cliniko_config(**inputs)


def _client_config(*, max_pages: int = 20, max_items: int = 2_000) -> ClinikoConfig:
    return ClinikoConfig(
        enabled=True,
        api_key="fixture-uk2",
        shard="uk2",
        user_agent="Clinic Recall Tests (cliniko-tests@example.invalid)",
        timeout_seconds=5.0,
        per_page=100,
        max_pages=max_pages,
        max_items=max_items,
    )


def _page(
    collection_key: str,
    items: list[dict[str, object]],
    *,
    next_url: str | None = None,
) -> dict[str, object]:
    return {
        collection_key: items,
        "total_entries": len(items),
        "links": {"next": next_url},
    }


def test_cliniko_client_sends_exact_auth_headers_and_filter_query() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_page("patients", [{"id": 900000001}]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = ClinikoClient(
            _client_config(),
            client=http_client,
            request_budget=ClinikoRequestBudget(max_attempts=1),
        )
        patients = client.get_collection(
            "patients",
            collection_key="patients",
            params=(("per_page", "1"), ("q[]", "updated_at:>2026-07-01T00:00:00Z")),
        )

    assert patients == ({"id": 900000001},)
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.scheme == "https"
    assert request.url.host == "api.uk2.cliniko.com"
    assert request.url.path == "/v1/patients"
    assert request.url.params.get_list("q[]") == ["updated_at:>2026-07-01T00:00:00Z"]
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"] == (
        "Clinic Recall Tests (cliniko-tests@example.invalid)"
    )
    expected_auth = base64.b64encode(b"fixture-uk2:").decode("ascii")
    assert request.headers["authorization"] == f"Basic {expected_auth}"


def test_cliniko_client_follows_each_valid_next_link_once() -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if len(requested_urls) == 1:
            return httpx.Response(
                200,
                json=_page(
                    "patients",
                    [{"id": 900000001}],
                    next_url="https://api.uk2.cliniko.com/v1/patients?page=2&per_page=1",
                ),
            )
        return httpx.Response(200, json=_page("patients", [{"id": 900000002}]))

    budget = ClinikoRequestBudget(max_attempts=2)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        result = ClinikoClient(
            _client_config(),
            client=http_client,
            request_budget=budget,
        ).get_collection("patients", collection_key="patients")

    assert [item["id"] for item in result] == [900000001, 900000002]
    assert requested_urls == [
        "https://api.uk2.cliniko.com/v1/patients",
        "https://api.uk2.cliniko.com/v1/patients?page=2&per_page=1",
    ]
    assert budget.attempts == 2


@pytest.mark.parametrize(
    "next_url",
    [
        "http://api.uk2.cliniko.com/v1/patients?page=2",
        "https://api.uk1.cliniko.com/v1/patients?page=2",
        "https://user@api.uk2.cliniko.com/v1/patients?page=2",
        "https://api.uk2.cliniko.com/v2/patients?page=2",
        "https://api.uk2.cliniko.com/v1/users?page=2",
        "https://api.uk2.cliniko.com/v1/patients?page=2#fragment",
    ],
)
def test_cliniko_client_rejects_unsafe_next_link(next_url: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_page("patients", [{"id": 900000001}], next_url=next_url),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = ClinikoClient(_client_config(), client=http_client)
        with pytest.raises(ClinikoPaginationError, match="^unsafe_next_link$"):
            client.get_collection("patients", collection_key="patients")


def test_cliniko_client_rejects_cycle_and_incomplete_limits() -> None:
    next_url = "https://api.uk2.cliniko.com/v1/patients"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_page("patients", [{"id": 900000001}], next_url=next_url),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = ClinikoClient(_client_config(), client=http_client)
        with pytest.raises(ClinikoPaginationError, match="^cyclic_next_link$"):
            client.get_collection("patients", collection_key="patients")

    def bounded_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_page(
                "patients",
                [{"id": 900000001}],
                next_url="https://api.uk2.cliniko.com/v1/patients?page=2",
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(bounded_handler)) as http_client:
        client = ClinikoClient(_client_config(max_pages=1), client=http_client)
        with pytest.raises(ClinikoPaginationError, match="^max_pages_exceeded$"):
            client.get_collection("patients", collection_key="patients")


def test_cliniko_client_rejects_item_and_request_budget_exhaustion() -> None:
    def oversized_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_page("patients", [{"id": 900000001}, {"id": 900000002}]),
        )

    with httpx.Client(transport=httpx.MockTransport(oversized_handler)) as http_client:
        client = ClinikoClient(_client_config(max_items=1), client=http_client)
        with pytest.raises(ClinikoPaginationError, match="^max_items_exceeded$"):
            client.get_collection("patients", collection_key="patients")

    calls = 0

    def paginated_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=_page(
                "patients",
                [{"id": 900000001}],
                next_url="https://api.uk2.cliniko.com/v1/patients?page=2",
            ),
        )

    budget = ClinikoRequestBudget(max_attempts=1)
    with httpx.Client(transport=httpx.MockTransport(paginated_handler)) as http_client:
        client = ClinikoClient(
            _client_config(),
            client=http_client,
            request_budget=budget,
        )
        with pytest.raises(ClinikoContractError, match="^request_budget_exhausted$"):
            client.get_collection("patients", collection_key="patients")

    assert calls == 1
    assert budget.attempts == 1


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(302, headers={"Location": "https://example.test"}), "redirect"),
        (httpx.Response(200, text="{}", headers={"Content-Type": "text/plain"}), "content_type"),
        (httpx.Response(200, content=b"{", headers={"Content-Type": "application/json"}), "malformed_json"),
        (
            httpx.Response(
                200,
                content=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(5 * 1024 * 1024 + 1),
                },
            ),
            "response_too_large",
        ),
        (httpx.Response(200, json={"patients": [], "total_entries": 0}), "missing_links"),
    ],
)
def test_cliniko_client_rejects_invalid_response_contract(
    response: httpx.Response,
    reason: str,
) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _request: response)) as http_client:
        client = ClinikoClient(_client_config(), client=http_client)
        with pytest.raises(ClinikoContractError, match=f"^{reason}$"):
            client.get_collection("patients", collection_key="patients")


def test_cliniko_client_enforces_streamed_body_limit_without_content_length(
    monkeypatch,
) -> None:
    from src.clinic_recall.sync import cliniko_client as client_module

    monkeypatch.setattr(client_module, "_MAX_RESPONSE_BYTES", 8)
    response = httpx.Response(
        200,
        headers={"Content-Type": "application/json"},
        stream=httpx.ByteStream(b"123456789"),
    )
    with httpx.Client(transport=httpx.MockTransport(lambda _request: response)) as http_client:
        client = ClinikoClient(_client_config(), client=http_client)
        with pytest.raises(ClinikoContractError, match="^response_too_large$"):
            client.get_collection("patients", collection_key="patients")


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (400, ClinikoValidationError),
        (401, ClinikoAuthenticationError),
        (403, ClinikoAuthenticationError),
        (404, ClinikoNotFoundError),
        (409, ClinikoValidationError),
        (422, ClinikoValidationError),
        (500, ClinikoServerError),
        (503, ClinikoServerError),
    ],
)
def test_cliniko_client_maps_status_without_retaining_provider_body(
    status_code: int,
    error_type: type[Exception],
) -> None:
    api_key = "fixture-uk2"
    response = httpx.Response(
        status_code,
        content=b'{"private_patient_value":"must-not-escape"}',
        headers={"Content-Type": "application/json"},
    )
    with httpx.Client(transport=httpx.MockTransport(lambda _request: response)) as http_client:
        client = ClinikoClient(_client_config(), client=http_client)
        with pytest.raises(error_type) as exc_info:
            client.get_collection("patients", collection_key="patients")

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert api_key not in rendered
    assert "private_patient_value" not in rendered
    assert "patients" not in rendered


def test_cliniko_client_parses_rate_limit_reset_as_aware_utc() -> None:
    response = httpx.Response(429, headers={"X-RateLimit-Reset": "2000000000"})
    with httpx.Client(transport=httpx.MockTransport(lambda _request: response)) as http_client:
        client = ClinikoClient(_client_config(), client=http_client)
        with pytest.raises(ClinikoRateLimitedError) as exc_info:
            client.get_collection("patients", collection_key="patients")

    assert exc_info.value.reset_at == datetime.fromtimestamp(2_000_000_000, UTC)


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({}, "rate_limit_reset_missing"),
        ({"X-RateLimit-Reset": "not-a-timestamp"}, "rate_limit_reset_invalid"),
    ],
)
def test_cliniko_client_rejects_missing_or_malformed_rate_limit_reset(
    headers: dict[str, str],
    reason: str,
) -> None:
    response = httpx.Response(429, headers=headers)
    with httpx.Client(transport=httpx.MockTransport(lambda _request: response)) as http_client:
        client = ClinikoClient(_client_config(), client=http_client)
        with pytest.raises(ClinikoContractError, match=f"^{reason}$"):
            client.get_collection("patients", collection_key="patients")


def test_cliniko_rate_limiter_waits_for_local_headroom() -> None:
    current = [0.0]
    sleeps: list[float] = []

    def clock() -> float:
        return current[0]

    def sleeper(delay: float) -> None:
        sleeps.append(delay)
        current[0] += delay

    limiter = ClinikoRateLimiter(
        max_requests=2,
        window_seconds=60,
        clock=clock,
        sleeper=sleeper,
    )
    limiter.before_request()
    limiter.before_request()
    limiter.before_request()

    assert sleeps == [60.0]


def test_cliniko_client_minimizes_transport_exception_and_suppresses_cause() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(
            "private-patient-value synthetic-cliniko-key-uk2",
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = ClinikoClient(_client_config(), client=http_client)
        with pytest.raises(ClinikoTransportError) as exc_info:
            client.get_collection("patients", collection_key="patients")

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert rendered == "read_timeout ClinikoTransportError('read_timeout')"
    assert exc_info.value.__cause__ is None


_CLINIKO_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "cliniko"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((_CLINIKO_FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def test_materialized_cliniko_source_maps_documented_fields_without_consent_inference() -> None:
    patient_page = _fixture("patients.json")
    appointment_page = _fixture("individual_appointments.json")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = (
            patient_page
            if request.url.path == "/v1/patients"
            else appointment_page
        )
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = ClinikoClient(_client_config(), client=http_client)
        source = materialize_cliniko_source(
            client,
            now=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
        )

    assert isinstance(source, SyncSource)
    patients = source.fetch_patients()
    assert isinstance(patients, tuple)
    assert patients[0].source_ref == "900000001"
    assert patients[0].name == "SYNTH Addie Example"
    assert patients[0].phone == "+447700900101"
    assert patients[0].email == "synth.patient@example.invalid"
    assert patients[0].consent_flags is None
    assert patients[0].opt_out_flags is None

    appointments = source.fetch_appointments()
    assert isinstance(appointments, tuple)
    assert [appointment.status for appointment in appointments] == [
        AppointmentStatus.SCHEDULED,
        AppointmentStatus.NO_SHOW,
        AppointmentStatus.CANCELLED,
        AppointmentStatus.COMPLETED,
    ]
    assert all(appointment.value is None for appointment in appointments)
    assert [request.url.path for request in requests] == [
        "/v1/patients",
        "/v1/individual_appointments",
    ]
    with pytest.raises(FrozenInstanceError):
        source._patients = ()


def test_cliniko_materialization_encodes_one_utc_updated_at_filter_per_resource() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        key = "patients" if request.url.path.endswith("/patients") else "individual_appointments"
        return httpx.Response(200, json=_page(key, []))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = ClinikoClient(_client_config(), client=http_client)
        materialize_cliniko_source(
            client,
            query=ClinikoSyncQuery(
                updated_after=datetime(2026, 7, 1, 8, 30, tzinfo=UTC)
            ),
            now=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
        )

    assert len(requests) == 2
    for request in requests:
        assert request.url.params.get("per_page") == "100"
        assert request.url.params.get_list("q[]") == [
            "updated_at:>2026-07-01T08:30:00Z"
        ]


def test_cliniko_record_parsers_retain_lifecycle_metadata_and_discard_additions() -> None:
    patient_payload = _fixture("patients.json")["patients"][1]
    appointment_payload = _fixture("individual_appointments.json")[
        "individual_appointments"
    ][2]

    patient = ClinikoPatientRecord.from_payload(patient_payload)
    appointment = ClinikoAppointmentRecord.from_payload(
        appointment_payload,
        base_url="https://api.uk2.cliniko.com/v1",
    )

    assert patient.archived_at == datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    assert appointment.cancelled_at == datetime(2026, 7, 22, 9, 0, tzinfo=UTC)
    assert appointment.archived_at == datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
    assert not hasattr(patient, "extra_provider_field")
    assert not hasattr(appointment, "extra_provider_field")


def test_cliniko_record_parsers_fail_closed_without_raw_values() -> None:
    patient_payload = dict(_fixture("patients.json")["patients"][0])
    patient_payload.pop("updated_at")
    with pytest.raises(ClinikoContractError, match="^patient_updated_at$") as patient_error:
        ClinikoPatientRecord.from_payload(patient_payload)

    appointment_payload = dict(
        _fixture("individual_appointments.json")["individual_appointments"][0]
    )
    appointment_payload["patient"] = {
        "links": {"self": "https://example.test/v1/patients/900000001"}
    }
    with pytest.raises(ClinikoContractError, match="^appointment_patient$") as appointment_error:
        ClinikoAppointmentRecord.from_payload(
            appointment_payload,
            base_url="https://api.uk2.cliniko.com/v1",
        )

    rendered = f"{patient_error.value!r} {appointment_error.value!r}"
    assert "SYNTH" not in rendered
    assert "example.test" not in rendered


def test_cliniko_materialization_rejects_naive_cursor_before_http() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=_page("patients", []))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = ClinikoClient(_client_config(), client=http_client)
        with pytest.raises(ClinikoContractError, match="^updated_after$"):
            materialize_cliniko_source(
                client,
                query=ClinikoSyncQuery(updated_after=datetime(2026, 7, 1, 8, 30)),
            )

    assert requests == 0


def test_sync_package_exports_read_only_cliniko_runtime_boundary() -> None:
    assert sync_package.ClinikoClient is ClinikoClient
    assert sync_package.materialize_cliniko_source is materialize_cliniko_source
    assert not hasattr(sync_package, "ClinikoSandboxProbe")
    assert not hasattr(ClinikoClient, "post")
    assert not hasattr(ClinikoClient, "create_appointment")
    assert not hasattr(ClinikoClient, "update_appointment")