from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from src.clinic_recall.availability import (
    AvailabilityConflictError,
    AvailabilityPreflightReason,
    AvailabilitySlotInput,
    AvailabilitySlotSignature,
    compare_availability_preflight,
    get_availability,
    upsert_availability_slots,
)
from src.clinic_recall.models import AvailabilitySlot, Clinic

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
START = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)


def _seed_clinic(session, clinic_id: str = "clinic-authoritative") -> str:
    session.add(
        Clinic(
            id=clinic_id,
            name="Synthetic Availability Clinic",
            timezone="Europe/London",
            daily_caps=20,
        )
    )
    session.flush()
    return clinic_id


def _slot_input(**overrides: object) -> AvailabilitySlotInput:
    values: dict[str, object] = {
        "source_ref": "cliniko:v1:" + "a" * 64,
        "source_provider": "cliniko",
        "business_id": "920600001",
        "appointment_type_id": "940600001",
        "clinician_id": "930600001",
        "start_at": START,
        "end_at": START + timedelta(minutes=30),
        "fetched_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
    }
    values.update(overrides)
    return AvailabilitySlotInput(**values)


def _signature(**overrides: object) -> AvailabilitySlotSignature:
    values: dict[str, object] = {
        "source_ref": "cliniko:v1:" + "a" * 64,
        "source_provider": "cliniko",
        "business_id": "920600001",
        "practitioner_id": "930600001",
        "appointment_type_id": "940600001",
        "start_at": START,
        "end_at": START + timedelta(minutes=30),
        "fetched_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "available": True,
    }
    values.update(overrides)
    return AvailabilitySlotSignature(**values)


def test_monotonic_upsert_ignores_older_observation_and_refreshes_newer(
    sqlite_session,
) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    first = upsert_availability_slots(
        sqlite_session,
        clinic_id,
        [_slot_input()],
        now=NOW,
    )[0]
    newer = _slot_input(
        fetched_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=11),
    )
    upsert_availability_slots(
        sqlite_session,
        clinic_id,
        [newer],
        now=NOW + timedelta(minutes=1),
    )
    older = _slot_input(
        fetched_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=29),
    )
    returned = upsert_availability_slots(
        sqlite_session,
        clinic_id,
        [older],
        now=NOW + timedelta(minutes=1),
    )[0]

    row = sqlite_session.get(AvailabilitySlot, first.slot_id)
    assert row is not None
    assert row.fetched_at == NOW + timedelta(minutes=1)
    assert row.expires_at == NOW + timedelta(minutes=11)
    assert returned.fetched_at == row.fetched_at
    assert returned.expires_at == row.expires_at


def test_equal_observation_is_idempotent_but_cannot_extend_expiry(
    sqlite_session,
) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    first = upsert_availability_slots(
        sqlite_session,
        clinic_id,
        [_slot_input()],
        now=NOW,
    )[0]
    repeated = upsert_availability_slots(
        sqlite_session,
        clinic_id,
        [_slot_input()],
        now=NOW,
    )[0]

    assert repeated.slot_id == first.slot_id
    with pytest.raises(
        AvailabilityConflictError,
        match="^equal_observation_conflict$",
    ):
        upsert_availability_slots(
            sqlite_session,
            clinic_id,
            [_slot_input(expires_at=NOW + timedelta(minutes=11))],
            now=NOW,
        )


def test_existing_source_ref_cannot_change_binding_or_slot_signature(
    sqlite_session,
) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    upsert_availability_slots(
        sqlite_session,
        clinic_id,
        [_slot_input()],
        now=NOW,
    )

    with pytest.raises(AvailabilityConflictError, match="^binding_mismatch$"):
        upsert_availability_slots(
            sqlite_session,
            clinic_id,
            [
                _slot_input(
                    business_id="920600999",
                    fetched_at=NOW + timedelta(minutes=1),
                    expires_at=NOW + timedelta(minutes=11),
                )
            ],
            now=NOW + timedelta(minutes=1),
        )


def test_failed_refresh_batch_does_not_partially_extend_an_earlier_slot(
    sqlite_session,
) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    first = _slot_input()
    second = _slot_input(
        source_ref="cliniko:v1:" + "b" * 64,
        start_at=START + timedelta(hours=1),
        end_at=START + timedelta(hours=1, minutes=30),
    )
    upsert_availability_slots(
        sqlite_session,
        clinic_id,
        [first, second],
        now=NOW,
    )

    with pytest.raises(AvailabilityConflictError, match="^binding_mismatch$"):
        upsert_availability_slots(
            sqlite_session,
            clinic_id,
            [
                replace(
                    first,
                    fetched_at=NOW + timedelta(minutes=1),
                    expires_at=NOW + timedelta(minutes=11),
                ),
                replace(
                    second,
                    business_id="920600999",
                    fetched_at=NOW + timedelta(minutes=1),
                    expires_at=NOW + timedelta(minutes=11),
                ),
            ],
            now=NOW + timedelta(minutes=1),
        )

    first_row = sqlite_session.execute(
        sa.select(AvailabilitySlot).where(AvailabilitySlot.source_ref == first.source_ref)
    ).scalar_one()
    assert first_row.fetched_at == NOW
    assert first_row.expires_at == NOW + timedelta(minutes=10)
    with pytest.raises(
        AvailabilityConflictError,
        match="^slot_signature_mismatch$",
    ):
        upsert_availability_slots(
            sqlite_session,
            clinic_id,
            [
                _slot_input(
                    end_at=START + timedelta(minutes=45),
                    fetched_at=NOW + timedelta(minutes=1),
                    expires_at=NOW + timedelta(minutes=11),
                )
            ],
            now=NOW + timedelta(minutes=1),
        )


def test_authoritative_upsert_rejects_future_or_incomplete_observations(
    sqlite_session,
) -> None:
    clinic_id = _seed_clinic(sqlite_session)

    with pytest.raises(ValueError, match="fetched_at must not be in the future"):
        upsert_availability_slots(
            sqlite_session,
            clinic_id,
            [
                _slot_input(
                    fetched_at=NOW + timedelta(seconds=1),
                    expires_at=NOW + timedelta(minutes=10),
                )
            ],
            now=NOW,
        )
    with pytest.raises(ValueError, match="authoritative slot binding is incomplete"):
        upsert_availability_slots(
            sqlite_session,
            clinic_id,
            [_slot_input(appointment_type_id=None)],
            now=NOW,
        )
    with pytest.raises(ValueError, match="authoritative slot details are not permitted"):
        upsert_availability_slots(
            sqlite_session,
            clinic_id,
            [_slot_input(details={"provider_text": "untrusted"})],
            now=NOW,
        )


def test_legacy_null_and_future_observations_are_unavailable(sqlite_session) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    upsert_availability_slots(
        sqlite_session,
        clinic_id,
        [
            AvailabilitySlotInput(
                source_ref="legacy-slot",
                start_at=START,
                end_at=START + timedelta(minutes=30),
                clinician_id="legacy-clinician",
            )
        ],
    )
    sqlite_session.add(
        AvailabilitySlot(
            id="slot-future-observation",
            clinic_id=clinic_id,
            source_ref="cliniko:v1:" + "b" * 64,
            source_provider="cliniko",
            business_id="920600001",
            clinician_id="930600001",
            appointment_type_id="940600001",
            start_at=START,
            end_at=START + timedelta(minutes=30),
            fetched_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=10),
            details={},
        )
    )
    sqlite_session.flush()

    offered = get_availability(
        sqlite_session,
        clinic_id,
        now=NOW,
        window_start=NOW,
        window_end=NOW + timedelta(days=7),
    )

    assert offered == []


def test_patient_safe_summary_does_not_serialize_provider_identity_or_details(
    sqlite_session,
) -> None:
    clinic_id = _seed_clinic(sqlite_session)
    summary = upsert_availability_slots(
        sqlite_session,
        clinic_id,
        [_slot_input()],
        now=NOW,
    )[0]

    payload = summary.as_dict()
    rendered = repr(payload)
    assert set(payload) == {"slot_id", "start_at", "end_at"}
    assert "920600001" not in rendered
    assert "930600001" not in rendered
    assert "940600001" not in rendered
    assert "cliniko" not in rendered


@pytest.mark.parametrize(
    ("observed", "already_claimed", "reason"),
    [
        (None, False, AvailabilityPreflightReason.MISSING),
        (_signature(), True, AvailabilityPreflightReason.ALREADY_CLAIMED),
        (
            _signature(
                fetched_at=NOW - timedelta(minutes=10),
                expires_at=NOW,
            ),
            False,
            AvailabilityPreflightReason.STALE,
        ),
        (
            _signature(source_provider="other"),
            False,
            AvailabilityPreflightReason.PROVIDER_MISMATCH,
        ),
        (
            _signature(source_ref="other-source"),
            False,
            AvailabilityPreflightReason.SOURCE_MISMATCH,
        ),
        (
            _signature(business_id="920600002"),
            False,
            AvailabilityPreflightReason.BUSINESS_MISMATCH,
        ),
        (
            _signature(practitioner_id="930600002"),
            False,
            AvailabilityPreflightReason.PRACTITIONER_MISMATCH,
        ),
        (
            _signature(appointment_type_id="940600002"),
            False,
            AvailabilityPreflightReason.APPOINTMENT_TYPE_MISMATCH,
        ),
        (
            _signature(start_at=START + timedelta(minutes=5)),
            False,
            AvailabilityPreflightReason.START_MISMATCH,
        ),
        (
            _signature(end_at=START + timedelta(minutes=45)),
            False,
            AvailabilityPreflightReason.END_MISMATCH,
        ),
        (
            _signature(available=False),
            False,
            AvailabilityPreflightReason.STALE,
        ),
    ],
)
def test_preflight_comparison_returns_closed_value_free_reason(
    observed: AvailabilitySlotSignature | None,
    already_claimed: bool,
    reason: AvailabilityPreflightReason,
) -> None:
    result = compare_availability_preflight(
        _signature(),
        observed,
        now=NOW,
        already_claimed=already_claimed,
    )

    assert result.matches is False
    assert result.reason is reason
    assert set(result.as_dict()) == {"matches", "reason"}
    assert "920600" not in repr(result.as_dict())


def test_preflight_exact_match_is_pure_and_deterministic() -> None:
    selected = _signature()
    observed = replace(selected)

    first = compare_availability_preflight(selected, observed, now=NOW)
    second = compare_availability_preflight(selected, observed, now=NOW)

    assert first == second
    assert first.matches is True
    assert first.reason is AvailabilityPreflightReason.MATCH
