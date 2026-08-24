"""Controlled CSV import service (PR-08): preview, approval, atomic import.

Both entry points take an already-materialized upload (parsing happens outside
database scope; the route disposes the request spool first) and run inside the
caller's transaction. On any :class:`CsvImportError` the caller must roll
back — the service never leaves a partial import to commit.

Authority boundaries (D3): the client never supplies clinic, actor, hashes,
counts, policy, or completion; approval binds to the exact preview bytes; and
a completed import grants no outreach, booking, or enrollment authority.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import clinic_scope, tenant_select
from ..enums import AuditAction, ImportBatchState, MatchStrategy, SourceSystem
from ..messaging.audit import audit_action
from ..models import ImportBatch
from ..rights import SubjectFrozenError, SubjectKeyring
from .csv_adapter import CsvMaterialization, CsvSafeError
from .csv_consent import CsvImportPolicy, apply_consent_policy, grantable_channels
from .csv_matching import SourceMatchError, create_patient_source_link
from .upsert import FlagMerge, SyncResult, find_source_patient, upsert_source

_LIVE_STATES = (ImportBatchState.PREVIEW_VALID, ImportBatchState.COMPLETED)

# Closed vocabulary of bounded import failure reasons (safe for responses).
_REASONS = frozenset(
    {
        "batch_not_found",
        "not_importable",
        "preview_expired",
        "file_hash_mismatch",
        "source_metadata_mismatch",
        "attestation_invalid",
        "subject_frozen",
        "source_link_conflict",
        "policy_mismatch",
        "invalid_state",
        "source_system_not_allowed",
        "export_time_invalid",
        "import_disabled",
    }
)


class CsvImportError(RuntimeError):
    """A bounded, reason-coded import failure. Never carries raw file values."""

    def __init__(self, reason: str) -> None:
        if reason not in _REASONS:
            raise ValueError(f"unknown import failure reason: {reason!r}")
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class CsvImportAttestation:
    """Structured staff attestation supplied with an approval request.

    The server derives clinic, actor, approval time, and policy version/hash;
    these fields carry only what the staff member actively selected.
    """

    source_system: SourceSystem
    export_at: datetime
    attestation_version: str
    attested_channels: tuple[str, ...]
    confirm_clinic_authority: bool


@dataclass(frozen=True)
class PreviewResult:
    """A preview outcome: metadata-only batch plus response-only safe errors."""

    batch: ImportBatch
    errors: tuple[CsvSafeError, ...]
    created: bool


@dataclass(frozen=True)
class ApprovalResult:
    """An approval outcome; ``replayed`` marks an idempotent completed replay."""

    batch: ImportBatch
    sync: SyncResult | None
    replayed: bool


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _batch_utc(value: datetime | None) -> datetime | None:
    """Normalize a database timestamp for comparison (SQLite drops tzinfo)."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def preview_csv_import(
    session: Session,
    clinic_id: str,
    *,
    materialization: CsvMaterialization,
    source_system: SourceSystem,
    export_at: datetime,
    actor: str,
    now: datetime,
    policy: CsvImportPolicy,
    upload_disposed_at: datetime,
) -> PreviewResult:
    """Record metadata-only preview provenance for one materialized upload.

    Creates no patient, appointment, source-link, review, campaign, or job
    rows. Safe row errors are returned to the authorized caller only; the
    database keeps aggregate reason counts and hashes.
    """
    now = _aware(now, "now")
    export_at = _aware(export_at, "export_at")
    upload_disposed_at = _aware(upload_disposed_at, "upload_disposed_at")
    if not actor.strip():
        raise ValueError("actor is required")
    if source_system not in policy.allowed_source_systems:
        raise CsvImportError("source_system_not_allowed")
    if export_at > now:
        raise CsvImportError("export_time_invalid")

    with clinic_scope(session, clinic_id):
        existing = _live_batch(session, materialization)
        if existing is not None:
            if existing.state == ImportBatchState.COMPLETED:
                return PreviewResult(batch=existing, errors=(), created=False)
            same_metadata = (
                existing.source_system == source_system
                and _batch_utc(existing.export_at) == export_at
            )
            expired = _batch_utc(existing.preview_expires_at) <= now
            if same_metadata and not expired:
                return PreviewResult(batch=existing, errors=materialization.errors, created=False)
            existing.state = ImportBatchState.EXPIRED if expired else ImportBatchState.SUPERSEDED
            session.flush()

        batch = ImportBatch(
            id=f"impb-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            state=(
                ImportBatchState.PREVIEW_VALID
                if materialization.valid
                else ImportBatchState.PREVIEW_INVALID
            ),
            file_sha256=materialization.file_sha256,
            validation_summary_sha256=materialization.validation_summary_sha256,
            schema_version=materialization.schema_version,
            source_system=source_system,
            export_at=export_at,
            preview_requested_at=now,
            preview_actor=actor,
            preview_expires_at=now + policy.preview_ttl,
            preview_upload_disposed_at=upload_disposed_at,
            consent_policy_version=policy.version,
            consent_policy_hash=policy.statement_hash,
            total_rows=materialization.total_rows,
            valid_row_count=materialization.valid_row_count,
            invalid_row_count=materialization.invalid_row_count,
            patient_count=materialization.patient_count,
            appointment_count=materialization.appointment_count,
            error_count=len(materialization.errors),
            error_reason_counts=dict(materialization.error_reason_counts) or None,
        )
        try:
            with session.begin_nested():
                session.add(batch)
        except IntegrityError:
            # A concurrent preview of the same bytes won the unique index.
            concurrent = _live_batch(session, materialization)
            if concurrent is None:
                raise
            return PreviewResult(batch=concurrent, errors=materialization.errors, created=False)
        audit_action(
            session,
            clinic_id,
            AuditAction.CSV_IMPORT_PREVIEW,
            batch.id,
            {
                "batch_id": batch.id,
                "file_sha256": materialization.file_sha256,
                "state": batch.state.value,
                "total_rows": materialization.total_rows,
                "error_count": len(materialization.errors),
                "occurred_at": now,
            },
            actor=actor,
        )
        session.flush()
        return PreviewResult(batch=batch, errors=materialization.errors, created=True)


def approve_csv_import(
    session: Session,
    clinic_id: str,
    batch_id: str,
    *,
    materialization: CsvMaterialization,
    attestation: CsvImportAttestation,
    actor: str,
    now: datetime,
    policy: CsvImportPolicy,
    keyring: SubjectKeyring,
    upload_disposed_at: datetime,
) -> ApprovalResult:
    """Approve a previewed batch with the same bytes and import atomically.

    Raises :class:`CsvImportError` (caller rolls back) on any mismatch,
    expiry, attestation failure, or frozen subject: zero rows import.
    A repeated approval of an identical completed batch replays safely.
    """
    now = _aware(now, "now")
    upload_disposed_at = _aware(upload_disposed_at, "upload_disposed_at")
    if not actor.strip():
        raise ValueError("actor is required")

    with clinic_scope(session, clinic_id):
        statement = tenant_select(ImportBatch).where(ImportBatch.id == batch_id)
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update()
        batch = session.execute(statement).scalar_one_or_none()
        if batch is None or batch.clinic_id != clinic_id:
            raise CsvImportError("batch_not_found")

        _validate_materialization(materialization, batch)
        if batch.state == ImportBatchState.COMPLETED:
            _validate_completed_replay(attestation, batch)
            return ApprovalResult(batch=batch, sync=None, replayed=True)
        if batch.state == ImportBatchState.PREVIEW_INVALID:
            raise CsvImportError("not_importable")
        if batch.state != ImportBatchState.PREVIEW_VALID:
            raise CsvImportError("invalid_state")

        if _batch_utc(batch.preview_expires_at) <= now:
            raise CsvImportError("preview_expired")
        if not materialization.valid:
            raise CsvImportError("not_importable")

        if (
            batch.consent_policy_version != policy.version
            or batch.consent_policy_hash != policy.statement_hash
        ):
            raise CsvImportError("policy_mismatch")
        _validate_attestation(attestation, batch, policy)

        grantable = grantable_channels(
            policy,
            attestation_version=attestation.attestation_version,
            attested_channels=attestation.attested_channels,
            export_at=_batch_utc(batch.export_at) or now,
            now=now,
        )
        evaluation = apply_consent_policy(materialization.patients, grantable=grantable)
        source = materialization.source()
        adjusted = type(source)(patients=evaluation.patients, appointments=source.appointments)
        try:
            sync = upsert_source(
                session,
                clinic_id,
                adjusted,
                keyring=keyring,
                flag_merge=FlagMerge.MONOTONIC,
                source_system=batch.source_system,
            )
        except SubjectFrozenError as exc:
            raise CsvImportError("subject_frozen") from exc

        try:
            for incoming in evaluation.patients:
                patient = find_source_patient(
                    session,
                    clinic_id,
                    incoming.source_ref,
                    batch.source_system,
                )
                if patient is None:
                    raise RuntimeError("imported patient identity did not resolve")
                evidence_hash = hashlib.sha256(
                    json.dumps(
                        {
                            "batch_id": batch.id,
                            "provider": batch.source_system.value,
                            "source_ref": incoming.source_ref,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                create_patient_source_link(
                    session,
                    clinic_id,
                    patient.id,
                    provider=batch.source_system,
                    source_ref=incoming.source_ref,
                    strategy=MatchStrategy.EXACT_SOURCE_REF,
                    evidence_hash=evidence_hash,
                    actor=actor,
                    now=now,
                    keyring=keyring,
                    import_batch_id=batch.id,
                )
        except SourceMatchError as exc:
            raise CsvImportError("source_link_conflict") from exc

        batch.state = ImportBatchState.COMPLETED
        batch.approved_at = now
        batch.approved_by = actor
        batch.approval_upload_disposed_at = upload_disposed_at
        batch.attestation_version = attestation.attestation_version
        batch.attested_channels = sorted(attestation.attested_channels)
        batch.consent_policy_version = policy.version
        batch.consent_policy_hash = policy.statement_hash
        batch.consent_authority_granted = evaluation.authority_granted
        batch.patients_inserted = sync.patients_inserted
        batch.patients_updated = sync.patients_updated
        batch.appointments_inserted = sync.appointments_inserted
        batch.appointments_updated = sync.appointments_updated
        batch.consent_granted_count = evaluation.granted_count
        batch.consent_unknown_count = evaluation.unknown_count
        batch.opt_out_count = evaluation.opt_out_count
        batch.completed_at = now
        audit_action(
            session,
            clinic_id,
            AuditAction.CSV_IMPORT_APPROVE,
            batch.id,
            {
                "batch_id": batch.id,
                "file_sha256": batch.file_sha256,
                "patients_inserted": sync.patients_inserted,
                "patients_updated": sync.patients_updated,
                "appointments_inserted": sync.appointments_inserted,
                "appointments_updated": sync.appointments_updated,
                "consent_granted": evaluation.granted_count,
                "consent_unknown": evaluation.unknown_count,
                "opt_outs": evaluation.opt_out_count,
                "occurred_at": now,
            },
            actor=actor,
        )
        session.flush()
        return ApprovalResult(batch=batch, sync=sync, replayed=False)


def list_import_batches(session: Session, clinic_id: str, *, limit: int = 20) -> list[ImportBatch]:
    """Most-recent-first aggregate import history for one clinic."""
    with clinic_scope(session, clinic_id):
        return list(
            session.execute(
                tenant_select(ImportBatch)
                .order_by(ImportBatch.created_at.desc(), ImportBatch.id)
                .limit(max(1, min(limit, 100)))
            ).scalars()
        )


def get_import_batch(session: Session, clinic_id: str, batch_id: str) -> ImportBatch | None:
    """One tenant-scoped batch, or ``None`` without cross-tenant existence leak."""
    with clinic_scope(session, clinic_id):
        batch = session.execute(
            tenant_select(ImportBatch).where(ImportBatch.id == batch_id)
        ).scalar_one_or_none()
        if batch is None or batch.clinic_id != clinic_id:
            return None
        return batch


def _live_batch(session: Session, materialization: CsvMaterialization) -> ImportBatch | None:
    return session.execute(
        tenant_select(ImportBatch).where(
            ImportBatch.file_sha256 == materialization.file_sha256,
            ImportBatch.schema_version == materialization.schema_version,
            ImportBatch.state.in_(_LIVE_STATES),
        )
    ).scalar_one_or_none()


def _validate_attestation(
    attestation: CsvImportAttestation, batch: ImportBatch, policy: CsvImportPolicy
) -> None:
    if attestation.confirm_clinic_authority is not True:
        raise CsvImportError("attestation_invalid")
    if attestation.attestation_version not in policy.attestation_versions:
        raise CsvImportError("attestation_invalid")
    if any(channel not in policy.channels for channel in attestation.attested_channels):
        raise CsvImportError("attestation_invalid")
    if attestation.source_system != batch.source_system:
        raise CsvImportError("source_metadata_mismatch")
    export_at = _aware(attestation.export_at, "attestation.export_at")
    if _batch_utc(batch.export_at) != export_at:
        raise CsvImportError("source_metadata_mismatch")


def _validate_materialization(materialization: CsvMaterialization, batch: ImportBatch) -> None:
    if materialization.file_sha256 != batch.file_sha256:
        raise CsvImportError("file_hash_mismatch")
    if (
        materialization.schema_version != batch.schema_version
        or materialization.validation_summary_sha256 != batch.validation_summary_sha256
        or materialization.total_rows != batch.total_rows
        or materialization.valid_row_count != batch.valid_row_count
        or materialization.invalid_row_count != batch.invalid_row_count
        or materialization.patient_count != batch.patient_count
        or materialization.appointment_count != batch.appointment_count
    ):
        raise CsvImportError("file_hash_mismatch")


def _validate_completed_replay(attestation: CsvImportAttestation, batch: ImportBatch) -> None:
    if attestation.confirm_clinic_authority is not True:
        raise CsvImportError("attestation_invalid")
    if attestation.source_system != batch.source_system:
        raise CsvImportError("source_metadata_mismatch")
    if _aware(attestation.export_at, "attestation.export_at") != _batch_utc(batch.export_at):
        raise CsvImportError("source_metadata_mismatch")
    if attestation.attestation_version != batch.attestation_version or sorted(
        attestation.attested_channels
    ) != sorted(batch.attested_channels or []):
        raise CsvImportError("attestation_invalid")
