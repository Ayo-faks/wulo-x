from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from src.clinic_recall.config import ClinikoConfig
from src.clinic_recall.sync.cliniko_availability import (
    ClinikoAvailabilityBinding,
    ClinikoAvailabilityConfigurationError,
    ClinikoAvailabilityProvider,
)
from src.clinic_recall.sync.cliniko_capability import EvidenceAuthority
from src.clinic_recall.sync.cliniko_client import (
    ClinikoClient,
    ClinikoContractError,
    ClinikoPaginationError,
    ClinikoRequestBudget,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "cliniko" / "pr06"
NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _client_config(
    *,
    per_page: int = 1,
    max_pages: int = 3,
    max_items: int = 4,
) -> ClinikoConfig:
    return ClinikoConfig(
        enabled=True,
        api_key="fixture-uk2",
        shard="uk2",
        user_agent="Clinic Recall Tests (cliniko-tests@example.invalid)",
        timeout_seconds=5.0,
        per_page=per_page,
        max_pages=max_pages,
        max_items=max_items,
    )


def _binding(**overrides: object) -> ClinikoAvailabilityBinding:
    values: dict[str, object] = {
        "clinic_id": "clinic-pr06-synthetic",
        "business_id": "920600001",
        "practitioner_id": "930600001",
        "appointment_type_id": "940600001",
        "appointment_duration": timedelta(minutes=30),
        "freshness_duration": timedelta(minutes=10),
        "evidence_authority": EvidenceAuthority.FIXTURE_VERIFIED,
    }
    values.update(overrides)
    return ClinikoAvailabilityBinding(**values)


def _list_from_payload(
    payload: dict[str, object],
    *,
    binding: ClinikoAvailabilityBinding | None = None,
) -> tuple:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=payload)
        )
    ) as http_client:
        provider = ClinikoAvailabilityProvider(
            ClinikoClient(_client_config(), client=http_client),
            binding or _binding(),
            clock=lambda: NOW,
        )
        return tuple(
            provider.list_slots(
                clinic_id="clinic-pr06-synthetic",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )
        )


def test_provider_materializes_exact_paginated_available_times_contract() -> None:
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        payload = (
            _fixture("binding_a_page_2.json")
            if request.url.params.get("page") == "2"
            else _fixture("binding_a_page_1.json")
        )
        return httpx.Response(200, json=payload)

    budget = ClinikoRequestBudget(max_attempts=2)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        provider = ClinikoAvailabilityProvider(
            ClinikoClient(
                _client_config(),
                client=http_client,
                request_budget=budget,
            ),
            _binding(),
            clock=lambda: NOW,
        )
        slots = tuple(
            provider.list_slots(
                clinic_id="clinic-pr06-synthetic",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )
        )

    assert len(slots) == 2
    assert [slot.start_at for slot in slots] == [
        datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
        datetime(2026, 8, 2, 10, 30, tzinfo=UTC),
    ]
    assert all(slot.end_at - slot.start_at == timedelta(minutes=30) for slot in slots)
    assert all(slot.fetched_at == NOW for slot in slots)
    assert all(slot.expires_at == NOW + timedelta(minutes=10) for slot in slots)
    assert all(slot.source_provider == "cliniko" for slot in slots)
    assert all(slot.business_id == "920600001" for slot in slots)
    assert all(slot.clinician_id == "930600001" for slot in slots)
    assert all(slot.appointment_type_id == "940600001" for slot in slots)
    assert all(slot.details is None for slot in slots)
    assert budget.attempts == 2
    assert [request.method for request in requested] == ["GET", "GET"]
    assert requested[0].url.path == (
        "/v1/businesses/920600001/practitioners/930600001/"
        "appointment_types/940600001/available_times"
    )
    assert dict(requested[0].url.params) == {
        "from": "2026-08-01",
        "to": "2026-08-02",
        "per_page": "1",
    }
    assert requested[1].url.params.get("page") == "2"


def test_canonical_source_identity_is_stable_and_binding_aware() -> None:
    start = "2026-08-01T09:00:00Z"
    first = _list_from_payload(
        {
            "available_times": [{"appointment_start": start}],
            "total_entries": 1,
            "links": {},
        }
    )[0]
    repeated = _list_from_payload(
        {
            "available_times": [{"appointment_start": start}],
            "total_entries": 1,
            "links": {},
        }
    )[0]
    other_binding = _list_from_payload(
        _fixture("binding_b_page_1.json"),
        binding=_binding(
            business_id="920600002",
            practitioner_id="930600002",
            appointment_type_id="940600002",
            appointment_duration=timedelta(minutes=45),
        ),
    )[0]

    assert first.source_ref == repeated.source_ref
    assert first.source_ref != other_binding.source_ref
    assert re.fullmatch(r"cliniko:v1:[0-9a-f]{64}", first.source_ref)
    assert first.start_at == other_binding.start_at
    assert first.end_at != other_binding.end_at


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        (
            {
                "appointment_start": "2026-08-01T09:00:00Z",
                "untrusted_addition": "do not retain",
            },
            "available_time_schema",
        ),
        ({"appointment_start": "2026-08-01T09:00:00"}, "appointment_start_utc"),
        (
            {"appointment_start": "2026-08-01T10:00:00+01:00"},
            "appointment_start_utc",
        ),
        ({"appointment_start": 123}, "appointment_start"),
        ({"appointment_start": "2026-08-04T09:00:00Z"}, "appointment_start_window"),
    ],
)
def test_provider_rejects_malformed_or_untrusted_available_time_items(
    item: dict[str, object],
    reason: str,
) -> None:
    payload = {"available_times": [item], "total_entries": 1, "links": {}}

    with pytest.raises(ClinikoContractError, match=f"^{reason}$") as exc_info:
        _list_from_payload(payload)

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert "untrusted_addition" not in rendered
    assert "2026-08" not in rendered


def test_provider_rejects_duplicate_starts_across_pages() -> None:
    first_page = _fixture("binding_a_page_1.json")
    duplicate_page = {
        "available_times": [{"appointment_start": "2026-08-01T09:00:00Z"}],
        "total_entries": 2,
        "links": {},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=(
                duplicate_page
                if request.url.params.get("page") == "2"
                else first_page
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        provider = ClinikoAvailabilityProvider(
            ClinikoClient(_client_config(), client=http_client),
            _binding(),
            clock=lambda: NOW,
        )
        with pytest.raises(
            ClinikoContractError,
            match="^duplicate_available_time$",
        ):
            provider.list_slots(
                clinic_id="clinic-pr06-synthetic",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )


def test_provider_rejects_unsafe_next_resource_without_following_it() -> None:
    calls = 0
    payload = {
        "available_times": [{"appointment_start": "2026-08-01T09:00:00Z"}],
        "total_entries": 1,
        "links": {"next": "https://api.uk2.cliniko.com/v1/patients?page=2"},
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        provider = ClinikoAvailabilityProvider(
            ClinikoClient(_client_config(), client=http_client),
            _binding(),
            clock=lambda: NOW,
        )
        with pytest.raises(ClinikoPaginationError, match="^unsafe_next_link$"):
            provider.list_slots(
                clinic_id="clinic-pr06-synthetic",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )

    assert calls == 1


@pytest.mark.parametrize(
    ("second_total", "second_items", "reason"),
    [
        (3, [{"appointment_start": "2026-08-02T10:30:00Z"}], "available_times_total_mismatch"),
        (2, [], "incomplete_available_times"),
    ],
)
def test_provider_rejects_inconsistent_or_partial_pagination(
    second_total: int,
    second_items: list[dict[str, object]],
    reason: str,
) -> None:
    first_page = _fixture("binding_a_page_1.json")
    second_page = {
        "available_times": second_items,
        "total_entries": second_total,
        "links": {},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=(
                second_page
                if request.url.params.get("page") == "2"
                else first_page
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        provider = ClinikoAvailabilityProvider(
            ClinikoClient(_client_config(), client=http_client),
            _binding(),
            clock=lambda: NOW,
        )
        with pytest.raises(ClinikoPaginationError, match=f"^{reason}$"):
            provider.list_slots(
                clinic_id="clinic-pr06-synthetic",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        ({"business_id": "not-numeric"}, "business_id"),
        ({"practitioner_id": "0"}, "practitioner_id"),
        ({"appointment_type_id": "-1"}, "appointment_type_id"),
        ({"appointment_duration": timedelta(0)}, "appointment_duration"),
        ({"appointment_duration": timedelta(days=1)}, "appointment_duration"),
        ({"freshness_duration": timedelta(0)}, "freshness_duration"),
        ({"freshness_duration": timedelta(minutes=31)}, "freshness_duration"),
        ({"evidence_authority": EvidenceAuthority.SANDBOX_WRITE_VERIFIED}, "evidence_authority"),
    ],
)
def test_binding_rejects_invalid_or_overbroad_configuration(
    config: dict[str, object],
    reason: str,
) -> None:
    with pytest.raises(ClinikoAvailabilityConfigurationError, match=f"^{reason}$"):
        _binding(**config)


def test_provider_rejects_binding_substitution_before_http() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_fixture("empty_page.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        provider = ClinikoAvailabilityProvider(
            ClinikoClient(_client_config(), client=http_client),
            _binding(),
            clock=lambda: NOW,
        )
        with pytest.raises(
            ClinikoAvailabilityConfigurationError,
            match="^clinic_binding_mismatch$",
        ):
            provider.list_slots(
                clinic_id="clinic-substituted",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )
        with pytest.raises(
            ClinikoAvailabilityConfigurationError,
            match="^practitioner_binding_mismatch$",
        ):
            provider.list_slots(
                clinic_id="clinic-pr06-synthetic",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
                clinician_id="930699999",
            )

    assert calls == 0


def test_empty_page_is_a_valid_synthetic_observation() -> None:
    slots = _list_from_payload(_fixture("empty_page.json"))

    assert slots == ()
    assert _binding().evidence_authority is EvidenceAuthority.FIXTURE_VERIFIED


@pytest.mark.parametrize(
    ("max_pages", "max_items", "reason"),
    [
        (1, 4, "max_pages_exceeded"),
        (3, 1, "max_items_exceeded"),
    ],
)
def test_provider_enforces_total_page_and_item_bounds(
    max_pages: int,
    max_items: int,
    reason: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = (
            _fixture("binding_a_page_2.json")
            if request.url.params.get("page") == "2"
            else _fixture("binding_a_page_1.json")
        )
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        provider = ClinikoAvailabilityProvider(
            ClinikoClient(
                _client_config(max_pages=max_pages, max_items=max_items),
                client=http_client,
            ),
            _binding(),
            clock=lambda: NOW,
        )
        with pytest.raises(ClinikoPaginationError, match=f"^{reason}$"):
            provider.list_slots(
                clinic_id="clinic-pr06-synthetic",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )
