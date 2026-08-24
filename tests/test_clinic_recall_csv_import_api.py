"""PR-08 CSV import API tests: authorize-before-parse, bounded errors, spools.

Uses the same TestClient harness as the surfaces API suite. Every fixture is
synthetic; the staff context comes from the env fallback or EasyAuth headers,
never from request bodies.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from apps.artagent.backend.api.v1.endpoints import clinic_recall
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.clinic_recall.enums import SourceSystem
from src.clinic_recall.models import (
    Appointment,
    Base,
    Campaign,
    Clinic,
    ExternalEffect,
    ImportBatch,
    OutreachJob,
    Patient,
)
from src.clinic_recall.sync.csv_matching import (
    ProviderPatientSnapshot,
)

FIXTURES = Path(__file__).parent / "fixtures" / "csv" / "pr08"
VALID_BYTES = (FIXTURES / "valid_multi.csv").read_bytes()
CHANGED_BYTES = (FIXTURES / "changed_variant.csv").read_bytes()

EXPORT_AT = "2026-07-25T18:00:00+00:00"

PREVIEW_FIELDS = {"source_system": "csv", "export_at": EXPORT_AT}
APPROVE_FIELDS = {
    "source_system": "csv",
    "export_at": EXPORT_AT,
    "attestation_version": "csv-attest-v1",
    "attested_channels": "sms",
    "confirm_clinic_authority": "true",
}

# Values that must never appear in any API response body.
RAW_MARKERS = ("PAT-PR08", "APPT-PR08", "Alpha", "+44770090", "clinic-test.invalid")


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(clinic_recall.router, prefix="/api/v1/clinic-recall")
    return app


def _factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(Clinic(id="clinic-a", name="Clinic A"))
        session.add(Clinic(id="clinic-b", name="Clinic B"))
        session.commit()
    return factory


@pytest.fixture
def api(monkeypatch):
    """A TestClient with env staff context for clinic-a and import enabled."""
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("CLINIC_RECALL_STAFF_CLINIC_ID", "clinic-a")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ACTOR", "staff:test-alice")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ROLES", "staff")
    monkeypatch.setenv("CLINIC_RECALL_CSV_IMPORT_ENABLED", "true")
    monkeypatch.delenv("CLINIC_RECALL_CSV_MATCHING_ENABLED", raising=False)
    client = TestClient(_app())
    return client, factory, monkeypatch


def _post_csv(client, url, data: bytes, fields: dict[str, str]):
    return client.post(
        url,
        files={"file": ("upload.csv", data, "text/csv")},
        data=fields,
    )


def _preview(client, data: bytes = VALID_BYTES):
    return _post_csv(client, "/api/v1/clinic-recall/imports/csv/preview", data, PREVIEW_FIELDS)


def _approve(client, batch_id: str, data: bytes = VALID_BYTES, fields=None):
    return _post_csv(
        client,
        f"/api/v1/clinic-recall/imports/csv/{batch_id}/approve",
        data,
        fields or APPROVE_FIELDS,
    )


# --------------------------------------------------------------------------- #
# Authorization before parsing
# --------------------------------------------------------------------------- #
def test_unauthenticated_requests_are_denied_before_parsing(api, monkeypatch):
    client, _, _ = api
    monkeypatch.delenv("CLINIC_RECALL_STAFF_CLINIC_ID", raising=False)

    async def _must_not_parse(request):  # pragma: no cover - failure marker
        raise AssertionError("multipart parsing ran before authorization")

    monkeypatch.setattr(clinic_recall, "_read_csv_multipart", _must_not_parse)
    for url in (
        "/api/v1/clinic-recall/imports/csv/preview",
        "/api/v1/clinic-recall/imports/csv/impb-x/approve",
    ):
        response = _post_csv(client, url, VALID_BYTES, PREVIEW_FIELDS)
        assert response.status_code == 403
    assert client.get("/api/v1/clinic-recall/imports/csv").status_code == 403
    assert client.get("/api/v1/clinic-recall/imports/csv/config").status_code == 403


def test_disconnect_after_spool_cleanup_creates_no_database_state(api, monkeypatch):
    client, factory, _ = api

    async def _disconnected(self):
        return True

    monkeypatch.setattr(Request, "is_disconnected", _disconnected)
    response = _preview(client)
    assert response.status_code == 400
    assert response.json()["detail"] == "request_disconnected"
    with factory() as session:
        assert session.execute(select(func.count()).select_from(ImportBatch)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(Patient)).scalar_one() == 0


def test_disabled_feature_rejects_preview_and_approve(api, monkeypatch):
    client, _, _ = api
    monkeypatch.delenv("CLINIC_RECALL_CSV_IMPORT_ENABLED", raising=False)
    assert _preview(client).status_code == 403
    assert _approve(client, "impb-x").status_code == 403
    config = client.get("/api/v1/clinic-recall/imports/csv/config")
    assert config.status_code == 200
    assert config.json()["enabled"] is False
    assert config.json()["consent_authority_available"] is False


def test_client_clinic_id_is_rejected(api):
    client, _, _ = api
    response = client.post(
        "/api/v1/clinic-recall/imports/csv/preview",
        files={"file": ("upload.csv", VALID_BYTES, "text/csv")},
        data={**PREVIEW_FIELDS, "clinic_id": "clinic-b"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "clinic_id is derived server-side"


def test_operator_endpoints_reject_staff(api):
    client, _, _ = api
    assert client.get("/api/v1/clinic-recall/operator/import-matches").status_code == 403
    assert (
        client.post(
            "/api/v1/clinic-recall/operator/import-matches/imr-x/resolve",
            json={"action": "dismiss"},
        ).status_code
        == 403
    )


# --------------------------------------------------------------------------- #
# Upload and parser security
# --------------------------------------------------------------------------- #
def test_missing_file_unknown_field_and_bad_metadata(api):
    client, _, _ = api
    no_file = client.post("/api/v1/clinic-recall/imports/csv/preview", data=PREVIEW_FIELDS)
    assert no_file.status_code == 422
    unknown = client.post(
        "/api/v1/clinic-recall/imports/csv/preview",
        files={"file": ("upload.csv", VALID_BYTES, "text/csv")},
        data={**PREVIEW_FIELDS, "surprise": "x"},
    )
    assert unknown.status_code == 422
    bad_source = client.post(
        "/api/v1/clinic-recall/imports/csv/preview",
        files={"file": ("upload.csv", VALID_BYTES, "text/csv")},
        data={"source_system": "fax", "export_at": EXPORT_AT},
    )
    assert bad_source.status_code == 422
    naive_export = client.post(
        "/api/v1/clinic-recall/imports/csv/preview",
        files={"file": ("upload.csv", VALID_BYTES, "text/csv")},
        data={"source_system": "csv", "export_at": "2026-07-25T18:00:00"},
    )
    assert naive_export.status_code == 422


def test_multipart_metadata_is_phase_exact_unique_and_bounded(api):
    client, _, _ = api
    duplicate = client.post(
        "/api/v1/clinic-recall/imports/csv/preview",
        files=[
            ("source_system", (None, "csv")),
            ("source_system", (None, "cliniko")),
            ("export_at", (None, EXPORT_AT)),
            ("file", ("upload.csv", VALID_BYTES, "text/csv")),
        ],
    )
    assert duplicate.status_code == 422
    assert duplicate.json()["detail"] == "duplicate form field"

    approval_only_preview = client.post(
        "/api/v1/clinic-recall/imports/csv/preview",
        files={"file": ("upload.csv", VALID_BYTES, "text/csv")},
        data={**PREVIEW_FIELDS, "attestation_version": "csv-attest-v1"},
    )
    assert approval_only_preview.status_code == 422
    assert approval_only_preview.json()["detail"] == "unknown form field"

    oversized_timestamp = client.post(
        "/api/v1/clinic-recall/imports/csv/preview",
        files={"file": ("upload.csv", VALID_BYTES, "text/csv")},
        data={"source_system": "csv", "export_at": "2" * 300},
    )
    assert oversized_timestamp.status_code == 422
    assert oversized_timestamp.json()["detail"] == "form field exceeds the size limit"


def test_oversize_content_length_is_rejected(api, monkeypatch):
    client, _, _ = api
    from src.clinic_recall.sync import csv_adapter

    response = client.post(
        "/api/v1/clinic-recall/imports/csv/preview",
        content=b"x",
        headers={
            "content-length": str(csv_adapter.MAX_BYTES + 1024 * 1024),
            "content-type": "multipart/form-data; boundary=x",
        },
    )
    assert response.status_code == 413


def test_extension_and_content_type_are_hints_only(api):
    client, _, _ = api
    # A spoofed extension/MIME with valid CSV bytes still previews on content.
    response = client.post(
        "/api/v1/clinic-recall/imports/csv/preview",
        files={"file": ("export.xlsx.csv", VALID_BYTES, "application/vnd.ms-excel")},
        data=PREVIEW_FIELDS,
    )
    assert response.status_code == 200
    # And a .csv name with non-CSV bytes fails on content, not extension.
    junk = client.post(
        "/api/v1/clinic-recall/imports/csv/preview",
        files={"file": ("fine.csv", b"\xff\xfe not a csv", "text/csv")},
        data=PREVIEW_FIELDS,
    )
    assert junk.status_code == 200
    assert junk.json()["importable"] is False


def test_filename_never_appears_in_responses_or_history(api):
    client, _, _ = api
    response = client.post(
        "/api/v1/clinic-recall/imports/csv/preview",
        files={
            "file": (
                "confidential-patient-list-2026.csv",
                VALID_BYTES,
                "text/csv",
            )
        },
        data=PREVIEW_FIELDS,
    )
    assert response.status_code == 200
    assert "confidential-patient-list" not in response.text
    history = client.get("/api/v1/clinic-recall/imports/csv")
    assert "confidential-patient-list" not in history.text


def test_large_spooled_upload_is_parsed_and_disposed(api):
    client, _, _ = api
    header = "appointment_source_ref,patient_source_ref,patient_name,status,start_at\n"
    rows = "".join(
        f"APPT-BIG-{i},PAT-BIG-{i},Test Patient Big {i},missed,2026-06-20T09:00:00+00:00\n"
        for i in range(30_000)
    )
    data = (header + rows).encode()
    assert len(data) > 2 * 1024 * 1024  # exercises the disk spool path
    tmp_before = set(Path(tempfile.gettempdir()).iterdir())
    response = _preview(client, data)
    tmp_after = set(Path(tempfile.gettempdir()).iterdir())
    assert response.status_code == 200
    assert response.json()["importable"] is True
    assert response.json()["batch"]["total_rows"] == 30_000
    assert tmp_after == tmp_before  # no spool or raw artifact remains


def test_no_raw_values_in_error_responses(api):
    client, _, _ = api
    data = (FIXTURES / "invalid_values.csv").read_bytes()
    response = _preview(client, data)
    assert response.status_code == 200
    body = response.text
    for marker in ("not-a-phone", "not-an-email", "not-a-decimal", "Tau", "Upsilon"):
        assert marker not in body
    payload = response.json()
    assert payload["importable"] is False
    assert payload["errors"]
    for error in payload["errors"]:
        assert set(error) == {"reason", "field", "record", "line"}


def test_unexpected_preview_failure_is_minimized(api, monkeypatch, caplog):
    client, _, _ = api
    from src.clinic_recall.sync import csv_import as csv_import_module

    private_marker = "PAT-PR08-PRIVATE-FAILURE-VALUE"

    def _fail(*args, **kwargs):
        raise RuntimeError(private_marker)

    monkeypatch.setattr(csv_import_module, "preview_csv_import", _fail)
    response = _preview(client)
    assert response.status_code == 500
    assert response.json() == {"detail": "csv_import_failed"}
    assert private_marker not in response.text
    assert private_marker not in caplog.text


# --------------------------------------------------------------------------- #
# Preview/approval workflow integrity
# --------------------------------------------------------------------------- #
def test_full_preview_approve_history_and_onboarding_flow(api):
    client, factory, _ = api
    preview = _preview(client)
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["importable"] is True
    assert payload["batch"]["total_rows"] == 4
    assert payload["batch"]["patient_count"] == 3
    batch_id = payload["batch"]["id"]
    for marker in RAW_MARKERS:
        assert marker not in preview.text

    onboarding_before = client.get("/api/v1/clinic-recall/onboarding")
    # clinic-a has no consent_policy: legacy clinics report onboarding complete.

    approve = _approve(client, batch_id)
    assert approve.status_code == 200, approve.text
    approved = approve.json()
    assert approved["replayed"] is False
    assert approved["batch"]["state"] == "completed"
    assert approved["batch"]["patients_inserted"] == 3
    assert approved["batch"]["appointments_inserted"] == 4
    assert approved["batch"]["consent_granted_count"] == 0  # no approved age policy
    assert approved["batch"]["consent_authority_granted"] is False

    with factory() as session:
        assert session.execute(select(func.count()).select_from(Patient)).scalar_one() == 3
        assert session.execute(select(func.count()).select_from(Appointment)).scalar_one() == 4
        for model in (Campaign, OutreachJob, ExternalEffect):
            assert session.execute(select(func.count()).select_from(model)).scalar_one() == 0

    history = client.get("/api/v1/clinic-recall/imports/csv")
    assert history.status_code == 200
    assert [b["id"] for b in history.json()["batches"]] == [batch_id]
    detail = client.get(f"/api/v1/clinic-recall/imports/csv/{batch_id}")
    assert detail.status_code == 200
    assert detail.json()["state"] == "completed"

    replay = _approve(client, batch_id)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True

    assert onboarding_before.status_code == 200


def test_changed_bytes_approval_is_bounded_409(api):
    client, factory, _ = api
    preview = _preview(client)
    batch_id = preview.json()["batch"]["id"]
    mismatch = _approve(client, batch_id, CHANGED_BYTES)
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == "file_hash_mismatch"
    with factory() as session:
        assert session.execute(select(func.count()).select_from(Patient)).scalar_one() == 0


def test_attestation_is_required_for_approval(api):
    client, _, _ = api
    batch_id = _preview(client).json()["batch"]["id"]
    unconfirmed = _approve(
        client,
        batch_id,
        VALID_BYTES,
        fields={**APPROVE_FIELDS, "confirm_clinic_authority": "false"},
    )
    assert unconfirmed.status_code == 422
    assert unconfirmed.json()["detail"] == "attestation_invalid"
    stale_version = _approve(
        client,
        batch_id,
        VALID_BYTES,
        fields={**APPROVE_FIELDS, "attestation_version": "csv-attest-v0"},
    )
    assert stale_version.status_code == 422
    wrong_export = _approve(
        client,
        batch_id,
        VALID_BYTES,
        fields={**APPROVE_FIELDS, "export_at": "2026-07-25T19:00:00+00:00"},
    )
    assert wrong_export.status_code == 409
    assert wrong_export.json()["detail"] == "source_metadata_mismatch"


def test_cross_tenant_batch_is_bounded_not_found(api, monkeypatch):
    client, _, _ = api
    batch_id = _preview(client).json()["batch"]["id"]
    monkeypatch.setenv("CLINIC_RECALL_STAFF_CLINIC_ID", "clinic-b")
    other = TestClient(_app())
    denied = _post_csv(
        other,
        f"/api/v1/clinic-recall/imports/csv/{batch_id}/approve",
        VALID_BYTES,
        APPROVE_FIELDS,
    )
    assert denied.status_code == 404
    assert denied.json()["detail"] == "batch_not_found"
    detail = other.get(f"/api/v1/clinic-recall/imports/csv/{batch_id}")
    assert detail.status_code == 404
    history = other.get("/api/v1/clinic-recall/imports/csv")
    assert history.json()["batches"] == []


# --------------------------------------------------------------------------- #
# Matching review workflow (operator-only, synthetic snapshots)
# --------------------------------------------------------------------------- #
def _operator_client(monkeypatch):
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ROLES", "operator")
    return TestClient(_app())


def test_matching_disabled_creates_no_reviews(api, monkeypatch):
    client, factory, _ = api
    batch_id = _preview(client).json()["batch"]["id"]
    assert _approve(client, batch_id).status_code == 200
    operator = _operator_client(monkeypatch)
    reviews = operator.get("/api/v1/clinic-recall/operator/import-matches")
    assert reviews.status_code == 200
    assert reviews.json()["reviews"] == []


def test_matching_enabled_flows_to_operator_review_and_resolution(api, monkeypatch):
    client, factory, _ = api
    monkeypatch.setenv("CLINIC_RECALL_CSV_MATCHING_ENABLED", "true")

    def _candidates(clinic_id, refs):
        return {
            "PAT-PR08-001": (),  # zero -> unmatched
            "PAT-PR08-002": (
                ProviderPatientSnapshot(provider=SourceSystem.CLINIKO, source_ref="PAT-PR08-002"),
                ProviderPatientSnapshot(provider=SourceSystem.CLINIKO, source_ref="PAT-PR08-002"),
            ),  # multiple -> ambiguous
            "PAT-PR08-003": (
                ProviderPatientSnapshot(provider=SourceSystem.CLINIKO, source_ref="PAT-PR08-003"),
            ),  # exactly one -> linked (auto_link enabled)
        }

    monkeypatch.setattr(clinic_recall, "_csv_match_candidates", _candidates)
    monkeypatch.setattr(
        clinic_recall,
        "_csv_match_candidates_for_review",
        lambda clinic_id, provider, source_ref: _candidates(clinic_id, (source_ref,)).get(
            source_ref, ()
        ),
    )
    batch_id = _preview(client).json()["batch"]["id"]
    assert _approve(client, batch_id).status_code == 200

    operator = _operator_client(monkeypatch)
    listing = operator.get("/api/v1/clinic-recall/operator/import-matches")
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["unmatched_count"] == 1
    assert payload["ambiguous_count"] == 1
    states = {review["state"] for review in payload["reviews"]}
    assert states == {"unmatched", "ambiguous", "linked"}
    # No provider payloads or raw candidate lists in the response.
    for review in payload["reviews"]:
        assert set(review) == {
            "id",
            "import_batch_id",
            "provider",
            "strategy",
            "strategy_version",
            "state",
            "candidate_count",
            "reason",
            "resolved_by",
            "resolved_at",
            "created_at",
        }

    ambiguous = next(r for r in payload["reviews"] if r["state"] == "ambiguous")
    refreshed = operator.post(
        f"/api/v1/clinic-recall/operator/import-matches/{ambiguous['id']}/refresh"
    )
    assert refreshed.status_code == 200
    options = refreshed.json()["candidates"]
    assert len(options) == 2
    assert "PAT-PR08-002" not in refreshed.text

    tampered = operator.post(
        f"/api/v1/clinic-recall/operator/import-matches/{ambiguous['id']}/resolve",
        json={
            "action": "link",
            "candidate_token": options[0]["token"][:-1]
            + ("0" if options[0]["token"][-1] != "0" else "1"),
        },
    )
    assert tampered.status_code == 409
    assert tampered.json()["detail"] == "candidate_mismatch"

    resolved = operator.post(
        f"/api/v1/clinic-recall/operator/import-matches/{ambiguous['id']}/resolve",
        json={"action": "link", "candidate_token": options[0]["token"]},
    )
    assert resolved.status_code == 200
    assert resolved.json()["state"] == "linked"

    unmatched = next(r for r in payload["reviews"] if r["state"] == "unmatched")
    dismissed = operator.post(
        f"/api/v1/clinic-recall/operator/import-matches/{unmatched['id']}/resolve",
        json={"action": "dismiss"},
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["state"] == "dismissed"


def test_link_resolution_requires_matching_switch(api, monkeypatch):
    client, factory, _ = api
    monkeypatch.setenv("CLINIC_RECALL_CSV_MATCHING_ENABLED", "true")
    monkeypatch.setattr(
        clinic_recall,
        "_csv_match_candidates",
        lambda clinic_id, refs: {
            ref: (ProviderPatientSnapshot(provider=SourceSystem.CLINIKO, source_ref=ref),)
            for ref in refs
        },
    )
    batch_id = _preview(client).json()["batch"]["id"]
    assert _approve(client, batch_id).status_code == 200
    operator = _operator_client(monkeypatch)
    # All exact matches auto-linked; disable matching and prove link is gated.
    monkeypatch.delenv("CLINIC_RECALL_CSV_MATCHING_ENABLED")
    gated = operator.post(
        "/api/v1/clinic-recall/operator/import-matches/imr-any/resolve",
        json={"action": "link", "candidate_token": "x" * 80},
    )
    assert gated.status_code == 403
    # Dismiss remains available while matching is disabled.
    listing = operator.get("/api/v1/clinic-recall/operator/import-matches")
    linked = [r for r in listing.json()["reviews"] if r["state"] == "linked"]
    assert len(linked) == 3
