from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from src.clinic_recall import sync as sync_package
from src.clinic_recall.config import ClinikoConfig
from src.clinic_recall.sync.cliniko_capability import (
    Capability,
    CapabilityStatus,
    EvidenceAuthority,
    cliniko_config_fingerprint,
)
from src.clinic_recall.sync.cliniko_sandbox_probe import (
    PrivateRequestLedger,
    SandboxProbeConfigurationError,
    SandboxReadManifest,
    load_private_manifest,
    main,
    run_sandbox_read_probe,
    verify_current_source_identity,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "cliniko"
NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _json_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _config() -> ClinikoConfig:
    return ClinikoConfig(
        enabled=True,
        api_key="fixture-uk2",
        shard="uk2",
        user_agent="Clinic Recall Probe Tests (probe@example.invalid)",
        timeout_seconds=5,
        per_page=1,
        max_pages=2,
        max_items=4,
    )


def _manifest(**overrides: object) -> SandboxReadManifest:
    values: dict[str, object] = {
        "schema_version": 1,
        "approval_evidence_sha256": "a" * 64,
        "expected_shard": "uk2",
        "synthetic_only": True,
        "source_commit": "b" * 40,
        "fixture_profile_sha256": "c" * 64,
        "client_sha256": "d" * 64,
        "adapter_sha256": "e" * 64,
        "config_fingerprint": cliniko_config_fingerprint(_config()),
        "planned_attempts": 16,
        "expires_at": datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        "patient_id": "900000001",
        "appointment_id": "910000001",
        "business_id": "920000001",
        "practitioner_id": "930000001",
        "appointment_type_id": "940000001",
        "updated_after": datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        "availability_from": date(2026, 8, 1),
        "availability_to": date(2026, 8, 7),
    }
    values.update(overrides)
    return SandboxReadManifest(**values)


def _manifest_json() -> dict[str, object]:
    manifest = _manifest()
    return {
        "adapter_sha256": manifest.adapter_sha256,
        "appointment_id": manifest.appointment_id,
        "appointment_type_id": manifest.appointment_type_id,
        "approval_evidence_sha256": manifest.approval_evidence_sha256,
        "availability_from": manifest.availability_from.isoformat(),
        "availability_to": manifest.availability_to.isoformat(),
        "business_id": manifest.business_id,
        "client_sha256": manifest.client_sha256,
        "config_fingerprint": manifest.config_fingerprint,
        "expected_shard": manifest.expected_shard,
        "expires_at": manifest.expires_at.isoformat().replace("+00:00", "Z"),
        "fixture_profile_sha256": manifest.fixture_profile_sha256,
        "patient_id": manifest.patient_id,
        "planned_attempts": manifest.planned_attempts,
        "practitioner_id": manifest.practitioner_id,
        "schema_version": manifest.schema_version,
        "source_commit": manifest.source_commit,
        "synthetic_only": manifest.synthetic_only,
        "updated_after": manifest.updated_after.isoformat().replace("+00:00", "Z"),
    }


def _private_root(tmp_path: Path) -> Path:
    root = tmp_path / "private-evidence"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def test_private_manifest_requires_private_parent_file_and_exact_schema(
    tmp_path: Path,
) -> None:
    root = _private_root(tmp_path)
    path = root / "read-manifest.json"
    path.write_text(json.dumps(_manifest_json()), encoding="utf-8")
    path.chmod(0o600)

    root.chmod(0o755)
    with pytest.raises(SandboxProbeConfigurationError, match="private_parent"):
        load_private_manifest(path, now=NOW)

    root.chmod(0o700)
    path.chmod(0o644)
    with pytest.raises(SandboxProbeConfigurationError, match="private_file"):
        load_private_manifest(path, now=NOW)

    path.chmod(0o600)
    loaded = load_private_manifest(path, now=NOW)
    assert loaded.expected_shard == "uk2"
    assert loaded.synthetic_only is True

    payload = _manifest_json()
    payload["api_key"] = "must-never-be-accepted"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(SandboxProbeConfigurationError, match="manifest_schema"):
        load_private_manifest(path, now=NOW)


def test_private_manifest_rejects_expired_approval_window(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    path = root / "expired-manifest.json"
    payload = _manifest_json()
    payload["expires_at"] = "2026-07-23T12:00:00Z"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(SandboxProbeConfigurationError, match="expires_at"):
        load_private_manifest(path, now=NOW)


def test_private_evidence_paths_are_create_once(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    ledger_path = root / "ledger.jsonl"
    ledger_path.write_text("existing", encoding="ascii")
    ledger_path.chmod(0o600)

    with pytest.raises(SandboxProbeConfigurationError, match="ledger_create"):
        PrivateRequestLedger.create(ledger_path, planned_attempts=16)


@pytest.mark.parametrize(
    "overrides",
    [
        {"synthetic_only": False},
        {"expected_shard": "au1"},
        {"planned_attempts": 17},
        {"availability_to": date(2026, 8, 9)},
        {"patient_id": "patient-private-text"},
    ],
)
def test_sandbox_manifest_fails_closed_on_unsafe_scope(overrides: dict[str, object]) -> None:
    with pytest.raises(SandboxProbeConfigurationError):
        _manifest(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"approval_evidence_sha256": 1},
        {"source_commit": 1},
        {"expected_shard": ["uk2"]},
        {"planned_attempts": True},
        {"updated_after": "2026-07-01T00:00:00Z"},
        {"availability_from": datetime(2026, 8, 1, tzinfo=UTC)},
    ],
)
def test_sandbox_manifest_rejects_hostile_json_types(overrides: dict[str, object]) -> None:
    with pytest.raises(SandboxProbeConfigurationError):
        _manifest(**overrides)


def test_sandbox_read_probe_is_bounded_minimized_and_truthful(tmp_path: Path) -> None:
    patient_page = _json_fixture("patients.json")
    appointment_page = _json_fixture("individual_appointments.json")
    available_page = _json_fixture("available_times.json")
    requests: list[httpx.Request] = []

    def page(key: str, items: list[dict[str, object]], *, next_url: str | None = None):
        return {key: items, "total_entries": len(items), "links": {"next": next_url}}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        filters = request.url.params.get_list("q[]")
        if path == "/v1/patients/900000001":
            return httpx.Response(200, json=patient_page["patients"][0])
        if path == "/v1/patients":
            if "archived_at:*" in filters:
                return httpx.Response(200, json=page("patients", [patient_page["patients"][1]]))
            if any(value.startswith("updated_at:>") for value in filters):
                return httpx.Response(200, json=page("patients", [patient_page["patients"][0]]))
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json=page("patients", [patient_page["patients"][1]]))
            return httpx.Response(
                200,
                json=page(
                    "patients",
                    [patient_page["patients"][0]],
                    next_url="https://api.uk2.cliniko.com/v1/patients?page=2&per_page=1",
                ),
            )
        if path == "/v1/individual_appointments/910000001":
            return httpx.Response(200, json=appointment_page["individual_appointments"][0])
        if path == "/v1/individual_appointments":
            if "archived_at:*" in filters:
                item = appointment_page["individual_appointments"][2]
            elif "cancelled_at:*" in filters:
                item = appointment_page["individual_appointments"][2]
            elif "did_not_arrive:=true" in filters:
                item = appointment_page["individual_appointments"][1]
            else:
                item = appointment_page["individual_appointments"][0]
            return httpx.Response(200, json=page("individual_appointments", [item]))
        if path.endswith("/available_times"):
            return httpx.Response(200, json=available_page)
        raise AssertionError("unexpected synthetic probe request")

    private_root = _private_root(tmp_path)
    ledger = PrivateRequestLedger.create(
        private_root / "request-ledger.jsonl",
        planned_attempts=16,
        clock=lambda: NOW,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        result = run_sandbox_read_probe(
            manifest=_manifest(),
            config=_config(),
            http_client=http_client,
            ledger=ledger,
            checked_at=NOW,
        )

    assert result.complete is True
    assert result.reason_codes == ()
    assert result.profile.attempted_request_count == 12
    assert result.profile.approval_evidence_sha256 == "a" * 64
    assert result.profile.previous_profile_sha256 == "c" * 64
    assert len(requests) == 12
    assert {request.method for request in requests} == {"GET"}

    records = {record.capability: record for record in result.profile.records}
    for capability in (
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
    ):
        assert records[capability].authority is EvidenceAuthority.SANDBOX_READ_VERIFIED
        assert records[capability].status is CapabilityStatus.SUPPORTED

    for capability in (
        Capability.APPOINTMENT_CREATE,
        Capability.APPOINTMENT_UPDATE,
        Capability.APPOINTMENT_READ_BACK,
    ):
        assert records[capability].authority is EvidenceAuthority.FIXTURE_VERIFIED
        assert records[capability].status is CapabilityStatus.NOT_TESTED

    ledger_text = ledger.path.read_text(encoding="ascii")
    assert len(ledger_text.splitlines()) == 12
    for forbidden in (
        "900000001",
        "910000001",
        "920000001",
        "synthetic-probe-key",
        "https://",
        "SYNTH",
    ):
        assert forbidden not in ledger_text


def test_sandbox_read_probe_stops_before_http_on_manifest_mismatch(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("provider transport must remain untouched")

    ledger = PrivateRequestLedger.create(
        _private_root(tmp_path) / "request-ledger.jsonl",
        planned_attempts=16,
        clock=lambda: NOW,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(SandboxProbeConfigurationError, match="config_fingerprint"):
            run_sandbox_read_probe(
                manifest=_manifest(config_fingerprint="0" * 64),
                config=_config(),
                http_client=http_client,
                ledger=ledger,
                checked_at=NOW,
            )

    assert calls == 0
    assert ledger.attempts == 0


def test_incomplete_read_evidence_never_claims_profile_completion(tmp_path: Path) -> None:
    patient_page = _json_fixture("patients.json")
    appointment_page = _json_fixture("individual_appointments.json")

    def empty_page(key: str) -> dict[str, object]:
        return {key: [], "total_entries": 0, "links": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        filters = request.url.params.get_list("q[]")
        if path == "/v1/patients/900000001":
            return httpx.Response(200, json=patient_page["patients"][0])
        if path == "/v1/patients":
            if "archived_at:*" in filters:
                return httpx.Response(200, json=empty_page("patients"))
            return httpx.Response(
                200,
                json={
                    "patients": [patient_page["patients"][0]],
                    "total_entries": 1,
                    "links": {},
                },
            )
        if path == "/v1/individual_appointments/910000001":
            return httpx.Response(200, json=appointment_page["individual_appointments"][0])
        if path == "/v1/individual_appointments":
            if any(
                value in filters
                for value in ("archived_at:*", "cancelled_at:*", "did_not_arrive:=true")
            ):
                return httpx.Response(200, json=empty_page("individual_appointments"))
            return httpx.Response(
                200,
                json={
                    "individual_appointments": [
                        appointment_page["individual_appointments"][0]
                    ],
                    "total_entries": 1,
                    "links": {},
                },
            )
        if path.endswith("/available_times"):
            return httpx.Response(200, json=empty_page("available_times"))
        raise AssertionError("unexpected synthetic probe request")

    ledger = PrivateRequestLedger.create(
        _private_root(tmp_path) / "request-ledger.jsonl",
        planned_attempts=16,
        clock=lambda: NOW,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        result = run_sandbox_read_probe(
            manifest=_manifest(),
            config=_config(),
            http_client=http_client,
            ledger=ledger,
            checked_at=NOW,
        )

    assert result.complete is False
    assert result.profile.attempted_request_count == 11
    assert set(result.reason_codes) == {
        "archived_appointment_not_observed",
        "archived_patient_not_observed",
        "available_time_not_observed",
        "cancelled_appointment_not_observed",
        "no_show_not_observed",
        "pagination_not_observed",
    }


def test_sandbox_cli_requires_confirmation_before_client_creation(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest_json()), encoding="utf-8")
    manifest_path.chmod(0o600)
    client_calls = 0

    def client_factory() -> httpx.Client:
        nonlocal client_calls
        client_calls += 1
        raise AssertionError("client must not be constructed")

    with pytest.raises(SystemExit):
        main(
            [
                "sandbox-read",
                "--manifest",
                str(manifest_path),
                "--ledger",
                str(root / "ledger.jsonl"),
                "--profile-output",
                str(root / "profile.json"),
                "--approval-sha256",
                "a" * 64,
            ],
            client_factory=client_factory,
        )

    assert client_calls == 0
    assert not (root / "ledger.jsonl").exists()
    assert not (root / "profile.json").exists()


def test_sandbox_cli_rejects_approval_mismatch_before_client_creation(
    tmp_path: Path,
    capsys,
) -> None:
    root = _private_root(tmp_path)
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest_json()), encoding="utf-8")
    manifest_path.chmod(0o600)
    client_calls = 0

    def client_factory() -> httpx.Client:
        nonlocal client_calls
        client_calls += 1
        raise AssertionError("client must not be constructed")

    result = main(
        [
            "sandbox-read",
            "--manifest",
            str(manifest_path),
            "--ledger",
            str(root / "ledger.jsonl"),
            "--profile-output",
            str(root / "profile.json"),
            "--approval-sha256",
            "0" * 64,
            "--confirm-sandbox-read",
        ],
        client_factory=client_factory,
        clock=lambda: NOW,
    )

    assert result == 2
    assert client_calls == 0
    assert json.loads(capsys.readouterr().err) == {
        "complete": False,
        "reason_code": "approval_evidence_sha256",
    }
    assert not (root / "ledger.jsonl").exists()
    assert not (root / "profile.json").exists()


def test_sandbox_cli_rejects_existing_profile_before_client_creation(
    tmp_path: Path,
    capsys,
) -> None:
    root = _private_root(tmp_path)
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest_json()), encoding="utf-8")
    manifest_path.chmod(0o600)
    profile_path = root / "profile.json"
    profile_path.write_text("existing", encoding="ascii")
    profile_path.chmod(0o600)
    client_calls = 0

    def client_factory() -> httpx.Client:
        nonlocal client_calls
        client_calls += 1
        raise AssertionError("client must not be constructed")

    result = main(
        [
            "sandbox-read",
            "--manifest",
            str(manifest_path),
            "--ledger",
            str(root / "ledger.jsonl"),
            "--profile-output",
            str(profile_path),
            "--approval-sha256",
            "a" * 64,
            "--confirm-sandbox-read",
        ],
        client_factory=client_factory,
        config_loader=_config,
        source_identity_validator=lambda _manifest: None,
        clock=lambda: NOW,
    )

    assert result == 2
    assert client_calls == 0
    assert json.loads(capsys.readouterr().err)["reason_code"] == "profile_create"
    assert not (root / "ledger.jsonl").exists()
    assert profile_path.read_text(encoding="ascii") == "existing"


def test_sandbox_cli_minimizes_unexpected_factory_failure(
    tmp_path: Path,
    capsys,
) -> None:
    root = _private_root(tmp_path)
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest_json()), encoding="utf-8")
    manifest_path.chmod(0o600)

    def client_factory() -> httpx.Client:
        raise RuntimeError("private transport setup detail")

    result = main(
        [
            "sandbox-read",
            "--manifest",
            str(manifest_path),
            "--ledger",
            str(root / "ledger.jsonl"),
            "--profile-output",
            str(root / "profile.json"),
            "--approval-sha256",
            "a" * 64,
            "--confirm-sandbox-read",
        ],
        client_factory=client_factory,
        config_loader=_config,
        source_identity_validator=lambda _manifest: None,
        clock=lambda: NOW,
    )

    stderr = capsys.readouterr().err
    assert result == 2
    assert json.loads(stderr)["reason_code"] == "unexpected_failure"
    assert "private transport" not in stderr
    assert (root / "ledger.jsonl").stat().st_mode & 0o077 == 0
    assert (root / "profile.json").stat().st_mode & 0o077 == 0


def test_source_identity_binds_git_head_client_and_adapter() -> None:
    root = Path(__file__).resolve().parents[1]
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    client_path = root / "src/clinic_recall/sync/cliniko_client.py"
    adapter_path = root / "src/clinic_recall/sync/cliniko_adapter.py"
    client_sha256 = hashlib.sha256(client_path.read_bytes()).hexdigest()
    adapter_sha256 = hashlib.sha256(adapter_path.read_bytes()).hexdigest()
    manifest = _manifest(
        source_commit=source_commit,
        client_sha256=client_sha256,
        adapter_sha256=adapter_sha256,
    )

    verify_current_source_identity(manifest)

    with pytest.raises(SandboxProbeConfigurationError, match="client_sha256"):
        verify_current_source_identity(replace(manifest, client_sha256="0" * 64))


def test_sandbox_probe_is_not_exported_from_runtime_package() -> None:
    assert not hasattr(sync_package, "SandboxReadManifest")
    assert not hasattr(sync_package, "run_sandbox_read_probe")
    assert not hasattr(sync_package, "PrivateRequestLedger")