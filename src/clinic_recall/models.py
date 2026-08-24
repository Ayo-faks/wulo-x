"""SQLAlchemy ORM models for the Clinic Recall data model (PRD section 9).

Every tenant-scoped table carries a non-null ``clinic_id`` foreign key. This is
the application-layer half of the per-clinic isolation invariant; the
database-layer half (PostgreSQL row-level security) is installed by the Alembic
migration. Both must hold — cross-clinic leakage is the highest-severity
failure mode for this product.

Enum columns store their lowercase string ``value`` (not the member name) so the
on-disk vocabulary matches the wire/JSON vocabulary on both PostgreSQL (native
enums) and SQLite (VARCHAR + CHECK, used only for offline tests).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum as _PyEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .enums import (
    AppointmentStatus,
    AuditAction,
    BookingActionStatus,
    BookingActionType,
    BookingWriteBackState,
    CallRecordingStatus,
    CampaignStatus,
    CampaignType,
    Channel,
    ClinicPhoneProvider,
    ClinicPhonePurpose,
    ClinicPhoneStatus,
    EscalationPriority,
    EscalationReason,
    EscalationStatus,
    ExternalEffectState,
    ExternalEffectType,
    HandoffAlternateState,
    HandoffDeliveryState,
    HandoffSeverity,
    IdentityEvidenceReason,
    IdentityEvidenceState,
    IdentityFactorResult,
    IdentityTier,
    ImportBatchState,
    ImportMatchReviewState,
    InboundCallStatus,
    InboundMessageStatus,
    InboundStaffTaskKind,
    InboundStaffTaskStatus,
    IncidentCategory,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
    InteractionDirection,
    InteractionIntent,
    InteractionOutcome,
    MatchStrategy,
    OutreachState,
    PilotProgrammeState,
    PromptProposalStatus,
    ProviderCallbackKind,
    ProviderCallbackReason,
    ProviderCallbackState,
    ReasonCode,
    RecordingConsentSource,
    RecordingConsentState,
    RecordingDeletionState,
    RightsRequestKind,
    RightsRequestState,
    RightsResidualCategory,
    RightsTargetAction,
    RightsTargetOwnerType,
    RightsTargetResource,
    RightsTargetState,
    RightsTargetSystem,
    SourceLinkState,
    SourceSystem,
)

# Tables that RLS must protect. The migration reads this list so the policy set
# and the schema can never drift apart.
TENANT_TABLES: tuple[str, ...] = (
    "patient",
    "identity_evidence",
    "identity_factor_attempt",
    "pilot_programme",
    "pilot_participant",
    "appointment",
    "availability_slot",
    "campaign",
    "cadence_cursor",
    "outreach_job",
    "interaction",
    "rights_request",
    "rights_target",
    "external_effect",
    "external_effect_handoff",
    "handoff_receipt",
    "provider_callback_receipt",
    "booking_action",
    "escalation",
    "inbound_call",
    "inbound_message",
    "inbound_staff_task",
    "call_record",
    "incident_report",
    "audit_log",
    "prompt_proposal",
    "import_batch",
    "patient_source_link",
    "import_match_review",
    "rights_alias_tombstone",
)

# The PostgreSQL session variable RLS policies read for the current tenant.
RLS_GUC = "app.clinic_id"


class Base(DeclarativeBase):
    """Declarative base for all Clinic Recall tables."""


class _UTCDateTime(sa.TypeDecorator[datetime]):
    """Keep UTC awareness when SQLite drops timezone metadata."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _enum(py_enum: type[_PyEnum], name: str) -> sa.Enum:
    """Build a SQLAlchemy Enum that persists the lowercase ``.value``."""
    return sa.Enum(
        py_enum,
        name=name,
        native_enum=True,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


def _pk() -> Mapped[str]:
    """A string primary key (external-friendly, e.g. ``clinic-phase0-uk``)."""
    return mapped_column(sa.String, primary_key=True)


def _clinic_fk() -> Mapped[str]:
    """A non-null ``clinic_id`` foreign key for tenant isolation."""
    return mapped_column(
        sa.String,
        sa.ForeignKey("clinic.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def _effect_token_default(context: Any) -> str:
    from .durable.callbacks import generate_effect_token

    clinic_id = str(context.get_current_parameters().get("clinic_id") or "")
    return generate_effect_token(clinic_id)


class TimestampMixin:
    """``created_at`` / ``updated_at`` columns maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


class Clinic(TimestampMixin, Base):
    """A clinic tenant. The root of every per-clinic data graph."""

    __tablename__ = "clinic"
    __table_args__ = (sa.UniqueConstraint("sms_number", name="uq_clinic_sms_number"),)

    id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(sa.String, nullable=False)
    sms_number: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    timezone: Mapped[str] = mapped_column(
        sa.String, nullable=False, server_default="Europe/London"
    )
    contact_hours: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    daily_caps: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="200")
    branding: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    consent_policy: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)


class ClinicIdentityMapping(TimestampMixin, Base):
    """Trusted identity-to-clinic mapping resolved before tenant scope is known."""

    __tablename__ = "clinic_identity_mapping"
    __table_args__ = (
        sa.UniqueConstraint("provider", "subject", name="uq_clinic_identity_mapping_provider_subject"),
        sa.UniqueConstraint("provider", "email", name="uq_clinic_identity_mapping_provider_email"),
        sa.Index("ix_clinic_identity_mapping_clinic_status", "clinic_id", "status"),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = mapped_column(
        sa.String,
        sa.ForeignKey("clinic.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(
        sa.String, nullable=False, server_default="aad", default="aad", index=True
    )
    subject: Mapped[str | None] = mapped_column(sa.String, nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(sa.String, nullable=True, index=True)
    roles: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(sa.String, nullable=False, server_default="active")


class ClinicPhoneNumber(TimestampMixin, Base):
    """Trusted provider number route resolved before tenant scope is known."""

    __tablename__ = "clinic_phone_number"
    __table_args__ = (
        sa.UniqueConstraint("provider", "phone_number", name="uq_clinic_phone_provider_number"),
        sa.Index("ix_clinic_phone_number_clinic_status", "clinic_id", "status"),
        sa.Index("ix_clinic_phone_number_provider_purpose", "provider", "purpose", "status"),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = mapped_column(
        sa.String,
        sa.ForeignKey("clinic.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    phone_number: Mapped[str] = mapped_column(sa.String, nullable=False)
    provider: Mapped[ClinicPhoneProvider] = mapped_column(
        _enum(ClinicPhoneProvider, "clinic_phone_provider"), nullable=False
    )
    purpose: Mapped[ClinicPhonePurpose] = mapped_column(
        _enum(ClinicPhonePurpose, "clinic_phone_purpose"), nullable=False
    )
    status: Mapped[ClinicPhoneStatus] = mapped_column(
        _enum(ClinicPhoneStatus, "clinic_phone_status"),
        nullable=False,
        server_default=ClinicPhoneStatus.ACTIVE.value,
    )
    config: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)


class InboundCall(TimestampMixin, Base):
    """A provider inbound call session routed to a clinic."""

    __tablename__ = "inbound_call"
    __table_args__ = (
        sa.UniqueConstraint("provider", "provider_call_id", name="uq_inbound_call_provider_call"),
        sa.UniqueConstraint("clinic_id", "id", name="uq_inbound_call_clinic_id_id"),
        sa.UniqueConstraint(
            "clinic_id",
            "id",
            "provider",
            name="uq_inbound_call_clinic_id_provider",
        ),
        sa.Index("ix_inbound_call_clinic_created", "clinic_id", "created_at"),
        sa.Index("ix_inbound_call_clinic_status", "clinic_id", "status"),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    clinic_phone_number_id: Mapped[str | None] = mapped_column(
        sa.String,
        sa.ForeignKey("clinic_phone_number.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    provider: Mapped[ClinicPhoneProvider] = mapped_column(
        _enum(ClinicPhoneProvider, "clinic_phone_provider"), nullable=False
    )
    provider_call_id: Mapped[str] = mapped_column(sa.String, nullable=False)
    called_number: Mapped[str] = mapped_column(sa.String, nullable=False)
    caller_number_hash: Mapped[str | None] = mapped_column(sa.String, nullable=True, index=True)
    status: Mapped[InboundCallStatus] = mapped_column(
        _enum(InboundCallStatus, "inbound_call_status"),
        nullable=False,
        server_default=InboundCallStatus.STARTED.value,
    )
    outcome: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    provider_metadata: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)


class InboundMessage(TimestampMixin, Base):
    """A minimized provider inbound SMS routed to a clinic."""

    __tablename__ = "inbound_message"
    __table_args__ = (
        sa.UniqueConstraint("provider", "provider_message_id", name="uq_inbound_message_provider_message"),
        sa.Index("ix_inbound_message_clinic_created", "clinic_id", "created_at"),
        sa.Index("ix_inbound_message_clinic_status", "clinic_id", "status"),
        sa.Index("ix_inbound_message_clinic_intent", "clinic_id", "intent"),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    clinic_phone_number_id: Mapped[str | None] = mapped_column(
        sa.String,
        sa.ForeignKey("clinic_phone_number.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    provider: Mapped[ClinicPhoneProvider] = mapped_column(
        _enum(ClinicPhoneProvider, "clinic_phone_provider"), nullable=False
    )
    provider_message_id: Mapped[str] = mapped_column(sa.String, nullable=False)
    to_number: Mapped[str] = mapped_column(sa.String, nullable=False)
    from_number_hash: Mapped[str | None] = mapped_column(sa.String, nullable=True, index=True)
    direction: Mapped[InteractionDirection] = mapped_column(
        _enum(InteractionDirection, "interaction_direction"),
        nullable=False,
        server_default=InteractionDirection.INBOUND.value,
    )
    body_length: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    body_sha256: Mapped[str] = mapped_column(sa.String, nullable=False)
    intent: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    status: Mapped[InboundMessageStatus] = mapped_column(
        _enum(InboundMessageStatus, "inbound_message_status"),
        nullable=False,
        server_default=InboundMessageStatus.RECEIVED.value,
    )
    summary: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)


class CallRecord(TimestampMixin, Base):
    """Minimized all-call ledger plus optional consented recording evidence."""

    __tablename__ = "call_record"
    __table_args__ = (
        sa.CheckConstraint(
            "NOT (external_effect_id IS NOT NULL AND inbound_call_id IS NOT NULL)",
            name="ck_call_record_not_both_internal_anchors",
        ),
        sa.CheckConstraint(
            "provider_call_id IS NOT NULL OR external_effect_id IS NOT NULL "
            "OR inbound_call_id IS NOT NULL",
            name="ck_call_record_has_trusted_anchor",
        ),
        sa.UniqueConstraint("provider", "provider_call_id", name="uq_call_record_provider_call"),
        sa.UniqueConstraint(
            "clinic_id",
            "external_effect_id",
            name="uq_call_record_external_effect",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "inbound_call_id",
            name="uq_call_record_inbound_call",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "external_effect_id"],
            ["external_effect.clinic_id", "external_effect.id"],
            ondelete="RESTRICT",
            name="fk_call_record_external_effect_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "inbound_call_id"],
            ["inbound_call.clinic_id", "inbound_call.id"],
            ondelete="RESTRICT",
            name="fk_call_record_inbound_call_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "inbound_call_id", "provider"],
            ["inbound_call.clinic_id", "inbound_call.id", "inbound_call.provider"],
            ondelete="RESTRICT",
            name="fk_call_record_inbound_provider_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patient.clinic_id", "patient.id"],
            ondelete="RESTRICT",
            name="fk_call_record_patient_tenant",
        ),
        sa.Index("ix_call_record_clinic_created", "clinic_id", "created_at"),
        sa.Index("ix_call_record_clinic_status", "clinic_id", "recording_status"),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    patient_id: Mapped[str | None] = mapped_column(
        sa.String,
        nullable=True,
        index=True,
    )
    external_effect_id: Mapped[str | None] = mapped_column(
        sa.String, nullable=True, index=True
    )
    inbound_call_id: Mapped[str | None] = mapped_column(
        sa.String, nullable=True, index=True
    )
    provider: Mapped[ClinicPhoneProvider] = mapped_column(
        _enum(ClinicPhoneProvider, "clinic_phone_provider"), nullable=False
    )
    provider_call_id: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(sa.String, nullable=True, index=True)
    direction: Mapped[InteractionDirection] = mapped_column(
        _enum(InteractionDirection, "interaction_direction"),
        nullable=False,
        server_default=InteractionDirection.OUTBOUND.value,
    )
    scenario: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    consent_state: Mapped[RecordingConsentState] = mapped_column(
        _enum(RecordingConsentState, "recording_consent_state"),
        nullable=False,
        server_default=RecordingConsentState.NOT_ASKED.value,
    )
    consent_asked_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    consent_decided_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    consent_decision_source: Mapped[RecordingConsentSource | None] = mapped_column(
        _enum(RecordingConsentSource, "recording_consent_source"), nullable=True
    )
    consent_version: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    recording_requested_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    recording_started_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    recording_stop_requested_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    recording_stopped_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    deletion_state: Mapped[RecordingDeletionState] = mapped_column(
        _enum(RecordingDeletionState, "recording_deletion_state"),
        nullable=False,
        server_default=RecordingDeletionState.NOT_REQUESTED.value,
    )
    recording_status: Mapped[CallRecordingStatus] = mapped_column(
        _enum(CallRecordingStatus, "call_recording_status"),
        nullable=False,
        server_default=CallRecordingStatus.NONE.value,
    )
    recording_sid: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    recording_blob_path: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    recording_duration_s: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    transcript: Mapped[list[dict[str, Any]] | None] = mapped_column(sa.JSON, nullable=True)
    consent_snapshot: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)


class InboundStaffTask(TimestampMixin, Base):
    """Anonymous-capable human task created from an inbound call or message."""

    __tablename__ = "inbound_staff_task"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id",
            "id",
            name="uq_inbound_staff_task_clinic_id_id",
        ),
        sa.CheckConstraint(
            "(inbound_call_id IS NOT NULL AND inbound_message_id IS NULL) OR "
            "(inbound_call_id IS NULL AND inbound_message_id IS NOT NULL)",
            name="ck_inbound_staff_task_one_inbound_anchor",
        ),
        sa.Index("ix_inbound_staff_task_clinic_status", "clinic_id", "status"),
        sa.Index("ix_inbound_staff_task_clinic_kind", "clinic_id", "kind"),
        sa.Index(
            "uq_inbound_staff_task_active_call_kind",
            "clinic_id",
            "inbound_call_id",
            "kind",
            unique=True,
            postgresql_where=sa.text(
                "inbound_call_id IS NOT NULL AND status IN ('open', 'acknowledged')"
            ),
            sqlite_where=sa.text(
                "inbound_call_id IS NOT NULL AND status IN ('open', 'acknowledged')"
            ),
        ),
        sa.Index(
            "uq_inbound_staff_task_active_message_kind",
            "clinic_id",
            "inbound_message_id",
            "kind",
            unique=True,
            postgresql_where=sa.text(
                "inbound_message_id IS NOT NULL AND status IN ('open', 'acknowledged')"
            ),
            sqlite_where=sa.text(
                "inbound_message_id IS NOT NULL AND status IN ('open', 'acknowledged')"
            ),
        ),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    inbound_call_id: Mapped[str | None] = mapped_column(
        sa.String,
        sa.ForeignKey("inbound_call.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    inbound_message_id: Mapped[str | None] = mapped_column(
        sa.String,
        sa.ForeignKey("inbound_message.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    patient_id: Mapped[str | None] = mapped_column(
        sa.String,
        sa.ForeignKey("patient.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    kind: Mapped[InboundStaffTaskKind] = mapped_column(
        _enum(InboundStaffTaskKind, "inbound_staff_task_kind"), nullable=False
    )
    status: Mapped[InboundStaffTaskStatus] = mapped_column(
        _enum(InboundStaffTaskStatus, "inbound_staff_task_status"),
        nullable=False,
        server_default=InboundStaffTaskStatus.OPEN.value,
    )
    priority: Mapped[str] = mapped_column(sa.String, nullable=False, server_default="normal")
    reason: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    summary: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    assigned_to: Mapped[str | None] = mapped_column(sa.String, nullable=True)


class Patient(TimestampMixin, Base):
    """A patient belonging to exactly one clinic."""

    __tablename__ = "patient"
    __table_args__ = (
        sa.UniqueConstraint("clinic_id", "source_ref", name="uq_patient_clinic_source_ref"),
        sa.UniqueConstraint("clinic_id", "id", name="uq_patient_clinic_id_id"),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    # External system id (Cliniko/CSV); the idempotency key for sync upserts.
    source_ref: Mapped[str] = mapped_column(sa.String, nullable=False)
    name: Mapped[str] = mapped_column(sa.String, nullable=False)
    phone: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    email: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    consent_flags: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict, server_default="{}"
    )
    opt_out_flags: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict, server_default="{}"
    )
    contact_prefs: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)

    appointments: Mapped[list[Appointment]] = relationship(back_populates="patient")


class PilotProgramme(TimestampMixin, Base):
    """One clinic's bounded production-pilot release programme."""

    __tablename__ = "pilot_programme"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id",
            "environment",
            "release_identity",
            name="uq_pilot_programme_release",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "id",
            name="uq_pilot_programme_clinic_id_id",
        ),
        sa.CheckConstraint(
            "length(environment) BETWEEN 1 AND 32",
            name="ck_pilot_programme_environment_length",
        ),
        sa.CheckConstraint(
            "length(release_identity) BETWEEN 1 AND 200",
            name="ck_pilot_programme_release_identity_length",
        ),
        sa.CheckConstraint(
            "maximum_unique_patients = 50",
            name="ck_pilot_programme_maximum_unique_patients",
        ),
        sa.CheckConstraint(
            "active_cumulative_limit IN (0, 5, 15, 30, 50)",
            name="ck_pilot_programme_cumulative_limit",
        ),
        sa.CheckConstraint(
            "release_evidence_hash IS NULL OR length(release_evidence_hash) = 64",
            name="ck_pilot_programme_release_evidence_hash",
        ),
        sa.Index(
            "ix_pilot_programme_clinic_state",
            "clinic_id",
            "state",
        ),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    environment: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    release_identity: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    state: Mapped[PilotProgrammeState] = mapped_column(
        _enum(PilotProgrammeState, "pilot_programme_state"),
        nullable=False,
        server_default=PilotProgrammeState.DRAFT.value,
    )
    maximum_unique_patients: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        server_default="50",
    )
    active_cumulative_limit: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        server_default="0",
    )
    released_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    released_by: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    release_evidence_hash: Mapped[str | None] = mapped_column(
        sa.String(64), nullable=True
    )
    paused_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    paused_by: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    pause_reason: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)


class PilotParticipant(TimestampMixin, Base):
    """Append-only patient membership and ordinal within one pilot programme."""

    __tablename__ = "pilot_participant"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id",
            "pilot_programme_id",
            "patient_key_hash",
            name="uq_pilot_participant_patient",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "pilot_programme_id",
            "patient_id",
            name="uq_pilot_participant_patient_reference",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "pilot_programme_id",
            "ordinal",
            name="uq_pilot_participant_ordinal",
        ),
        sa.CheckConstraint(
            "ordinal BETWEEN 1 AND 50",
            name="ck_pilot_participant_ordinal",
        ),
        sa.CheckConstraint(
            "wave BETWEEN 1 AND 4",
            name="ck_pilot_participant_wave",
        ),
        sa.CheckConstraint(
            "(wave = 1 AND ordinal BETWEEN 1 AND 5) OR "
            "(wave = 2 AND ordinal BETWEEN 6 AND 15) OR "
            "(wave = 3 AND ordinal BETWEEN 16 AND 30) OR "
            "(wave = 4 AND ordinal BETWEEN 31 AND 50)",
            name="ck_pilot_participant_wave_ordinal",
        ),
        sa.CheckConstraint(
            "length(patient_key_hash) = 64",
            name="ck_pilot_participant_patient_key_hash",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "pilot_programme_id"],
            ["pilot_programme.clinic_id", "pilot_programme.id"],
            ondelete="RESTRICT",
            name="fk_pilot_participant_programme_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patient.clinic_id", "patient.id"],
            ondelete="RESTRICT",
            name="fk_pilot_participant_patient_tenant",
        ),
        sa.Index(
            "ix_pilot_participant_clinic_programme_ordinal",
            "clinic_id",
            "pilot_programme_id",
            "ordinal",
        ),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    pilot_programme_id: Mapped[str] = mapped_column(sa.String, nullable=False)
    patient_id: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    patient_key_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    wave: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    enrolled_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    first_contact_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class IdentityEvidence(TimestampMixin, Base):
    """Minimized, expiring identity authority bound to one live session."""

    __tablename__ = "identity_evidence"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id",
            "id",
            name="uq_identity_evidence_clinic_id_id",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "session_key_hash",
            name="uq_identity_evidence_session",
        ),
        sa.CheckConstraint(
            "length(session_key_hash) = 64",
            name="ck_identity_evidence_session_hash",
        ),
        sa.CheckConstraint(
            "length(route_key_hash) = 64",
            name="ck_identity_evidence_route_hash",
        ),
        sa.CheckConstraint(
            "length(patient_key_hash) = 64",
            name="ck_identity_evidence_patient_hash",
        ),
        sa.CheckConstraint(
            "challenge_token_hash IS NULL OR length(challenge_token_hash) = 64",
            name="ck_identity_evidence_challenge_hash",
        ),
        sa.CheckConstraint(
            "matched_factor_count >= 0 AND attempt_count >= 0 "
            "AND max_attempts >= 1 AND attempt_count <= max_attempts",
            name="ck_identity_evidence_attempt_bounds",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name="ck_identity_evidence_expiry_order",
        ),
        sa.CheckConstraint(
            "tier <> 't2' OR (matched_factor_count >= 2 AND dob_verified = true)",
            name="ck_identity_evidence_t2_factors",
        ),
        sa.CheckConstraint(
            "state <> 'revoked' OR revoked_at IS NOT NULL",
            name="ck_identity_evidence_revocation_time",
        ),
        sa.Index(
            "ix_identity_evidence_clinic_expiry",
            "clinic_id",
            "state",
            "expires_at",
        ),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    session_key_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    route_key_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    patient_key_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    channel: Mapped[Channel] = mapped_column(_enum(Channel, "channel"), nullable=False)
    policy_version: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    tier: Mapped[IdentityTier] = mapped_column(
        _enum(IdentityTier, "identity_tier"),
        nullable=False,
        server_default=IdentityTier.T0.value,
    )
    state: Mapped[IdentityEvidenceState] = mapped_column(
        _enum(IdentityEvidenceState, "identity_evidence_state"),
        nullable=False,
        server_default=IdentityEvidenceState.ACTIVE.value,
    )
    reason: Mapped[IdentityEvidenceReason] = mapped_column(
        _enum(IdentityEvidenceReason, "identity_evidence_reason"),
        nullable=False,
        server_default=IdentityEvidenceReason.ROUTE_ONLY.value,
    )
    matched_factor_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    dob_verified: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    attempt_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(_UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(_UTCDateTime(), nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(_UTCDateTime(), nullable=True)
    challenge_token_hash: Mapped[str | None] = mapped_column(
        sa.String(64), nullable=True
    )
    challenge_consumed_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    pending_factor_type: Mapped[str | None] = mapped_column(
        sa.String(64), nullable=True
    )
    revision: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )


class IdentityFactorAttempt(TimestampMixin, Base):
    """Append-only metadata for one transient factor comparison."""

    __tablename__ = "identity_factor_attempt"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id",
            "id",
            name="uq_identity_factor_attempt_clinic_id_id",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "evidence_id",
            "attempt_number",
            name="uq_identity_factor_attempt_number",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "evidence_id"],
            ["identity_evidence.clinic_id", "identity_evidence.id"],
            ondelete="CASCADE",
            name="fk_identity_factor_attempt_evidence_tenant",
        ),
        sa.CheckConstraint(
            "length(factor_type) BETWEEN 1 AND 64",
            name="ck_identity_factor_attempt_type_length",
        ),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name="ck_identity_factor_attempt_number_positive",
        ),
        sa.Index(
            "ix_identity_factor_attempt_evidence",
            "clinic_id",
            "evidence_id",
        ),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    evidence_id: Mapped[str] = mapped_column(sa.String, nullable=False)
    factor_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    result: Mapped[IdentityFactorResult] = mapped_column(
        _enum(IdentityFactorResult, "identity_factor_result"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(_UTCDateTime(), nullable=False)


class Appointment(TimestampMixin, Base):
    """An appointment belonging to one clinic and one patient."""

    __tablename__ = "appointment"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id", "source_ref", name="uq_appointment_clinic_source_ref"
        ),
        sa.Index("ix_appointment_clinic_status", "clinic_id", "status"),
        sa.Index("ix_appointment_clinic_start_at", "clinic_id", "start_at"),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    patient_id: Mapped[str] = mapped_column(
        sa.String, sa.ForeignKey("patient.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_ref: Mapped[str] = mapped_column(sa.String, nullable=False)
    status: Mapped[AppointmentStatus] = mapped_column(
        _enum(AppointmentStatus, "appointment_status"), nullable=False
    )
    start_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    value: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 2), nullable=True)
    reason_code: Mapped[ReasonCode | None] = mapped_column(
        _enum(ReasonCode, "reason_code"), nullable=True
    )

    patient: Mapped[Patient] = relationship(back_populates="appointments")


class Campaign(TimestampMixin, Base):
    """A batch of outreach for a clinic and a reason."""

    __tablename__ = "campaign"

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    type: Mapped[CampaignType] = mapped_column(_enum(CampaignType, "campaign_type"), nullable=False)
    schedule: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    cadence_config: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    status: Mapped[CampaignStatus] = mapped_column(
        _enum(CampaignStatus, "campaign_status"),
        nullable=False,
        server_default=CampaignStatus.ACTIVE.value,
    )


class CadenceCursor(TimestampMixin, Base):
    """Tenant-scoped UTC watermark for one finite cadence planner."""

    __tablename__ = "cadence_cursor"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id",
            "planner_name",
            name="uq_cadence_cursor_clinic_planner",
        ),
        sa.CheckConstraint(
            "length(planner_name) BETWEEN 1 AND 64",
            name="ck_cadence_cursor_planner_name_length",
        ),
        sa.CheckConstraint(
            "last_run_id IS NULL OR length(last_run_id) BETWEEN 1 AND 64",
            name="ck_cadence_cursor_run_id_length",
        ),
        sa.Index("ix_cadence_cursor_clinic_watermark", "clinic_id", "watermark_at"),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    planner_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    watermark_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    last_started_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_completed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_run_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)


class OutreachJob(TimestampMixin, Base):
    """One contact attempt: a patient + an appointment on a single channel."""

    __tablename__ = "outreach_job"
    __table_args__ = (sa.Index("ix_outreach_job_clinic_state", "clinic_id", "state"),)

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    campaign_id: Mapped[str] = mapped_column(
        sa.String, sa.ForeignKey("campaign.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[str] = mapped_column(
        sa.String, sa.ForeignKey("patient.id", ondelete="CASCADE"), nullable=False
    )
    appointment_id: Mapped[str | None] = mapped_column(
        sa.String, sa.ForeignKey("appointment.id", ondelete="SET NULL"), nullable=True
    )
    channel: Mapped[Channel] = mapped_column(_enum(Channel, "channel"), nullable=False)
    state: Mapped[OutreachState] = mapped_column(
        _enum(OutreachState, "outreach_state"),
        nullable=False,
        server_default=OutreachState.QUEUED.value,
    )
    reason_code: Mapped[ReasonCode | None] = mapped_column(
        _enum(ReasonCode, "reason_code"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    next_action_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class Interaction(TimestampMixin, Base):
    """A single inbound or outbound message / call event for a job."""

    __tablename__ = "interaction"

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    outreach_job_id: Mapped[str] = mapped_column(
        sa.String, sa.ForeignKey("outreach_job.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[Channel] = mapped_column(_enum(Channel, "channel"), nullable=False)
    direction: Mapped[InteractionDirection] = mapped_column(
        _enum(InteractionDirection, "interaction_direction"), nullable=False
    )
    content: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    intent: Mapped[InteractionIntent | None] = mapped_column(
        _enum(InteractionIntent, "interaction_intent"), nullable=True
    )
    outcome: Mapped[InteractionOutcome | None] = mapped_column(
        _enum(InteractionOutcome, "interaction_outcome"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class ExternalEffect(TimestampMixin, Base):
    """Minimized, tenant-scoped request for one external provider effect."""

    __tablename__ = "external_effect"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id",
            "effect_type",
            "idempotency_key",
            name="uq_external_effect_logical_request",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "id",
            name="uq_external_effect_clinic_id_id",
        ),
        sa.UniqueConstraint(
            "callback_token",
            name="uq_external_effect_callback_token",
        ),
        sa.CheckConstraint(
            "length(callback_token) BETWEEN 50 AND 240",
            name="ck_external_effect_callback_token_length",
        ),
        sa.CheckConstraint(
            "read_attempt_count >= 0 AND max_read_attempts >= 1 "
            "AND read_attempt_count <= max_read_attempts",
            name="ck_external_effect_read_attempt_bounds",
        ),
        sa.CheckConstraint(
            "preflight_evidence_hash IS NULL OR length(preflight_evidence_hash) = 64",
            name="ck_external_effect_preflight_evidence_hash",
        ),
        sa.Index(
            "ix_external_effect_claim",
            "clinic_id",
            "state",
            "available_at",
        ),
        sa.Index(
            "ix_external_effect_expired_lease",
            "clinic_id",
            "state",
            "lease_expires_at",
        ),
        sa.Index(
            "ix_external_effect_provider_resource",
            "clinic_id",
            "effect_type",
            "provider_resource_id",
        ),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    aggregate_type: Mapped[str] = mapped_column(sa.String, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(sa.String, nullable=False)
    effect_type: Mapped[ExternalEffectType] = mapped_column(
        _enum(ExternalEffectType, "external_effect_type"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(sa.String, nullable=False)
    callback_token: Mapped[str] = mapped_column(
        sa.String(240),
        nullable=False,
        default=_effect_token_default,
    )
    payload_version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="1"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    request_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    state: Mapped[ExternalEffectState] = mapped_column(
        _enum(ExternalEffectState, "external_effect_state"),
        nullable=False,
        server_default=ExternalEffectState.PENDING.value,
    )
    available_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="3"
    )
    read_attempt_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    max_read_attempts: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="4"
    )
    settle_deadline_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    preflight_evidence_hash: Mapped[str | None] = mapped_column(
        sa.String(64), nullable=True
    )
    lease_owner: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    dispatch_started_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    provider_resource_id: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    provider_status: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    provider_sequence: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    last_error_class: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    completion_evidence_hash: Mapped[str | None] = mapped_column(
        sa.String(64), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class RightsRequest(TimestampMixin, Base):
    """Permanent privacy aggregate and anti-rehydration tombstone."""

    __tablename__ = "rights_request"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id",
            "id",
            name="uq_rights_request_clinic_id_id",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "kind",
            "scope_hash",
            name="uq_rights_request_convergence",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patient.clinic_id", "patient.id"],
            ondelete="RESTRICT",
            name="fk_rights_request_patient_tenant",
        ),
        sa.CheckConstraint(
            "length(subject_key_hash) = 64",
            name="ck_rights_request_subject_hash_length",
        ),
        sa.CheckConstraint(
            "length(patient_reference_hash) = 64",
            name="ck_rights_request_patient_reference_hash_length",
        ),
        sa.CheckConstraint(
            "length(request_identity_hash) = 64",
            name="ck_rights_request_identity_hash_length",
        ),
        sa.CheckConstraint(
            "length(actor_reference_hash) = 64",
            name="ck_rights_request_actor_hash_length",
        ),
        sa.CheckConstraint(
            "length(approval_evidence_hash) = 64",
            name="ck_rights_request_approval_hash_length",
        ),
        sa.CheckConstraint(
            "length(scope_hash) = 64",
            name="ck_rights_request_scope_hash_length",
        ),
        sa.CheckConstraint(
            "target_count >= 0 AND verified_target_count >= 0 AND residual_target_count >= 0",
            name="ck_rights_request_counts_nonnegative",
        ),
        sa.Index("ix_rights_request_clinic_state", "clinic_id", "state"),
        sa.Index(
            "ix_rights_request_clinic_subject",
            "clinic_id",
            "kind",
            "subject_key_hash",
        ),
        sa.Index("ix_rights_request_clinic_due", "clinic_id", "due_at"),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    kind: Mapped[RightsRequestKind] = mapped_column(
        _enum(RightsRequestKind, "rights_request_kind"), nullable=False
    )
    subject_key_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    subject_key_version: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    patient_reference_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    patient_id: Mapped[str | None] = mapped_column(sa.String, nullable=True, index=True)
    request_identity_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    actor_role: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    actor_reference_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    approval_evidence_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    scope_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    state: Mapped[RightsRequestState] = mapped_column(
        _enum(RightsRequestState, "rights_request_state"),
        nullable=False,
        server_default=RightsRequestState.REQUESTED.value,
    )
    requested_at: Mapped[datetime] = mapped_column(_UTCDateTime(), nullable=False)
    frozen_at: Mapped[datetime | None] = mapped_column(_UTCDateTime(), nullable=True)
    inventory_finalized_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    deleting_at: Mapped[datetime | None] = mapped_column(_UTCDateTime(), nullable=True)
    verifying_at: Mapped[datetime | None] = mapped_column(_UTCDateTime(), nullable=True)
    due_at: Mapped[datetime] = mapped_column(_UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(_UTCDateTime(), nullable=True)
    completion_evidence_hash: Mapped[str | None] = mapped_column(
        sa.String(64), nullable=True
    )
    target_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    verified_target_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    residual_target_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )


class RightsTarget(TimestampMixin, Base):
    """One minimized deletion, purge, verification, or procedure target."""

    __tablename__ = "rights_target"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id",
            "id",
            name="uq_rights_target_clinic_id_id",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "request_id",
            "target_key_hash",
            name="uq_rights_target_request_key",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "request_id"],
            ["rights_request.clinic_id", "rights_request.id"],
            ondelete="RESTRICT",
            name="fk_rights_target_request_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "prerequisite_target_id"],
            ["rights_target.clinic_id", "rights_target.id"],
            ondelete="RESTRICT",
            name="fk_rights_target_prerequisite_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "current_effect_id"],
            ["external_effect.clinic_id", "external_effect.id"],
            ondelete="RESTRICT",
            name="fk_rights_target_effect_tenant",
        ),
        sa.CheckConstraint(
            "length(target_key_hash) = 64",
            name="ck_rights_target_key_hash_length",
        ),
        sa.CheckConstraint(
            "attempt_ordinal >= 0 AND reconciliation_count >= 0",
            name="ck_rights_target_counts_nonnegative",
        ),
        sa.Index("ix_rights_target_request_state", "clinic_id", "request_id", "state"),
        sa.Index("ix_rights_target_clinic_due", "clinic_id", "due_at"),
        sa.Index("ix_rights_target_clinic_owner", "clinic_id", "owner_type", "owner_id"),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    request_id: Mapped[str] = mapped_column(sa.String, nullable=False, index=True)
    system: Mapped[RightsTargetSystem] = mapped_column(
        _enum(RightsTargetSystem, "rights_target_system"), nullable=False
    )
    resource: Mapped[RightsTargetResource] = mapped_column(
        _enum(RightsTargetResource, "rights_target_resource"), nullable=False
    )
    action: Mapped[RightsTargetAction] = mapped_column(
        _enum(RightsTargetAction, "rights_target_action"), nullable=False
    )
    owner_type: Mapped[RightsTargetOwnerType] = mapped_column(
        _enum(RightsTargetOwnerType, "rights_target_owner_type"), nullable=False
    )
    owner_id: Mapped[str] = mapped_column(sa.String, nullable=False)
    target_key_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    prerequisite_target_id: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    mandatory: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    state: Mapped[RightsTargetState] = mapped_column(
        _enum(RightsTargetState, "rights_target_state"),
        nullable=False,
        server_default=RightsTargetState.REQUESTED.value,
    )
    current_effect_id: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    attempt_ordinal: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    available_at: Mapped[datetime] = mapped_column(_UTCDateTime(), nullable=False)
    due_at: Mapped[datetime] = mapped_column(_UTCDateTime(), nullable=False)
    disposition_code: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    reconciliation_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    last_reconciled_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(_UTCDateTime(), nullable=True)
    residual_category: Mapped[RightsResidualCategory | None] = mapped_column(
        _enum(RightsResidualCategory, "rights_residual_category"), nullable=True
    )
    residual_policy_version: Mapped[str | None] = mapped_column(
        sa.String(128), nullable=True
    )
    residual_approval_evidence_hash: Mapped[str | None] = mapped_column(
        sa.String(64), nullable=True
    )
    residual_completion_eligible: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    residual_due_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    locator_cleared_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )


class ExternalEffectHandoff(TimestampMixin, Base):
    """Minimized queued staff receipt for one exhausted external effect."""

    __tablename__ = "external_effect_handoff"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id",
            "id",
            name="uq_external_effect_handoff_clinic_id_id",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "external_effect_id",
            name="uq_external_effect_handoff_effect",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "external_effect_id"],
            ["external_effect.clinic_id", "external_effect.id"],
            ondelete="RESTRICT",
            name="fk_external_effect_handoff_effect_tenant",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'acknowledged', 'resolved')",
            name="ck_external_effect_handoff_status",
        ),
        sa.Index(
            "ix_external_effect_handoff_clinic_status",
            "clinic_id",
            "status",
        ),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    external_effect_id: Mapped[str] = mapped_column(sa.String, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(32), nullable=False, server_default="queued"
    )
    reason_code: Mapped[str] = mapped_column(sa.String(64), nullable=False)


class HandoffReceipt(TimestampMixin, Base):
    """Minimized receipt for exactly one tenant-owned human-work item."""

    __tablename__ = "handoff_receipt"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id", "id", name="uq_handoff_receipt_clinic_id_id"
        ),
        sa.CheckConstraint(
            "(CASE WHEN escalation_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN inbound_staff_task_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN booking_action_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN external_effect_handoff_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_handoff_receipt_exactly_one_owner",
        ),
        sa.CheckConstraint(
            "due_at >= queued_at AND "
            "(sent_at IS NULL OR sent_at >= queued_at) AND "
            "(delivered_at IS NULL OR "
            "(sent_at IS NOT NULL AND delivered_at >= sent_at)) AND "
            "(acknowledged_at IS NULL OR acknowledged_at >= queued_at) AND "
            "(resolved_at IS NULL OR "
            "(acknowledged_at IS NOT NULL AND resolved_at >= acknowledged_at))",
            name="ck_handoff_receipt_timestamp_order",
        ),
        sa.CheckConstraint(
            "(acknowledged_at IS NULL AND acknowledged_by IS NULL) OR "
            "(acknowledged_at IS NOT NULL AND acknowledged_by IS NOT NULL)",
            name="ck_handoff_receipt_ack_complete",
        ),
        sa.CheckConstraint(
            "(resolved_at IS NULL AND resolved_by IS NULL) OR "
            "(resolved_at IS NOT NULL AND resolved_by IS NOT NULL "
            "AND acknowledged_at IS NOT NULL)",
            name="ck_handoff_receipt_resolution_complete",
        ),
        sa.CheckConstraint(
            "(alternate_state = 'not_requested' AND alternate_requested_at IS NULL) OR "
            "(alternate_state = 'requested' AND alternate_requested_at IS NOT NULL)",
            name="ck_handoff_receipt_alternate_complete",
        ),
        sa.CheckConstraint(
            "(delivery_state = 'queued' AND sent_at IS NULL AND delivered_at IS NULL) OR "
            "(delivery_state = 'sent' AND sent_at IS NOT NULL AND delivered_at IS NULL) OR "
            "(delivery_state = 'delivered' AND sent_at IS NOT NULL "
            "AND delivered_at IS NOT NULL) OR "
            "(delivery_state IN ('definitive_failure', 'reconcile_required') "
            "AND delivered_at IS NULL)",
            name="ck_handoff_receipt_delivery_evidence",
        ),
        sa.CheckConstraint(
            "length(policy_version) BETWEEN 1 AND 128 AND length(policy_sha256) = 64",
            name="ck_handoff_receipt_policy_identity",
        ),
        sa.CheckConstraint(
            "policy_critical_minutes BETWEEN 1 AND 5 AND "
            "policy_high_minutes BETWEEN 1 AND 15 AND "
            "policy_normal_business_hours BETWEEN 1 AND 4",
            name="ck_handoff_receipt_policy_bounds",
        ),
        sa.CheckConstraint(
            "(acknowledged_by IS NULL OR length(acknowledged_by) BETWEEN 1 AND 200) "
            "AND (resolved_by IS NULL OR length(resolved_by) BETWEEN 1 AND 200)",
            name="ck_handoff_receipt_actor_bounds",
        ),
        sa.CheckConstraint(
            "severity_generation BETWEEN 0 AND 16 AND "
            "notification_count BETWEEN 0 AND 32 AND "
            "escalation_level BETWEEN 0 AND 1",
            name="ck_handoff_receipt_counter_bounds",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "escalation_id"],
            ["escalation.clinic_id", "escalation.id"],
            ondelete="RESTRICT",
            name="fk_handoff_receipt_escalation_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "inbound_staff_task_id"],
            ["inbound_staff_task.clinic_id", "inbound_staff_task.id"],
            ondelete="RESTRICT",
            name="fk_handoff_receipt_inbound_task_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "booking_action_id"],
            ["booking_action.clinic_id", "booking_action.id"],
            ondelete="RESTRICT",
            name="fk_handoff_receipt_booking_action_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "external_effect_handoff_id"],
            ["external_effect_handoff.clinic_id", "external_effect_handoff.id"],
            ondelete="RESTRICT",
            name="fk_handoff_receipt_external_handoff_tenant",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "escalation_id",
            name="uq_handoff_receipt_escalation",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "inbound_staff_task_id",
            name="uq_handoff_receipt_inbound_task",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "booking_action_id",
            name="uq_handoff_receipt_booking_action",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "external_effect_handoff_id",
            name="uq_handoff_receipt_external_handoff",
        ),
        sa.Index(
            "ix_handoff_receipt_clinic_state_due",
            "clinic_id",
            "delivery_state",
            "due_at",
        ),
        sa.Index(
            "ix_handoff_receipt_clinic_severity_due",
            "clinic_id",
            "severity",
            "due_at",
        ),
        sa.Index(
            "ix_handoff_receipt_clinic_open_due",
            "clinic_id",
            "acknowledged_at",
            "resolved_at",
            "due_at",
        ),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    escalation_id: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    inbound_staff_task_id: Mapped[str | None] = mapped_column(
        sa.String, nullable=True
    )
    booking_action_id: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    external_effect_handoff_id: Mapped[str | None] = mapped_column(
        sa.String, nullable=True
    )
    severity: Mapped[HandoffSeverity] = mapped_column(
        _enum(HandoffSeverity, "handoff_severity"), nullable=False
    )
    delivery_state: Mapped[HandoffDeliveryState] = mapped_column(
        _enum(HandoffDeliveryState, "handoff_delivery_state"),
        nullable=False,
        server_default=HandoffDeliveryState.QUEUED.value,
    )
    queued_at: Mapped[datetime] = mapped_column(_UTCDateTime(), nullable=False)
    due_at: Mapped[datetime] = mapped_column(_UTCDateTime(), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(_UTCDateTime(), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    acknowledged_by: Mapped[str | None] = mapped_column(
        sa.String(200), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(_UTCDateTime(), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    policy_version: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    policy_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    policy_critical_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    policy_high_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    policy_normal_business_hours: Mapped[int] = mapped_column(
        sa.Integer, nullable=False
    )
    severity_generation: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    notification_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    escalation_level: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    alternate_state: Mapped[HandoffAlternateState] = mapped_column(
        _enum(HandoffAlternateState, "handoff_alternate_state"),
        nullable=False,
        server_default=HandoffAlternateState.NOT_REQUESTED.value,
    )
    alternate_requested_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )


class ProviderCallbackReceipt(TimestampMixin, Base):
    """Minimized, tenant-scoped evidence from one signed provider callback."""

    __tablename__ = "provider_callback_receipt"
    __table_args__ = (
        sa.CheckConstraint(
            "length(provider) BETWEEN 1 AND 32",
            name="ck_provider_callback_receipt_provider_length",
        ),
        sa.CheckConstraint(
            "length(deduplication_hash) = 64",
            name="ck_provider_callback_receipt_dedup_hash_length",
        ),
        sa.CheckConstraint(
            "length(effect_token_hash) = 64",
            name="ck_provider_callback_receipt_token_hash_length",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_provider_callback_receipt_payload_hash_length",
        ),
        sa.CheckConstraint(
            "length(normalized_status) BETWEEN 1 AND 32",
            name="ck_provider_callback_receipt_status_length",
        ),
        sa.CheckConstraint(
            "provider_sequence IS NULL OR provider_sequence >= 0",
            name="ck_provider_callback_receipt_sequence_nonnegative",
        ),
        sa.CheckConstraint(
            "processing_attempts >= 0",
            name="ck_provider_callback_receipt_attempts_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "external_effect_id"],
            ["external_effect.clinic_id", "external_effect.id"],
            ondelete="RESTRICT",
            name="fk_provider_callback_receipt_tenant_effect",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "provider",
            "callback_kind",
            "deduplication_hash",
            name="uq_provider_callback_receipt_event",
        ),
        sa.Index(
            "ix_provider_callback_receipt_claim",
            "clinic_id",
            "state",
            "received_at",
        ),
        sa.Index(
            "ix_provider_callback_receipt_expired_lease",
            "clinic_id",
            "state",
            "lease_expires_at",
        ),
        sa.Index(
            "ix_provider_callback_receipt_effect",
            "clinic_id",
            "external_effect_id",
            "callback_kind",
        ),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    external_effect_id: Mapped[str] = mapped_column(
        sa.String,
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    callback_kind: Mapped[ProviderCallbackKind] = mapped_column(
        _enum(ProviderCallbackKind, "provider_callback_kind"),
        nullable=False,
    )
    deduplication_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    effect_token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    provider_resource_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    normalized_status: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    provider_sequence: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    provider_observed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    payload_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    state: Mapped[ProviderCallbackState] = mapped_column(
        _enum(ProviderCallbackState, "provider_callback_state"),
        nullable=False,
        server_default=ProviderCallbackState.PENDING.value,
    )
    reason_code: Mapped[ProviderCallbackReason | None] = mapped_column(
        _enum(ProviderCallbackReason, "provider_callback_reason"),
        nullable=True,
    )
    processing_attempts: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        server_default="0",
    )
    lease_owner: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    received_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )
    applied_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )


class AvailabilitySlot(TimestampMixin, Base):
    """A deterministic, provider-sourced appointment slot offered by tools."""

    __tablename__ = "availability_slot"
    __table_args__ = (
        sa.UniqueConstraint("clinic_id", "source_ref", name="uq_availability_slot_source"),
        sa.Index("ix_availability_slot_clinic_start", "clinic_id", "start_at"),
        sa.Index("ix_availability_slot_clinic_clinician", "clinic_id", "clinician_id"),
        sa.Index(
            "ix_availability_slot_clinic_fresh",
            "clinic_id",
            "expires_at",
            "start_at",
        ),
        sa.CheckConstraint(
            "(source_provider IS NULL AND business_id IS NULL "
            "AND appointment_type_id IS NULL AND fetched_at IS NULL "
            "AND expires_at IS NULL) OR "
            "(source_provider IS NOT NULL AND business_id IS NOT NULL "
            "AND clinician_id IS NOT NULL AND appointment_type_id IS NOT NULL "
            "AND fetched_at IS NOT NULL AND expires_at IS NOT NULL "
            "AND expires_at > fetched_at)",
            name="ck_availability_slot_authoritative_observation",
        ),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    source_ref: Mapped[str] = mapped_column(sa.String, nullable=False)
    source_provider: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    business_id: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    appointment_type_id: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    clinician_id: Mapped[str | None] = mapped_column(sa.String, nullable=True, index=True)
    start_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime | None] = mapped_column(_UTCDateTime(), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(_UTCDateTime(), nullable=True)
    appointment_id: Mapped[str | None] = mapped_column(
        sa.String, sa.ForeignKey("appointment.id", ondelete="SET NULL"), nullable=True, index=True
    )
    details: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)


class BookingAction(TimestampMixin, Base):
    """A deterministic booking or a staff approval-queue entry."""

    __tablename__ = "booking_action"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id", "id", name="uq_booking_action_clinic_id_id"
        ),
        sa.UniqueConstraint(
            "clinic_id", "availability_slot_id", name="uq_booking_action_clinic_slot"
        ),
        sa.CheckConstraint(
            "request_hash IS NULL OR length(request_hash) = 64",
            name="ck_booking_action_request_hash_length",
        ),
        sa.CheckConstraint(
            "(identity_evidence_id IS NULL AND identity_policy_version IS NULL "
            "AND identity_evidence_revision IS NULL) OR "
            "(identity_evidence_id IS NOT NULL AND identity_policy_version IS NOT NULL "
            "AND identity_evidence_revision >= 0)",
            name="ck_booking_action_identity_binding_complete",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "identity_evidence_id"],
            ["identity_evidence.clinic_id", "identity_evidence.id"],
            ondelete="RESTRICT",
            name="fk_booking_action_identity_evidence_tenant",
        ),
        sa.CheckConstraint(
            "conflict_reason IS NULL OR length(conflict_reason) BETWEEN 1 AND 64",
            name="ck_booking_action_conflict_reason_length",
        ),
        sa.CheckConstraint(
            "(write_back_state = 'verified' AND written_back = true "
            "AND external_appointment_ref IS NOT NULL "
            "AND provider_attempted_at IS NOT NULL "
            "AND read_back_verified_at IS NOT NULL) OR "
            "(write_back_state <> 'verified' AND written_back = false "
            "AND read_back_verified_at IS NULL)",
            name="ck_booking_action_verified_write_back",
        ),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    appointment_id: Mapped[str] = mapped_column(
        sa.String, sa.ForeignKey("appointment.id", ondelete="CASCADE"), nullable=False, index=True
    )
    outreach_job_id: Mapped[str | None] = mapped_column(
        sa.String, sa.ForeignKey("outreach_job.id", ondelete="SET NULL"), nullable=True, index=True
    )
    availability_slot_id: Mapped[str | None] = mapped_column(
        sa.String,
        sa.ForeignKey("availability_slot.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    type: Mapped[BookingActionType] = mapped_column(
        _enum(BookingActionType, "booking_action_type"), nullable=False
    )
    status: Mapped[BookingActionStatus] = mapped_column(
        _enum(BookingActionStatus, "booking_action_status"),
        nullable=False,
        server_default=BookingActionStatus.PENDING.value,
    )
    approved_by: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    written_back: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    write_back_state: Mapped[BookingWriteBackState] = mapped_column(
        _enum(BookingWriteBackState, "booking_write_back_state"),
        nullable=False,
        server_default=BookingWriteBackState.NOT_ATTEMPTED.value,
    )
    external_appointment_ref: Mapped[str | None] = mapped_column(
        sa.String(200), nullable=True
    )
    request_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    identity_evidence_id: Mapped[str | None] = mapped_column(
        sa.String, nullable=True, index=True
    )
    identity_policy_version: Mapped[str | None] = mapped_column(
        sa.String(128), nullable=True
    )
    identity_evidence_revision: Mapped[int | None] = mapped_column(
        sa.Integer, nullable=True
    )
    provider_attempted_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    read_back_verified_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    conflict_reason: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)


class Escalation(TimestampMixin, Base):
    """A human-review queue entry for a clinical/urgent/ambiguous case."""

    __tablename__ = "escalation"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id", "id", name="uq_escalation_clinic_id_id"
        ),
        sa.Index(
            "uq_escalation_active_outreach_job",
            "clinic_id",
            "outreach_job_id",
            unique=True,
            postgresql_where=sa.text(
                "outreach_job_id IS NOT NULL AND status IN ('open', 'acknowledged')"
            ),
            sqlite_where=sa.text(
                "outreach_job_id IS NOT NULL AND status IN ('open', 'acknowledged')"
            ),
        ),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    patient_id: Mapped[str] = mapped_column(
        sa.String, sa.ForeignKey("patient.id", ondelete="CASCADE"), nullable=False, index=True
    )
    outreach_job_id: Mapped[str | None] = mapped_column(
        sa.String,
        sa.ForeignKey("outreach_job.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reason: Mapped[EscalationReason] = mapped_column(
        _enum(EscalationReason, "escalation_reason"), nullable=False
    )
    priority: Mapped[EscalationPriority] = mapped_column(
        _enum(EscalationPriority, "escalation_priority"),
        nullable=False,
        server_default=EscalationPriority.NORMAL.value,
    )
    context_ref: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    status: Mapped[EscalationStatus] = mapped_column(
        _enum(EscalationStatus, "escalation_status"),
        nullable=False,
        server_default=EscalationStatus.OPEN.value,
    )
    assigned_to: Mapped[str | None] = mapped_column(sa.String, nullable=True)


class IncidentReport(TimestampMixin, Base):
    """An ANONYMOUS clinical-governance incident report (LFPSE/Datix-style).

    Anonymity invariants (hard, by schema design):
    - No reporter identity, patient identity, phone number, or IP is stored.
    - ``occurred_hour`` is coarsened to the hour so report timing cannot be
      trivially joined against per-message transport logs.
    - ``related_job_id`` may only be set by STAFF reports (patients cannot be
      linked to jobs without identifying them); enforced in the service layer.
    Free-text ``description`` is stored as given; reporters may self-identify
    in prose (documented residual risk, mirrored in the reporting UI/SMS copy).
    """

    __tablename__ = "incident_report"
    __table_args__ = (
        sa.Index("ix_incident_report_clinic_status", "clinic_id", "status"),
        sa.Index("ix_incident_report_clinic_severity", "clinic_id", "severity"),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    source: Mapped[IncidentSource] = mapped_column(
        _enum(IncidentSource, "incident_source"), nullable=False
    )
    category: Mapped[IncidentCategory] = mapped_column(
        _enum(IncidentCategory, "incident_category"),
        nullable=False,
        server_default=IncidentCategory.OTHER.value,
    )
    severity: Mapped[IncidentSeverity] = mapped_column(
        _enum(IncidentSeverity, "incident_severity"),
        nullable=False,
        server_default=IncidentSeverity.NO_HARM.value,
    )
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    related_job_id: Mapped[str | None] = mapped_column(
        sa.String, sa.ForeignKey("outreach_job.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[IncidentStatus] = mapped_column(
        _enum(IncidentStatus, "incident_status"),
        nullable=False,
        server_default=IncidentStatus.NEW.value,
    )
    occurred_hour: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class AuditLog(TimestampMixin, Base):
    """Immutable audit trail of every action (SR-05).

    Rows are append-only. ``payload_hash`` is the SHA-256 of the action's input
    for non-repudiation without storing PII in the clear.
    """

    __tablename__ = "audit_log"
    __table_args__ = (sa.Index("ix_audit_log_clinic_action", "clinic_id", "action"),)

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    actor: Mapped[str] = mapped_column(sa.String, nullable=False)
    action: Mapped[AuditAction] = mapped_column(_enum(AuditAction, "audit_action"), nullable=False)
    entity_ref: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class PromptProposal(TimestampMixin, Base):
    """Operator-authored prompt proposal awaiting governed AgentOps review."""

    __tablename__ = "prompt_proposal"
    __table_args__ = (
        sa.Index("ix_prompt_proposal_clinic_status", "clinic_id", "status"),
        sa.Index("ix_prompt_proposal_clinic_created", "clinic_id", "created_at"),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    actor: Mapped[str] = mapped_column(sa.String, nullable=False)
    status: Mapped[PromptProposalStatus] = mapped_column(
        _enum(PromptProposalStatus, "prompt_proposal_status"),
        nullable=False,
        server_default=PromptProposalStatus.SUBMITTED.value,
    )
    proposed_prompt: Mapped[str] = mapped_column(sa.Text, nullable=False)
    diff: Mapped[str] = mapped_column(sa.Text, nullable=False)


class ImportBatch(TimestampMixin, Base):
    """Metadata-only provenance for one controlled CSV import (PR-08, D3).

    Never stores raw file bytes, the original filename, row values, or
    row-level errors — only exact hashes, bounded aggregate counts, actors,
    lifecycle timestamps, and upload-disposal evidence. Raw CSV exists only
    for the duration of the authorized request.
    """

    __tablename__ = "import_batch"
    __table_args__ = (
        sa.UniqueConstraint("clinic_id", "id", name="uq_import_batch_clinic_id_id"),
        # One live (previewable or completed) batch per clinic/file/schema.
        sa.Index(
            "uq_import_batch_live_file",
            "clinic_id",
            "file_sha256",
            "schema_version",
            unique=True,
            postgresql_where=sa.text("state IN ('preview_valid', 'completed')"),
            sqlite_where=sa.text("state IN ('preview_valid', 'completed')"),
        ),
        sa.Index("ix_import_batch_clinic_state", "clinic_id", "state"),
        sa.Index("ix_import_batch_clinic_created", "clinic_id", "created_at"),
        sa.CheckConstraint(
            "length(file_sha256) = 64", name="ck_import_batch_file_hash_length"
        ),
        sa.CheckConstraint(
            "length(validation_summary_sha256) = 64",
            name="ck_import_batch_summary_hash_length",
        ),
        sa.CheckConstraint(
            "consent_policy_hash IS NULL OR length(consent_policy_hash) = 64",
            name="ck_import_batch_policy_hash_length",
        ),
        sa.CheckConstraint(
            "total_rows >= 0 AND valid_row_count >= 0 AND invalid_row_count >= 0 "
            "AND patient_count >= 0 AND appointment_count >= 0 "
            "AND error_count >= 0 AND patients_inserted >= 0 "
            "AND patients_updated >= 0 AND appointments_inserted >= 0 "
            "AND appointments_updated >= 0 AND consent_granted_count >= 0 "
            "AND consent_unknown_count >= 0 AND opt_out_count >= 0",
            name="ck_import_batch_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "valid_row_count + invalid_row_count = total_rows",
            name="ck_import_batch_row_counts_exact",
        ),
        sa.CheckConstraint(
            "error_count <= 100",
            name="ck_import_batch_error_count_bounded",
        ),
        sa.CheckConstraint(
            "patient_count <= total_rows AND appointment_count <= total_rows",
            name="ck_import_batch_counts_bounded",
        ),
        sa.CheckConstraint(
            "state != 'completed' OR ("
            "completed_at IS NOT NULL AND approved_at IS NOT NULL "
            "AND approved_by IS NOT NULL "
            "AND approval_upload_disposed_at IS NOT NULL)",
            name="ck_import_batch_completed_evidence",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= preview_requested_at",
            name="ck_import_batch_timestamp_order",
        ),
        sa.CheckConstraint(
            "preview_expires_at > preview_requested_at "
            "AND preview_upload_disposed_at <= preview_requested_at "
            "AND (approved_at IS NULL OR approved_at >= preview_requested_at) "
            "AND (approval_upload_disposed_at IS NULL OR "
            "approval_upload_disposed_at >= preview_requested_at) "
            "AND (completed_at IS NULL OR (approved_at IS NOT NULL "
            "AND completed_at >= approved_at "
            "AND approval_upload_disposed_at <= completed_at))",
            name="ck_import_batch_lifecycle_order",
        ),
        sa.CheckConstraint(
            "state != 'completed' OR ("
            "patients_inserted + patients_updated = patient_count "
            "AND appointments_inserted + appointments_updated = appointment_count)",
            name="ck_import_batch_completed_counts_exact",
        ),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    state: Mapped[ImportBatchState] = mapped_column(
        _enum(ImportBatchState, "import_batch_state"),
        nullable=False,
        server_default=ImportBatchState.PREVIEW_VALID.value,
    )
    file_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    validation_summary_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    source_system: Mapped[SourceSystem] = mapped_column(
        _enum(SourceSystem, "source_system"), nullable=False
    )
    export_at: Mapped[datetime] = mapped_column(_UTCDateTime(), nullable=False)
    # Preview lifecycle (server-derived; never client-supplied).
    preview_requested_at: Mapped[datetime] = mapped_column(_UTCDateTime(), nullable=False)
    preview_actor: Mapped[str] = mapped_column(sa.String(254), nullable=False)
    preview_expires_at: Mapped[datetime] = mapped_column(_UTCDateTime(), nullable=False)
    preview_upload_disposed_at: Mapped[datetime] = mapped_column(
        _UTCDateTime(), nullable=False
    )
    # Approval lifecycle.
    approved_at: Mapped[datetime | None] = mapped_column(_UTCDateTime(), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(sa.String(254), nullable=True)
    approval_upload_disposed_at: Mapped[datetime | None] = mapped_column(
        _UTCDateTime(), nullable=True
    )
    attestation_version: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    attested_channels: Mapped[list[str] | None] = mapped_column(sa.JSON, nullable=True)
    consent_policy_version: Mapped[str | None] = mapped_column(
        sa.String(128), nullable=True
    )
    consent_policy_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    consent_authority_granted: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    # Bounded aggregate counts only (no rows, no values).
    total_rows: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    valid_row_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    invalid_row_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    patient_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    appointment_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    error_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    error_reason_counts: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    patients_inserted: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    patients_updated: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    appointments_inserted: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    appointments_updated: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    consent_granted_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    consent_unknown_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    opt_out_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    completed_at: Mapped[datetime | None] = mapped_column(_UTCDateTime(), nullable=True)
    metadata_retention_state: Mapped[str] = mapped_column(
        sa.String(32), nullable=False, server_default="retained"
    )


class PatientSourceLink(TimestampMixin, Base):
    """Provider-qualified external identity for one patient (PR-08).

    ``Patient.source_ref`` is never rewritten to attach another system; each
    additional provider identity is a separate reviewed link. Only an active
    link may influence future sync identity resolution.
    """

    __tablename__ = "patient_source_link"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id", "id", name="uq_patient_source_link_clinic_id_id"
        ),
        sa.UniqueConstraint(
            "clinic_id", "provider", "source_ref", name="uq_patient_source_link_provider_ref"
        ),
        # At most one active link per patient/provider.
        sa.Index(
            "uq_patient_source_link_active",
            "clinic_id",
            "patient_id",
            "provider",
            unique=True,
            postgresql_where=sa.text("state = 'active'"),
            sqlite_where=sa.text("state = 'active'"),
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patient.clinic_id", "patient.id"],
            ondelete="CASCADE",
            name="fk_patient_source_link_patient_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "import_batch_id"],
            ["import_batch.clinic_id", "import_batch.id"],
            ondelete="RESTRICT",
            name="fk_patient_source_link_batch_tenant",
        ),
        sa.CheckConstraint(
            "length(evidence_hash) = 64", name="ck_patient_source_link_evidence_hash"
        ),
        sa.CheckConstraint(
            "length(source_ref) >= 1 AND length(source_ref) <= 255",
            name="ck_patient_source_link_ref_bounds",
        ),
        sa.Index("ix_patient_source_link_clinic_patient", "clinic_id", "patient_id"),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    patient_id: Mapped[str] = mapped_column(sa.String, nullable=False, index=True)
    provider: Mapped[SourceSystem] = mapped_column(
        _enum(SourceSystem, "source_system"), nullable=False
    )
    source_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    import_batch_id: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    state: Mapped[SourceLinkState] = mapped_column(
        _enum(SourceLinkState, "source_link_state"),
        nullable=False,
        server_default=SourceLinkState.ACTIVE.value,
    )
    strategy: Mapped[MatchStrategy] = mapped_column(
        _enum(MatchStrategy, "match_strategy"), nullable=False
    )
    strategy_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    resolved_by: Mapped[str] = mapped_column(sa.String(254), nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(_UTCDateTime(), nullable=False)


class ImportMatchReview(TimestampMixin, Base):
    """Deterministic provider source-match review aggregate (PR-08).

    Zero or multiple provider matches for an imported patient enter this
    queue; an operator resolves or dismisses. No raw provider payload and no
    candidate list is persisted — only counts and a candidate-evidence hash.
    """

    __tablename__ = "import_match_review"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id",
            "import_batch_id",
            "patient_id",
            "provider",
            name="uq_import_match_review_scope",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patient.clinic_id", "patient.id"],
            ondelete="CASCADE",
            name="fk_import_match_review_patient_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "import_batch_id"],
            ["import_batch.clinic_id", "import_batch.id"],
            ondelete="CASCADE",
            name="fk_import_match_review_batch_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "source_link_id"],
            ["patient_source_link.clinic_id", "patient_source_link.id"],
            ondelete="RESTRICT",
            name="fk_import_match_review_source_link_tenant",
        ),
        sa.CheckConstraint(
            "candidate_count >= 0", name="ck_import_match_review_candidates_nonnegative"
        ),
        sa.CheckConstraint(
            "candidate_evidence_hash IS NULL OR length(candidate_evidence_hash) = 64",
            name="ck_import_match_review_evidence_hash",
        ),
        sa.CheckConstraint(
            "(state = 'linked' AND source_link_id IS NOT NULL "
            "AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL) OR "
            "(state = 'dismissed' AND source_link_id IS NULL "
            "AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL) OR "
            "(state NOT IN ('linked', 'dismissed') AND source_link_id IS NULL "
            "AND resolved_by IS NULL AND resolved_at IS NULL)",
            name="ck_import_match_review_resolution_state",
        ),
        sa.Index("ix_import_match_review_clinic_state", "clinic_id", "state"),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    import_batch_id: Mapped[str] = mapped_column(sa.String, nullable=False, index=True)
    patient_id: Mapped[str] = mapped_column(sa.String, nullable=False, index=True)
    provider: Mapped[SourceSystem] = mapped_column(
        _enum(SourceSystem, "source_system"), nullable=False
    )
    strategy: Mapped[MatchStrategy] = mapped_column(
        _enum(MatchStrategy, "match_strategy"), nullable=False
    )
    strategy_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    state: Mapped[ImportMatchReviewState] = mapped_column(
        _enum(ImportMatchReviewState, "import_match_review_state"),
        nullable=False,
        server_default=ImportMatchReviewState.PENDING.value,
    )
    candidate_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    candidate_evidence_hash: Mapped[str | None] = mapped_column(
        sa.String(64), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(sa.String(254), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(_UTCDateTime(), nullable=True)
    source_link_id: Mapped[str | None] = mapped_column(sa.String, nullable=True)


class RightsAliasTombstone(TimestampMixin, Base):
    """Permanent per-alias anti-rehydration evidence (PR-08 extension of PR-10).

    One row per frozen provider alias of an erased subject, keyed by the same
    versioned HMAC vocabulary as ``RightsRequest.subject_key_hash``. Rows carry
    no raw source ref and survive patient and source-link deletion.
    """

    __tablename__ = "rights_alias_tombstone"
    __table_args__ = (
        sa.UniqueConstraint(
            "clinic_id",
            "subject_key_hash",
            name="uq_rights_alias_tombstone_subject",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "rights_request_id"],
            ["rights_request.clinic_id", "rights_request.id"],
            ondelete="RESTRICT",
            name="fk_rights_alias_tombstone_request_tenant",
        ),
        sa.CheckConstraint(
            "length(subject_key_hash) = 64",
            name="ck_rights_alias_tombstone_hash_length",
        ),
        sa.Index(
            "ix_rights_alias_tombstone_clinic_hash",
            "clinic_id",
            "subject_key_hash",
        ),
    )

    id: Mapped[str] = _pk()
    clinic_id: Mapped[str] = _clinic_fk()
    rights_request_id: Mapped[str] = mapped_column(sa.String, nullable=False, index=True)
    provider: Mapped[SourceSystem] = mapped_column(
        _enum(SourceSystem, "source_system"), nullable=False
    )
    subject_key_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    subject_key_version: Mapped[str] = mapped_column(sa.String(64), nullable=False)
