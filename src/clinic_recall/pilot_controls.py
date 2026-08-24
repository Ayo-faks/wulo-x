"""Deterministic programme, cohort, and operational-stop controls."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .enums import (
    AuditAction,
    BookingWriteBackState,
    CampaignStatus,
    Channel,
    ExternalEffectState,
    ExternalEffectType,
    PilotProgrammeState,
)
from .messaging.audit import audit_action
from .models import (
    BookingAction,
    Campaign,
    Clinic,
    ExternalEffect,
    OutreachJob,
    Patient,
    PilotParticipant,
    PilotProgramme,
)
from .rights import assert_patient_writable
from .sync.base import make_id
from .telemetry import emit_runtime_event

PILOT_MAXIMUM_PATIENTS = 50
CUMULATIVE_LIMITS = (0, 5, 15, 30, 50)
_NEXT_CUMULATIVE_LIMIT = {0: 5, 5: 15, 15: 30, 30: 50}
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class PilotControlError(ValueError):
    """A requested pilot transition would violate a closed invariant."""


def _invariant_violation(reason_code: str, message: str) -> PilotControlError:
    """Record one aggregate cohort-invariant breach, then fail closed.

    Emission is non-transactional and best-effort: the violating transaction
    still rolls back, and telemetry failure never changes the raised error.
    """
    try:
        emit_runtime_event(
            "pilot.invariant.violation",
            {"reason_code": reason_code, "count": 1},
        )
    except Exception:
        return PilotControlError(message)
    return PilotControlError(message)


@dataclass(frozen=True)
class OperationalSwitchSnapshot:
    """One atomically refreshed operational configuration observation."""

    outreach_enabled: bool
    voice_enabled: bool
    recording_enabled: bool
    refreshed_at: datetime | None
    max_age: timedelta
    environment: str | None = None
    release_identity: str | None = None

    def decision(self, channel: Channel, now: datetime) -> PilotGateDecision:
        """Evaluate freshness and the switches required for one channel."""
        blocked = self._configuration_block(now)
        if blocked is not None:
            return blocked
        if not self.outreach_enabled:
            return PilotGateDecision(False, "outreach_switch_disabled")
        if channel == Channel.CALL and not self.voice_enabled:
            return PilotGateDecision(False, "voice_switch_disabled")
        if channel not in {Channel.SMS, Channel.CALL}:
            return PilotGateDecision(False, "pilot_channel_not_allowed")
        return PilotGateDecision(True, "allowed")

    def recording_decision(self, now: datetime) -> PilotGateDecision:
        """Require all three fresh operational switches before recording."""
        blocked = self._configuration_block(now)
        if blocked is not None:
            return blocked
        if not self.outreach_enabled:
            return PilotGateDecision(False, "outreach_switch_disabled")
        if not self.voice_enabled:
            return PilotGateDecision(False, "voice_switch_disabled")
        if not self.recording_enabled:
            return PilotGateDecision(False, "recording_switch_disabled")
        return PilotGateDecision(True, "allowed")

    def _configuration_block(self, now: datetime) -> PilotGateDecision | None:
        _require_aware(now)
        if not self.environment or not self.release_identity:
            return PilotGateDecision(False, "configuration_identity_missing")
        if (
            self.refreshed_at is None
            or self.refreshed_at.tzinfo is None
            or self.refreshed_at.utcoffset() is None
            or self.max_age <= timedelta(0)
            or self.max_age > timedelta(hours=1)
        ):
            return PilotGateDecision(False, "configuration_evidence_missing")
        age = now.astimezone(UTC) - self.refreshed_at.astimezone(UTC)
        if age < timedelta(0) or age > self.max_age:
            return PilotGateDecision(False, "configuration_stale")
        return None


@dataclass(frozen=True)
class PilotGateDecision:
    """Reason-coded conjunction of operational and database pilot controls."""

    allowed: bool
    reason: str
    programme_id: str | None = None
    participant_id: str | None = None


PatientPilotGate = Callable[
    [Session, str, str, Channel, datetime],
    PilotGateDecision,
]
JobPilotGate = Callable[
    [Session, str, OutreachJob, datetime],
    PilotGateDecision,
]


def pilot_gate_decision(value: PilotGateDecision | bool) -> PilotGateDecision:
    """Preserve legacy boolean seams while retaining closed PR-13 reasons."""
    if isinstance(value, PilotGateDecision):
        return value
    return PilotGateDecision(
        bool(value),
        "allowed" if value else "programme_gate_unbound",
    )


def operational_switch_snapshot_from_environment(
    environment: Mapping[str, str] | None = None,
) -> OperationalSwitchSnapshot:
    """Parse one immutable non-secret snapshot; malformed values remain false."""
    values = environment if environment is not None else os.environ
    pilot_environment = values.get("CLINIC_RECALL_PILOT_ENVIRONMENT", "").strip().lower()
    release_identity = values.get("CLINIC_RECALL_PILOT_RELEASE_IDENTITY", "").strip()
    raw_refreshed_at = values.get("CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT", "")
    try:
        refreshed_at = datetime.fromisoformat(raw_refreshed_at.replace("Z", "+00:00"))
    except ValueError:
        refreshed_at = None
    try:
        max_age_seconds = int(values.get("CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS", ""))
    except ValueError:
        max_age_seconds = 0
    return OperationalSwitchSnapshot(
        outreach_enabled=_explicit_true(values.get("CLINIC_RECALL_PILOT_OUTREACH_ENABLED")),
        voice_enabled=_explicit_true(values.get("CLINIC_RECALL_PILOT_VOICE_ENABLED")),
        recording_enabled=_explicit_true(values.get("CLINIC_RECALL_PILOT_RECORDING_ENABLED")),
        refreshed_at=refreshed_at,
        max_age=timedelta(seconds=max_age_seconds),
        environment=pilot_environment or None,
        release_identity=release_identity or None,
    )


def patient_gate_for_snapshot(
    switches: OperationalSwitchSnapshot,
) -> PatientPilotGate:
    """Bind one immutable operational snapshot to the database patient gate."""

    def gate(
        session: Session,
        clinic_id: str,
        patient_id: str,
        channel: Channel,
        now: datetime,
    ) -> PilotGateDecision:
        return evaluate_patient_gate(
            session,
            clinic_id=clinic_id,
            patient_id=patient_id,
            channel=channel,
            switches=switches,
            now=now,
        )

    return gate


def job_gate_for_snapshot(
    switches: OperationalSwitchSnapshot,
    channel: Channel,
) -> JobPilotGate:
    """Bind campaign, participant, programme, and operational checks to a job."""

    def gate(
        session: Session,
        clinic_id: str,
        job: OutreachJob,
        now: datetime,
    ) -> PilotGateDecision:
        campaign_active = session.execute(
            tenant_select(Campaign)
            .with_only_columns(Campaign.id)
            .where(
                Campaign.id == job.campaign_id,
                Campaign.status == CampaignStatus.ACTIVE,
            )
        ).first()
        if campaign_active is None:
            return PilotGateDecision(False, "campaign_not_active")
        return evaluate_patient_gate(
            session,
            clinic_id=clinic_id,
            patient_id=job.patient_id,
            channel=channel,
            switches=switches,
            now=now,
        )

    return gate


def create_programme(
    session: Session,
    *,
    clinic_id: str,
    programme_id: str,
    environment: str,
    release_identity: str,
) -> PilotProgramme:
    """Create or return one exact tenant/release programme in draft state."""
    environment = environment.strip().lower()
    release_identity = release_identity.strip()
    if not clinic_id or not programme_id:
        raise PilotControlError("clinic_id and programme_id are required")
    if not 1 <= len(environment) <= 32:
        raise PilotControlError("environment must contain 1 to 32 characters")
    if not 1 <= len(release_identity) <= 200:
        raise PilotControlError("release_identity must contain 1 to 200 characters")

    with clinic_scope(session, clinic_id):
        if session.get(Clinic, clinic_id) is None:
            raise PilotControlError("clinic not found")
        existing_by_id = session.execute(
            tenant_select(PilotProgramme).where(PilotProgramme.id == programme_id)
        ).scalar_one_or_none()
        if existing_by_id is not None:
            if (
                existing_by_id.environment != environment
                or existing_by_id.release_identity != release_identity
            ):
                raise _invariant_violation(
                    "programme_release_conflict",
                    "programme id already belongs to another release",
                )
            return existing_by_id
        existing = session.execute(
            tenant_select(PilotProgramme).where(
                PilotProgramme.environment == environment,
                PilotProgramme.release_identity == release_identity,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.id != programme_id:
                raise _invariant_violation(
                    "programme_release_conflict",
                    "release identity already belongs to another programme",
                )
            return existing
        programme = PilotProgramme(
            id=programme_id,
            clinic_id=clinic_id,
            environment=environment,
            release_identity=release_identity,
            state=PilotProgrammeState.DRAFT,
            maximum_unique_patients=PILOT_MAXIMUM_PATIENTS,
            active_cumulative_limit=0,
        )
        session.add(programme)
        session.flush()
        return programme


def enroll_participant(
    session: Session,
    *,
    clinic_id: str,
    programme_id: str,
    patient_id: str,
    now: datetime,
) -> PilotParticipant:
    """Append one unique participant while holding the programme row lock."""
    _require_aware(now)
    with clinic_scope(session, clinic_id):
        programme = _lock_programme(session, programme_id)
        if programme.state in {PilotProgrammeState.PAUSED, PilotProgrammeState.CLOSED}:
            raise _invariant_violation(
                "programme_state_invalid",
                "paused or closed programme cannot enroll patients",
            )
        patient = session.execute(
            tenant_select(Patient).where(Patient.id == patient_id)
        ).scalar_one_or_none()
        if patient is None:
            raise PilotControlError("patient not found")
        assert_patient_writable(session, clinic_id, patient.id)
        patient_key_hash = _patient_key_hash(clinic_id, patient_id)
        existing = session.execute(
            tenant_select(PilotParticipant).where(
                PilotParticipant.pilot_programme_id == programme_id,
                PilotParticipant.patient_key_hash == patient_key_hash,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        maximum_ordinal = session.scalar(
            select(func.max(PilotParticipant.ordinal)).where(
                PilotParticipant.clinic_id == clinic_id,
                PilotParticipant.pilot_programme_id == programme_id,
            )
        )
        ordinal = int(maximum_ordinal or 0) + 1
        if ordinal > programme.maximum_unique_patients:
            raise _invariant_violation(
                "cohort_limit_exceeded",
                "pilot programme is limited to 50 unique patients",
            )
        participant = PilotParticipant(
            id=make_id("pilot-participant", clinic_id, f"{programme_id}:{patient_id}"),
            clinic_id=clinic_id,
            pilot_programme_id=programme_id,
            patient_id=patient_id,
            patient_key_hash=patient_key_hash,
            ordinal=ordinal,
            wave=_wave_for_ordinal(ordinal),
            enrolled_at=now.astimezone(UTC),
        )
        session.add(participant)
        session.flush()
        return participant


def release_cumulative_limit(
    session: Session,
    *,
    clinic_id: str,
    programme_id: str,
    cumulative_limit: int,
    actor: str,
    evidence_hash: str,
    now: datetime,
) -> PilotProgramme:
    """Release exactly the next cumulative wave under the programme lock."""
    _require_aware(now)
    actor = actor.strip()
    evidence_hash = evidence_hash.strip().lower()
    if not actor or len(actor) > 200:
        raise PilotControlError("release actor is required")
    if len(evidence_hash) != 64 or any(
        character not in "0123456789abcdef" for character in evidence_hash
    ):
        raise PilotControlError("release evidence must be a SHA-256 hex digest")

    with clinic_scope(session, clinic_id):
        programme = _lock_programme(session, programme_id)
        if programme.state in {PilotProgrammeState.PAUSED, PilotProgrammeState.CLOSED}:
            raise _invariant_violation(
                "programme_state_invalid",
                "paused or closed programme cannot release a wave",
            )
        if programme.active_cumulative_limit == 0 and programme.state != PilotProgrammeState.DARK:
            raise _invariant_violation(
                "programme_state_invalid",
                "Wave 1 requires a dark-qualified programme",
            )
        if programme.active_cumulative_limit > 0 and programme.state != PilotProgrammeState.ACTIVE:
            raise _invariant_violation(
                "programme_state_invalid",
                "later waves require an active programme",
            )
        expected = _NEXT_CUMULATIVE_LIMIT.get(programme.active_cumulative_limit)
        if expected is None:
            raise _invariant_violation(
                "cumulative_limit_invalid",
                "pilot programme is already at terminal cumulative limit",
            )
        if cumulative_limit != expected:
            raise _invariant_violation(
                "cumulative_limit_invalid",
                f"next cumulative limit must be {expected}",
            )
        participant_count = int(
            session.scalar(
                select(func.count())
                .select_from(PilotParticipant)
                .where(
                    PilotParticipant.clinic_id == clinic_id,
                    PilotParticipant.pilot_programme_id == programme_id,
                    PilotParticipant.ordinal <= cumulative_limit,
                )
            )
            or 0
        )
        if participant_count != cumulative_limit:
            raise _invariant_violation(
                "cumulative_limit_invalid",
                "cumulative release requires every preceding ordinal",
            )

        released_at = now.astimezone(UTC)
        participants = list(
            session.execute(
                tenant_select(PilotParticipant).where(
                    PilotParticipant.pilot_programme_id == programme_id,
                    PilotParticipant.ordinal <= cumulative_limit,
                )
            ).scalars()
        )
        for participant in participants:
            if participant.released_at is None:
                participant.released_at = released_at
        session.flush()
        programme.active_cumulative_limit = cumulative_limit
        programme.state = PilotProgrammeState.ACTIVE
        programme.released_at = released_at
        programme.released_by = actor
        programme.release_evidence_hash = evidence_hash
        programme.paused_at = None
        programme.paused_by = None
        programme.pause_reason = None
        audit_action(
            session,
            clinic_id,
            AuditAction.APPROVE,
            f"{programme.id}:wave:{cumulative_limit}",
            {
                "programme_id": programme.id,
                "cumulative_limit": cumulative_limit,
                "evidence_hash": evidence_hash,
                "occurred_at": released_at,
            },
            actor=actor,
        )
        session.flush()
        return programme


def mark_programme_dark(
    session: Session,
    *,
    clinic_id: str,
    programme_id: str,
    actor: str,
    evidence_hash: str,
    now: datetime,
) -> PilotProgramme:
    """Move one draft programme into dark qualification with evidence."""
    _require_aware(now)
    actor = actor.strip()
    evidence_hash = evidence_hash.strip().lower()
    if not actor or len(actor) > 200:
        raise PilotControlError("dark actor is required")
    if len(evidence_hash) != 64 or any(
        character not in "0123456789abcdef" for character in evidence_hash
    ):
        raise PilotControlError("dark evidence must be a SHA-256 hex digest")
    with clinic_scope(session, clinic_id):
        programme = _lock_programme(session, programme_id)
        if programme.state == PilotProgrammeState.DARK:
            return programme
        if programme.state != PilotProgrammeState.DRAFT:
            raise _invariant_violation(
                "programme_state_invalid",
                "only a draft programme can enter dark state",
            )
        programme.state = PilotProgrammeState.DARK
        audit_action(
            session,
            clinic_id,
            AuditAction.APPROVE,
            f"{programme.id}:dark",
            {
                "programme_id": programme.id,
                "evidence_hash": evidence_hash,
                "occurred_at": now.astimezone(UTC),
            },
            actor=actor,
        )
        session.flush()
        return programme


def pause_programme(
    session: Session,
    *,
    clinic_id: str,
    programme_id: str,
    actor: str,
    reason: str,
    now: datetime,
) -> PilotProgramme:
    """Pause one programme and cancel only work not yet provider-bound."""
    _require_aware(now)
    actor = actor.strip()
    reason = reason.strip().lower()
    if not actor or len(actor) > 200:
        raise PilotControlError("pause actor is required")
    if not _REASON_CODE.fullmatch(reason):
        raise PilotControlError("pause reason must be a bounded reason code")

    with clinic_scope(session, clinic_id):
        programme = _lock_programme(session, programme_id)
        if programme.state == PilotProgrammeState.CLOSED:
            raise _invariant_violation(
                "programme_state_invalid",
                "closed programme cannot be paused",
            )
        paused_at = programme.paused_at or now.astimezone(UTC)
        if paused_at.tzinfo is None or paused_at.utcoffset() is None:
            paused_at = paused_at.replace(tzinfo=UTC)
        else:
            paused_at = paused_at.astimezone(UTC)
        if programme.state != PilotProgrammeState.PAUSED:
            programme.state = PilotProgrammeState.PAUSED
            programme.paused_at = paused_at
            programme.paused_by = actor
            programme.pause_reason = reason

        patient_ids = select(PilotParticipant.patient_id).where(
            PilotParticipant.clinic_id == clinic_id,
            PilotParticipant.pilot_programme_id == programme_id,
            PilotParticipant.patient_id.is_not(None),
        )
        job_ids = select(OutreachJob.id).where(
            OutreachJob.clinic_id == clinic_id,
            OutreachJob.patient_id.in_(patient_ids),
        )
        booking_action_ids = select(BookingAction.id).where(
            BookingAction.clinic_id == clinic_id, BookingAction.outreach_job_id.in_(job_ids)
        )
        effect_statement = tenant_select(ExternalEffect).where(
            sa.or_(
                sa.and_(
                    ExternalEffect.aggregate_type == "outreach_job",
                    ExternalEffect.aggregate_id.in_(job_ids),
                    ExternalEffect.state.in_(
                        {
                            ExternalEffectState.PENDING,
                            ExternalEffectState.LEASED,
                        }
                    ),
                ),
                sa.and_(
                    ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING,
                    ExternalEffect.aggregate_type == "booking_action",
                    ExternalEffect.aggregate_id.in_(booking_action_ids),
                    ExternalEffect.state.in_(
                        {
                            ExternalEffectState.PENDING,
                            ExternalEffectState.LEASED,
                            ExternalEffectState.DISPATCHING,
                        }
                    ),
                ),
            ),
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            effect_statement = effect_statement.with_for_update()
        effects = list(session.execute(effect_statement).scalars())
        cliniko_action_ids = {
            effect.aggregate_id
            for effect in effects
            if effect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            and effect.aggregate_type == "booking_action"
        }
        booking_actions: list[BookingAction] = []
        if cliniko_action_ids:
            booking_statement = tenant_select(BookingAction).where(
                BookingAction.id.in_(cliniko_action_ids)
            )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                booking_statement = booking_statement.with_for_update()
            booking_actions = list(session.execute(booking_statement).scalars())
        booking_actions_by_id = {action.id: action for action in booking_actions}
        for effect in effects:
            booking_action = booking_actions_by_id.get(effect.aggregate_id)
            if (
                effect.effect_type == ExternalEffectType.CLINIKO_BOOKING
                and effect.state == ExternalEffectState.DISPATCHING
            ):
                effect.state = ExternalEffectState.RECONCILE_REQUIRED
                effect.provider_status = "provider_outcome_unknown"
                effect.last_error_class = "AmbiguousDispatch"
                effect.last_error_code = "pilot_programme_paused"
                effect.lease_owner = None
                effect.lease_expires_at = None
                if booking_action is not None:
                    booking_action.write_back_state = BookingWriteBackState.RECONCILE_REQUIRED
                continue
            effect.state = ExternalEffectState.CANCELED
            effect.provider_status = "not_dispatched"
            effect.last_error_class = "DispatchCanceled"
            effect.last_error_code = "pilot_programme_paused"
            effect.completed_at = paused_at
            effect.lease_owner = None
            effect.lease_expires_at = None
            if booking_action is not None:
                booking_action.write_back_state = BookingWriteBackState.REJECTED
        from .recording import enforce_recording_switch_off

        enforce_recording_switch_off(
            session,
            clinic_id=clinic_id,
            now=paused_at,
            reason_code="pilot_programme_paused",
        )
        session.flush()
        return programme


def close_programme(
    session: Session,
    *,
    clinic_id: str,
    programme_id: str,
    actor: str,
    reason: str,
    now: datetime,
) -> PilotProgramme:
    """Close one programme terminally and cancel any late undispatched work."""
    programme = pause_programme(
        session,
        clinic_id=clinic_id,
        programme_id=programme_id,
        actor=actor,
        reason=reason,
        now=now,
    )
    with clinic_scope(session, clinic_id):
        programme = _lock_programme(session, programme_id)
        programme.state = PilotProgrammeState.CLOSED
        audit_action(
            session,
            clinic_id,
            AuditAction.APPROVE,
            f"{programme.id}:close",
            {
                "programme_id": programme.id,
                "reason": reason,
                "occurred_at": now.astimezone(UTC),
            },
            actor=actor,
        )
        session.flush()
        return programme


def mark_participant_contact_started(
    session: Session,
    *,
    clinic_id: str,
    participant_id: str,
    now: datetime,
) -> PilotParticipant:
    """Persist the first provider-accepted contact exactly once."""
    _require_aware(now)
    with clinic_scope(session, clinic_id):
        statement = tenant_select(PilotParticipant).where(PilotParticipant.id == participant_id)
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update()
        participant = session.execute(statement).scalar_one_or_none()
        if participant is None:
            raise PilotControlError("pilot participant not found")
        if participant.released_at is None:
            raise _invariant_violation(
                "participant_wave_invalid",
                "unreleased participant cannot be contacted",
            )
        if participant.first_contact_at is None:
            participant.first_contact_at = now.astimezone(UTC)
            session.flush()
        return participant


def evaluate_patient_gate(
    session: Session,
    *,
    clinic_id: str,
    patient_id: str,
    channel: Channel,
    switches: OperationalSwitchSnapshot,
    now: datetime,
) -> PilotGateDecision:
    """Require a fresh switch observation and an active released participant."""
    operational = switches.decision(channel, now)
    if not operational.allowed:
        return operational
    with clinic_scope(session, clinic_id):
        programmes = list(
            session.execute(
                tenant_select(PilotProgramme).where(
                    PilotProgramme.environment == switches.environment,
                    PilotProgramme.release_identity == switches.release_identity,
                )
            ).scalars()
        )
        if len(programmes) != 1:
            return PilotGateDecision(False, "programme_missing_or_ambiguous")
        programme = programmes[0]
        if programme.state != PilotProgrammeState.ACTIVE:
            return PilotGateDecision(False, "programme_not_active", programme.id)
        participant = session.execute(
            tenant_select(PilotParticipant).where(
                PilotParticipant.pilot_programme_id == programme.id,
                PilotParticipant.patient_key_hash == _patient_key_hash(clinic_id, patient_id),
            )
        ).scalar_one_or_none()
        if participant is None:
            return PilotGateDecision(False, "participant_not_enrolled", programme.id)
        if participant.patient_id is None:
            return PilotGateDecision(
                False,
                "participant_identity_erased",
                programme.id,
                participant.id,
            )
        if participant.released_at is None:
            return PilotGateDecision(
                False,
                "participant_not_released",
                programme.id,
                participant.id,
            )
        if participant.ordinal > programme.active_cumulative_limit:
            return PilotGateDecision(
                False,
                "participant_out_of_wave",
                programme.id,
                participant.id,
            )
        return PilotGateDecision(True, "allowed", programme.id, participant.id)


def evaluate_recording_gate(
    session: Session,
    *,
    clinic_id: str,
    switches: OperationalSwitchSnapshot,
    now: datetime,
) -> PilotGateDecision:
    """Require the independent recording switch and one exact active programme."""
    operational = switches.recording_decision(now)
    if not operational.allowed:
        return operational
    with clinic_scope(session, clinic_id):
        programmes = list(
            session.execute(
                tenant_select(PilotProgramme).where(
                    PilotProgramme.environment == switches.environment,
                    PilotProgramme.release_identity == switches.release_identity,
                )
            ).scalars()
        )
        if len(programmes) != 1:
            return PilotGateDecision(False, "programme_missing_or_ambiguous")
        programme = programmes[0]
        if programme.state != PilotProgrammeState.ACTIVE:
            return PilotGateDecision(False, "programme_not_active", programme.id)
        return PilotGateDecision(True, "allowed", programme.id)


def _lock_programme(session: Session, programme_id: str) -> PilotProgramme:
    statement = tenant_select(PilotProgramme).where(PilotProgramme.id == programme_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    programme = session.execute(statement).scalar_one_or_none()
    if programme is None:
        raise PilotControlError("pilot programme not found")
    return programme


def _wave_for_ordinal(ordinal: int) -> int:
    if ordinal <= 5:
        return 1
    if ordinal <= 15:
        return 2
    if ordinal <= 30:
        return 3
    if ordinal <= 50:
        return 4
    raise _invariant_violation(
        "cohort_limit_exceeded",
        "participant ordinal exceeds the pilot maximum",
    )


def _patient_key_hash(clinic_id: str, patient_id: str) -> str:
    return hashlib.sha256(f"{clinic_id}\0{patient_id}".encode()).hexdigest()


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PilotControlError("timestamp must be timezone-aware")


def _explicit_true(value: str | None) -> bool:
    return bool(value and value.strip().lower() in _TRUE_VALUES)
