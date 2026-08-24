from __future__ import annotations

import json
import stat
from datetime import UTC, date, datetime
from pathlib import Path

from src.clinic_recall.sync.cliniko_sandbox_launcher import (
    Stage8Inputs,
    prepare_stage8,
)


def test_launcher_prepares_private_artifacts_without_persisting_api_key(
    tmp_path: Path,
) -> None:
    api_key = "synthetic-private-launcher-key-uk2"
    inputs = Stage8Inputs(
        controller_approver="Synthetic Controller",
        platform_approver="Synthetic Platform Owner",
        sandbox_identity="synthetic-uk2-trial",
        cleanup_owner="Synthetic Cleanup Owner",
        expires_at=datetime(2027, 7, 25, tzinfo=UTC),
        shard="uk2",
        user_agent="Clinic Recall Launcher Test (launcher@example.invalid)",
        api_key=api_key,
        patient_id="900000001",
        appointment_id="910000001",
        business_id="920000001",
        practitioner_id="930000001",
        appointment_type_id="940000001",
        updated_after=datetime(2026, 7, 1, tzinfo=UTC),
        availability_from=date(2026, 8, 1),
        availability_to=date(2026, 8, 7),
    )
    root = tmp_path / "stage8"

    prepared = prepare_stage8(
        inputs,
        evidence_root=root,
        now=datetime(2026, 7, 24, 12, tzinfo=UTC),
    )

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(prepared.approval_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(prepared.manifest_path.stat().st_mode) == 0o600
    manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    approval = json.loads(prepared.approval_path.read_text(encoding="utf-8"))
    all_private_bytes = prepared.manifest_path.read_bytes() + prepared.approval_path.read_bytes()
    assert api_key.encode() not in all_private_bytes
    assert "api_key" not in manifest
    assert approval["scope"] == "cliniko_sandbox_read"
    assert manifest["approval_evidence_sha256"] == prepared.approval_sha256
    assert manifest["fixture_profile_sha256"] == prepared.fixture_profile_sha256
    assert api_key not in repr(prepared)
    assert api_key not in repr(inputs)


def test_launcher_creates_every_private_evidence_directory_as_0700(
    tmp_path: Path,
) -> None:
    inputs = Stage8Inputs(
        controller_approver="Synthetic Controller",
        platform_approver="Synthetic Platform Owner",
        sandbox_identity="synthetic-uk2-trial",
        cleanup_owner="Synthetic Cleanup Owner",
        expires_at=datetime(2027, 7, 25, tzinfo=UTC),
        shard="uk2",
        user_agent="Clinic Recall Launcher Test (launcher@example.invalid)",
        api_key="synthetic-private-launcher-key-uk2",
        patient_id="900000001",
        appointment_id="910000001",
        business_id="920000001",
        practitioner_id="930000001",
        appointment_type_id="940000001",
        updated_after=datetime(2026, 7, 1, tzinfo=UTC),
        availability_from=date(2026, 8, 1),
        availability_to=date(2026, 8, 7),
    )
    base = tmp_path / "private"
    root = base / "clinic-recall-pr05" / "stage8"

    prepare_stage8(
        inputs,
        evidence_root=root,
        now=datetime(2026, 7, 24, 12, tzinfo=UTC),
    )

    for path in (base, base / "clinic-recall-pr05", root):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700