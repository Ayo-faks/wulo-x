import json
from datetime import UTC, datetime, timedelta

import pytest
from src.clinic_recall.config import ClinikoConfig
from src.clinic_recall.sync.cliniko_capability import (
    REQUIRED_CAPABILITIES,
    Capability,
    CapabilityProfile,
    CapabilityRecord,
    CapabilityStatus,
    EvidenceAuthority,
    HttpMethod,
    canonical_profile_bytes,
    cliniko_config_fingerprint,
    fixture_capability_profile,
    main,
    seal_profile,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _record(
    capability: Capability,
    *,
    fields: tuple[str, ...] = (),
    method: HttpMethod = HttpMethod.GET,
    authority: EvidenceAuthority = EvidenceAuthority.FIXTURE_VERIFIED,
) -> CapabilityRecord:
    return CapabilityRecord(
        capability=capability,
        method=method,
        authority=authority,
        status=CapabilityStatus.SUPPORTED,
        reason_code="fixture_contract",
        status_class="2xx",
        response_field_paths=fields,
        schema_sha256="a" * 64 if fields else None,
        checked_at=NOW,
        expires_at=NOW + timedelta(days=30),
        revalidation_triggers=("client_changed",),
    )


def _profile(records: tuple[CapabilityRecord, ...]) -> CapabilityProfile:
    return CapabilityProfile(
        schema_version=1,
        generated_at=NOW,
        source_commit="b" * 40,
        client_sha256="c" * 64,
        adapter_sha256="d" * 64,
        config_fingerprint="e" * 64,
        approval_evidence_sha256=None,
        attempted_request_count=0,
        previous_profile_sha256=None,
        records=records,
    )


def test_profile_json_canonicalization_determinism() -> None:
    forward = fixture_capability_profile(
        generated_at=NOW,
        source_commit="b" * 40,
        client_sha256="c" * 64,
        adapter_sha256="d" * 64,
        config_fingerprint="e" * 64,
    )
    reversed_profile = _profile(tuple(reversed(forward.records)))

    forward_bytes = canonical_profile_bytes(forward)
    reversed_bytes = canonical_profile_bytes(reversed_profile)

    assert forward_bytes == reversed_bytes
    assert seal_profile(forward).sha256 == seal_profile(reversed_profile).sha256
    payload = json.loads(forward_bytes)
    assert "profile_sha256" not in payload
    assert payload["records"][0]["capability"] == min(
        capability.value for capability in REQUIRED_CAPABILITIES
    )


def test_capability_profile_requires_every_capability_exactly_once() -> None:
    complete = fixture_capability_profile(
        generated_at=NOW,
        source_commit="b" * 40,
        client_sha256="c" * 64,
        adapter_sha256="d" * 64,
        config_fingerprint="e" * 64,
    )
    with pytest.raises(ValueError, match="capability set"):
        _profile(complete.records[:-1])
    with pytest.raises(ValueError, match="capability set"):
        _profile(complete.records + (complete.records[0],))


def test_sandbox_read_authority_is_restricted_to_get_capabilities() -> None:
    with pytest.raises(ValueError, match="sandbox read authority"):
        _record(
            Capability.APPOINTMENT_CREATE,
            method=HttpMethod.POST,
            authority=EvidenceAuthority.SANDBOX_READ_VERIFIED,
        )


def test_capability_record_rejects_raw_or_unbounded_evidence_fields() -> None:
    with pytest.raises(ValueError, match="response_field_paths"):
        _record(Capability.PATIENT_INDEX, fields=("patients.900000001.name",))
    with pytest.raises(ValueError, match="reason_code"):
        CapabilityRecord(
            capability=Capability.PATIENT_INDEX,
            method=HttpMethod.GET,
            authority=EvidenceAuthority.FIXTURE_VERIFIED,
            status=CapabilityStatus.SUPPORTED,
            reason_code="SYNTH patient private value",
            status_class="2xx",
            response_field_paths=(),
            schema_sha256=None,
            checked_at=NOW,
            expires_at=None,
            revalidation_triggers=(),
        )
    with pytest.raises(ValueError, match="checked_at"):
        CapabilityRecord(
            capability=Capability.PATIENT_INDEX,
            method=HttpMethod.GET,
            authority=EvidenceAuthority.FIXTURE_VERIFIED,
            status=CapabilityStatus.SUPPORTED,
            reason_code="fixture_contract",
            status_class="2xx",
            response_field_paths=(),
            schema_sha256=None,
            checked_at=datetime(2026, 7, 24, 12, 0),
            expires_at=None,
            revalidation_triggers=(),
        )


def test_config_fingerprint_excludes_key_and_user_agent() -> None:
    first = ClinikoConfig(
        enabled=True,
        api_key="first-uk2",
        shard="uk2",
        user_agent="First Contact (first@example.invalid)",
        timeout_seconds=5,
        per_page=100,
        max_pages=20,
        max_items=2_000,
    )
    second = ClinikoConfig(
        enabled=True,
        api_key="second-uk2",
        shard="uk2",
        user_agent="Second Contact (second@example.invalid)",
        timeout_seconds=5,
        per_page=100,
        max_pages=20,
        max_items=2_000,
    )

    assert cliniko_config_fingerprint(first) == cliniko_config_fingerprint(second)


def test_fixture_cli_is_offline_and_emits_only_canonical_profile(capsys) -> None:
    result = main(
        [
            "fixture",
            "--checked-at",
            "2026-07-24T12:00:00Z",
            "--source-commit",
            "b" * 40,
            "--client-sha256",
            "c" * 64,
            "--adapter-sha256",
            "d" * 64,
            "--config-fingerprint",
            "e" * 64,
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["authority"] == "fixture_verified"
    assert payload["attempted_request_count"] == 0
    assert len(payload["records"]) == len(REQUIRED_CAPABILITIES)


def test_fixture_profile_claims_only_exercised_read_contracts() -> None:
    profile = fixture_capability_profile(
        generated_at=NOW,
        source_commit="b" * 40,
        client_sha256="c" * 64,
        adapter_sha256="d" * 64,
        config_fingerprint="e" * 64,
    )
    records = {record.capability: record for record in profile.records}

    assert records[Capability.AVAILABLE_TIMES_READ].authority is (
        EvidenceAuthority.FIXTURE_VERIFIED
    )
    assert records[Capability.AVAILABLE_TIMES_READ].status is CapabilityStatus.SUPPORTED

    for capability in (
        Capability.APPOINTMENT_CREATE,
        Capability.APPOINTMENT_UPDATE,
        Capability.APPOINTMENT_READ_BACK,
    ):
        assert records[capability].authority is EvidenceAuthority.FIXTURE_VERIFIED
        assert records[capability].status is CapabilityStatus.NOT_TESTED
        assert records[capability].reason_code == "not_tested"
        assert records[capability].status_class is None