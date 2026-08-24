"""Interactive, no-echo launcher for an approved Cliniko Stage 8 read."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from ..config import ClinikoConfig, build_cliniko_config
from .cliniko_capability import (
    cliniko_config_fingerprint,
    fixture_capability_profile,
    seal_profile,
)
from .cliniko_sandbox_probe import main as probe_main

_IDENTIFIER_PATTERN = re.compile(r"[1-9][0-9]*")
_TEXT_PATTERN = re.compile(r"[^\x00-\x1f\x7f]{1,128}")


class SandboxLauncherError(ValueError):
    """Bounded launcher validation failure raised before provider access."""


@dataclass(frozen=True)
class Stage8Inputs:
    """No-echo operator inputs; sensitive values are excluded from repr."""

    controller_approver: str = field(repr=False)
    platform_approver: str = field(repr=False)
    sandbox_identity: str = field(repr=False)
    cleanup_owner: str = field(repr=False)
    expires_at: datetime
    shard: str
    user_agent: str = field(repr=False)
    api_key: str = field(repr=False)
    patient_id: str = field(repr=False)
    appointment_id: str = field(repr=False)
    business_id: str = field(repr=False)
    practitioner_id: str = field(repr=False)
    appointment_type_id: str = field(repr=False)
    updated_after: datetime
    availability_from: date
    availability_to: date

    def __post_init__(self) -> None:
        for value in (
            self.controller_approver,
            self.platform_approver,
            self.sandbox_identity,
            self.cleanup_owner,
        ):
            if _TEXT_PATTERN.fullmatch(value) is None:
                raise SandboxLauncherError("approval_identity")
        for value in (
            self.patient_id,
            self.appointment_id,
            self.business_id,
            self.practitioner_id,
            self.appointment_type_id,
        ):
            if _IDENTIFIER_PATTERN.fullmatch(value) is None:
                raise SandboxLauncherError("synthetic_identifier")
        for name in ("expires_at", "updated_after"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise SandboxLauncherError(name)
        if self.expires_at <= datetime.now(UTC):
            raise SandboxLauncherError("expires_at")
        window_days = (self.availability_to - self.availability_from).days
        if not 0 <= window_days <= 7:
            raise SandboxLauncherError("availability_window")


@dataclass(frozen=True)
class PreparedStage8:
    """Private artifact paths and in-memory configuration for one run."""

    evidence_root: Path
    approval_path: Path
    manifest_path: Path
    ledger_path: Path
    profile_path: Path
    approval_sha256: str
    fixture_profile_sha256: str
    config: ClinikoConfig = field(repr=False)


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _write_private_new(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(descriptor)
    except OSError:
        raise SandboxLauncherError("private_artifact") from None
    finally:
        if "descriptor" in locals():
            try:
                os.close(descriptor)
            except OSError:
                pass


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        raise SandboxLauncherError("source_identity") from None


def _git_head(repository_root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise SandboxLauncherError("source_identity") from None


def prepare_stage8(
    inputs: Stage8Inputs,
    *,
    evidence_root: Path,
    now: datetime,
) -> PreparedStage8:
    """Freeze private approval and manifest artifacts without provider I/O."""
    repository_root = Path(__file__).resolve().parents[3]
    evidence_root = evidence_root.resolve()
    private_base = evidence_root
    while not private_base.exists():
        private_base = private_base.parent
    created: list[Path] = []
    try:
        current = private_base
        for component in evidence_root.relative_to(private_base).parts:
            current = current / component
            current.mkdir(mode=0o700)
            current.chmod(0o700)
            created.append(current)
    except (FileExistsError, OSError, ValueError):
        raise SandboxLauncherError("evidence_root") from None
    if not created or created[-1] != evidence_root:
        raise SandboxLauncherError("evidence_root")
    source_commit = _git_head(repository_root)
    client_path = Path(__file__).with_name("cliniko_client.py")
    adapter_path = Path(__file__).with_name("cliniko_adapter.py")
    plan_path = repository_root / "docs/clinic-recall-mvp-integration-plan.md"
    client_sha256 = _sha256_file(client_path)
    adapter_sha256 = _sha256_file(adapter_path)
    config = build_cliniko_config(
        api_key=inputs.api_key,
        shard=inputs.shard,
        user_agent=inputs.user_agent,
        timeout_seconds=5,
        per_page=1,
        max_pages=2,
        max_items=4,
    )
    config_fingerprint = cliniko_config_fingerprint(config)
    approval_payload: dict[str, object] = {
        "adapter_sha256": adapter_sha256,
        "approved_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "cleanup_owner": inputs.cleanup_owner,
        "controller_approver": inputs.controller_approver,
        "expires_at": inputs.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "hard_attempt_cap": 20,
        "plan_sha256": _sha256_file(plan_path),
        "planned_attempts": 16,
        "platform_approver": inputs.platform_approver,
        "sandbox_identity": inputs.sandbox_identity,
        "schema_version": 1,
        "scope": "cliniko_sandbox_read",
        "source_commit": source_commit,
        "synthetic_only": True,
        "uk_shard": inputs.shard,
    }
    approval_bytes = _canonical_bytes(approval_payload)
    approval_sha256 = hashlib.sha256(approval_bytes).hexdigest()
    fixture_profile = fixture_capability_profile(
        generated_at=now,
        source_commit=source_commit,
        client_sha256=client_sha256,
        adapter_sha256=adapter_sha256,
        config_fingerprint=config_fingerprint,
    )
    fixture_profile_sha256 = seal_profile(fixture_profile).sha256
    manifest_payload: dict[str, object] = {
        "adapter_sha256": adapter_sha256,
        "appointment_id": inputs.appointment_id,
        "appointment_type_id": inputs.appointment_type_id,
        "approval_evidence_sha256": approval_sha256,
        "availability_from": inputs.availability_from.isoformat(),
        "availability_to": inputs.availability_to.isoformat(),
        "business_id": inputs.business_id,
        "client_sha256": client_sha256,
        "config_fingerprint": config_fingerprint,
        "expected_shard": inputs.shard,
        "expires_at": inputs.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "fixture_profile_sha256": fixture_profile_sha256,
        "patient_id": inputs.patient_id,
        "planned_attempts": 16,
        "practitioner_id": inputs.practitioner_id,
        "schema_version": 1,
        "source_commit": source_commit,
        "synthetic_only": True,
        "updated_after": inputs.updated_after.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    approval_path = evidence_root / "approval.json"
    manifest_path = evidence_root / "read-manifest.json"
    _write_private_new(approval_path, approval_bytes)
    _write_private_new(manifest_path, _canonical_bytes(manifest_payload))
    return PreparedStage8(
        evidence_root=evidence_root,
        approval_path=approval_path,
        manifest_path=manifest_path,
        ledger_path=evidence_root / "request-ledger.jsonl",
        profile_path=evidence_root / "capability-profile.json",
        approval_sha256=approval_sha256,
        fixture_profile_sha256=fixture_profile_sha256,
        config=config,
    )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return parsed.astimezone(UTC)


def _collect_inputs(prompt: Callable[[str], str]) -> Stage8Inputs:
    try:
        return Stage8Inputs(
            controller_approver=prompt("Named clinic controller/DPO approver: ").strip(),
            platform_approver=prompt("Named platform owner approver: ").strip(),
            sandbox_identity=prompt("Sandbox account identity (non-secret label): ").strip(),
            cleanup_owner=prompt("Cleanup owner: ").strip(),
            expires_at=_parse_utc(prompt("Approval expiry (RFC3339): ").strip()),
            shard=prompt("Approved UK shard (uk1/uk2/uk3): ").strip(),
            user_agent=prompt("Cliniko User-Agent NAME (contact-email): ").strip(),
            api_key=prompt("Cliniko API key (hidden, not persisted): ").strip(),
            patient_id=prompt("Synthetic patient ID: ").strip(),
            appointment_id=prompt("Synthetic appointment ID: ").strip(),
            business_id=prompt("Synthetic business ID: ").strip(),
            practitioner_id=prompt("Synthetic practitioner ID: ").strip(),
            appointment_type_id=prompt("Synthetic appointment type ID: ").strip(),
            updated_after=_parse_utc(prompt("Updated-after cursor (RFC3339): ").strip()),
            availability_from=date.fromisoformat(prompt("Availability from (YYYY-MM-DD): ").strip()),
            availability_to=date.fromisoformat(prompt("Availability to (YYYY-MM-DD): ").strip()),
        )
    except (EOFError, TypeError, ValueError):
        raise SandboxLauncherError("operator_input") from None


def main(
    argv: Sequence[str] | None = None,
    *,
    prompt: Callable[[str], str] = getpass.getpass,
    probe: Callable[..., int] = probe_main,
) -> int:
    """Collect hidden inputs, freeze private evidence, and run Stage 8."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=Path(".private-evidence/clinic-recall-pr05/stage8"),
    )
    parser.add_argument("--confirm-stage8-approved", required=True, action="store_true")
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    try:
        inputs = _collect_inputs(prompt)
        prepared = prepare_stage8(inputs, evidence_root=args.evidence_root, now=now)
        return probe(
            [
                "sandbox-read",
                "--manifest",
                str(prepared.manifest_path),
                "--ledger",
                str(prepared.ledger_path),
                "--profile-output",
                str(prepared.profile_path),
                "--approval-sha256",
                prepared.approval_sha256,
                "--confirm-sandbox-read",
            ],
            config_loader=lambda: prepared.config,
        )
    except SandboxLauncherError as error:
        print(
            json.dumps(
                {"complete": False, "reason_code": str(error)},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())