"""Server-owned, expiring identity evidence and action-tier decisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hmac import compare_digest

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .enums import (
    Channel,
    IdentityEvidenceReason,
    IdentityEvidenceState,
    IdentityFactorResult,
    IdentityTier,
)
from .models import IdentityEvidence, IdentityFactorAttempt, Patient
from .rights import SubjectFrozenError, assert_patient_writable


class IdentityPolicyMode(StrEnum):
    """Authority source for an explicitly supplied identity policy."""

    APPROVED = "approved"
    SYNTHETIC_TEST = "synthetic_test"


class IdentityProviderField(StrEnum):
    """Typed provider fields the policy may explicitly select."""

    FULL_NAME = "full_name"
    DATE_OF_BIRTH = "date_of_birth"


class IdentityAction(StrEnum):
    """Closed disclosure/action classes governed by identity evidence."""

    GENERIC_INFO = "generic_info"
    SAFETY = "safety"
    OPT_OUT = "opt_out"
    RIGHTS = "rights"
    ANONYMOUS_CALLBACK = "anonymous_callback"
    STAFF_HANDOFF = "staff_handoff"
    GENERIC_BOOKING_REQUEST = "generic_booking_request"
    AVAILABILITY_READ = "availability_read"
    PATIENT_APPOINTMENT_READ = "patient_appointment_read"
    BOOK_SLOT = "book_slot"
    RESCHEDULE = "reschedule"
    PROVIDER_EFFECT = "provider_effect"
    PROVIDER_CONFIRMATION = "provider_confirmation"


@dataclass(frozen=True)
class IdentityFactorRule:
    """One explicit factor, canonicalizer, provider field, and script."""

    factor_type: str
    independence_group: str
    provider_field: IdentityProviderField
    canonicalizer: Callable[[str], str] = field(repr=False, compare=False)
    prompt: str = field(repr=False)

    def __post_init__(self) -> None:
        if not 1 <= len(self.factor_type) <= 64:
            raise ValueError("factor_type")
        if not 1 <= len(self.independence_group) <= 64:
            raise ValueError("independence_group")
        if not callable(self.canonicalizer):
            raise ValueError("canonicalizer")
        if not self.prompt.strip():
            raise ValueError("prompt")


@dataclass(frozen=True)
class IdentityPolicy:
    """Complete versioned policy; every operational value is explicit."""

    version: str
    mode: IdentityPolicyMode
    approval_reference: str
    factors: tuple[IdentityFactorRule, ...]
    expires_after: timedelta
    max_attempts: int
    t1_match_count: int
    t2_match_count: int
    date_of_birth_factor_type: str
    mismatch_text: str = field(repr=False)
    uncertain_text: str = field(repr=False)
    handoff_text: str = field(repr=False)

    def __post_init__(self) -> None:
        if not 1 <= len(self.version) <= 128:
            raise ValueError("policy_version")
        if not self.approval_reference.strip():
            raise ValueError("approval_reference")
        if self.expires_after <= timedelta(0):
            raise ValueError("expires_after")
        if self.max_attempts < 1:
            raise ValueError("max_attempts")
        if not self.factors:
            raise ValueError("factors")
        factor_types = [factor.factor_type for factor in self.factors]
        if len(factor_types) != len(set(factor_types)):
            raise ValueError("duplicate_factor_type")
        if not 1 <= self.t1_match_count < self.t2_match_count:
            raise ValueError("tier_match_counts")
        if self.t2_match_count > len(self.factors):
            raise ValueError("t2_match_count")
        if self.max_attempts < self.t2_match_count:
            raise ValueError("max_attempts")
        dob_rules = [
            factor
            for factor in self.factors
            if factor.factor_type == self.date_of_birth_factor_type
        ]
        if (
            len(dob_rules) != 1
            or dob_rules[0].provider_field != IdentityProviderField.DATE_OF_BIRTH
        ):
            raise ValueError("date_of_birth_factor_type")
        independent_groups = {factor.independence_group for factor in self.factors}
        if len(independent_groups) < self.t2_match_count:
            raise ValueError("independent_factors")
        if not all(
            text.strip()
            for text in (self.mismatch_text, self.uncertain_text, self.handoff_text)
        ):
            raise ValueError("policy_scripts")

    def factor(self, factor_type: str) -> IdentityFactorRule | None:
        return next(
            (factor for factor in self.factors if factor.factor_type == factor_type),
            None,
        )


@dataclass(frozen=True)
class IdentityDecision:
    """Internal decision; challenge and evidence identifiers never enter model data."""

    allowed: bool
    tier: IdentityTier
    reason: IdentityEvidenceReason
    evidence_id: str | None
    challenge_token: str | None = field(default=None, repr=False)
    policy_version: str | None = None
    revision: int | None = None


@dataclass(frozen=True)
class IdentityAuthorizationContext:
    """Trusted session bindings injected by the server, never model parameters."""

    evidence_id: str
    session_id: str = field(repr=False)
    route_id: str = field(repr=False)
    channel: Channel


_TIER_RANK = {IdentityTier.T0: 0, IdentityTier.T1: 1, IdentityTier.T2: 2}
_ACTION_TIER = {
    IdentityAction.GENERIC_INFO: IdentityTier.T0,
    IdentityAction.SAFETY: IdentityTier.T0,
    IdentityAction.OPT_OUT: IdentityTier.T0,
    IdentityAction.RIGHTS: IdentityTier.T0,
    IdentityAction.ANONYMOUS_CALLBACK: IdentityTier.T0,
    IdentityAction.STAFF_HANDOFF: IdentityTier.T0,
    IdentityAction.GENERIC_BOOKING_REQUEST: IdentityTier.T1,
    IdentityAction.AVAILABILITY_READ: IdentityTier.T2,
    IdentityAction.PATIENT_APPOINTMENT_READ: IdentityTier.T2,
    IdentityAction.BOOK_SLOT: IdentityTier.T2,
    IdentityAction.RESCHEDULE: IdentityTier.T2,
    IdentityAction.PROVIDER_EFFECT: IdentityTier.T2,
    IdentityAction.PROVIDER_CONFIRMATION: IdentityTier.T2,
}


class IdentityEvidenceService:
    """Create, promote, revoke, and authorize one bound evidence session."""

    def __init__(
        self,
        *,
        policy: IdentityPolicy | None,
        clock: Callable[[], datetime],
        identifier_factory: Callable[[], str],
        challenge_factory: Callable[[], str],
    ) -> None:
        self._policy = policy
        self._clock = clock
        self._identifier_factory = identifier_factory
        self._challenge_factory = challenge_factory

    def begin(
        self,
        session: Session,
        *,
        clinic_id: str,
        session_id: str,
        route_id: str,
        channel: Channel,
        patient_id: str,
        route_possession: bool,
    ) -> IdentityDecision:
        now = self._now()
        if not route_possession:
            return self._deny(IdentityEvidenceReason.ROUTE_UNVERIFIED)
        if self._policy is None:
            return self._deny(IdentityEvidenceReason.MISSING_POLICY)
        with clinic_scope(session, clinic_id):
            patient = session.execute(
                tenant_select(Patient).where(Patient.id == patient_id)
            ).scalar_one_or_none()
            if patient is None:
                return self._deny(IdentityEvidenceReason.BINDING_MISMATCH)
            try:
                assert_patient_writable(session, clinic_id, patient_id)
            except SubjectFrozenError:
                return self._deny(IdentityEvidenceReason.RIGHTS_FROZEN)
            session_hash = _binding_hash("session", clinic_id, session_id)
            existing = session.execute(
                tenant_select(IdentityEvidence).where(
                    IdentityEvidence.session_key_hash == session_hash
                )
            ).scalar_one_or_none()
            if existing is not None:
                self._revoke(existing, now, IdentityEvidenceReason.REPLAYED)
                session.flush()
                return self._decision(existing)
            challenge = self._challenge_factory()
            evidence = IdentityEvidence(
                id=self._identifier_factory(),
                clinic_id=clinic_id,
                session_key_hash=session_hash,
                route_key_hash=_binding_hash("route", clinic_id, route_id),
                patient_key_hash=_binding_hash("patient", clinic_id, patient_id),
                channel=channel,
                policy_version=self._policy.version,
                tier=IdentityTier.T0,
                state=IdentityEvidenceState.ACTIVE,
                reason=IdentityEvidenceReason.ROUTE_ONLY,
                matched_factor_count=0,
                dob_verified=False,
                attempt_count=0,
                max_attempts=self._policy.max_attempts,
                issued_at=now,
                expires_at=now + self._policy.expires_after,
                challenge_token_hash=_token_hash(challenge),
                revision=0,
            )
            session.add(evidence)
            session.flush()
            return IdentityDecision(
                allowed=False,
                tier=IdentityTier.T0,
                reason=IdentityEvidenceReason.ROUTE_ONLY,
                evidence_id=evidence.id,
                challenge_token=challenge,
                policy_version=evidence.policy_version,
                revision=evidence.revision,
            )

    def record_factor_result(
        self,
        session: Session,
        *,
        clinic_id: str,
        evidence_id: str | None,
        session_id: str,
        route_id: str,
        channel: Channel,
        patient_id: str,
        challenge_token: str | None,
        factor_type: str,
        matched: bool,
        uncertain: bool,
    ) -> IdentityDecision:
        now = self._now()
        if self._policy is None or evidence_id is None or challenge_token is None:
            return self._deny(IdentityEvidenceReason.MISSING_POLICY)
        with clinic_scope(session, clinic_id):
            evidence = self._load_evidence(session, evidence_id, for_update=True)
            if evidence is None:
                return self._deny(IdentityEvidenceReason.BINDING_MISMATCH)
            invalid = self._validate_current(
                session,
                evidence,
                clinic_id=clinic_id,
                session_id=session_id,
                route_id=route_id,
                channel=channel,
                patient_id=patient_id,
                now=now,
            )
            if invalid is not None:
                self._revoke(evidence, now, invalid)
                session.flush()
                return self._decision(evidence)
            if (
                evidence.challenge_token_hash is None
                or not compare_digest(
                    evidence.challenge_token_hash,
                    _token_hash(challenge_token),
                )
            ):
                self._revoke(evidence, now, IdentityEvidenceReason.REPLAYED)
                session.flush()
                return self._decision(evidence)
            factor = self._policy.factor(factor_type)
            if factor is None:
                self._revoke(evidence, now, IdentityEvidenceReason.INVALID_FACTOR)
                session.flush()
                return self._decision(evidence)
            if evidence.attempt_count >= evidence.max_attempts:
                self._revoke(evidence, now, IdentityEvidenceReason.RETRY_EXHAUSTED)
                session.flush()
                return self._decision(evidence)

            if evidence.pending_factor_type is None:
                if evidence.challenge_consumed_at is not None:
                    self._revoke(evidence, now, IdentityEvidenceReason.REPLAYED)
                    session.flush()
                    return self._decision(evidence)
                evidence.attempt_count += 1
                evidence.challenge_consumed_at = now
                evidence.pending_factor_type = factor.factor_type
                evidence.revision += 1
            elif evidence.pending_factor_type != factor.factor_type:
                self._revoke(evidence, now, IdentityEvidenceReason.REPLAYED)
                session.flush()
                return self._decision(evidence)
            result = (
                IdentityFactorResult.UNCERTAIN
                if uncertain
                else IdentityFactorResult.MATCH
                if matched
                else IdentityFactorResult.NO_MATCH
            )
            session.add(
                IdentityFactorAttempt(
                    id=self._identifier_factory(),
                    clinic_id=clinic_id,
                    evidence_id=evidence.id,
                    factor_type=factor.factor_type,
                    result=result,
                    attempt_number=evidence.attempt_count,
                    policy_version=self._policy.version,
                    attempted_at=now,
                )
            )
            evidence.pending_factor_type = None
            session.flush()
            if uncertain:
                self._revoke(evidence, now, IdentityEvidenceReason.UNCERTAIN)
                session.flush()
                return self._decision(evidence)
            if not matched:
                self._revoke(evidence, now, IdentityEvidenceReason.MISMATCH)
                session.flush()
                return self._decision(evidence)

            matched_types = set(
                session.execute(
                    select(IdentityFactorAttempt.factor_type).where(
                        IdentityFactorAttempt.clinic_id == clinic_id,
                        IdentityFactorAttempt.evidence_id == evidence.id,
                        IdentityFactorAttempt.result == IdentityFactorResult.MATCH,
                        IdentityFactorAttempt.policy_version == self._policy.version,
                    )
                ).scalars()
            )
            matched_rules = [
                rule for rule in self._policy.factors if rule.factor_type in matched_types
            ]
            matched_groups = {rule.independence_group for rule in matched_rules}
            dob_verified = self._policy.date_of_birth_factor_type in matched_types
            promoted = IdentityTier.T0
            if (
                len(matched_types) >= self._policy.t2_match_count
                and len(matched_groups) >= self._policy.t2_match_count
                and dob_verified
            ):
                promoted = IdentityTier.T2
            elif len(matched_types) >= self._policy.t1_match_count:
                promoted = IdentityTier.T1
            if _TIER_RANK[promoted] > _TIER_RANK[evidence.tier]:
                evidence.tier = promoted
            evidence.matched_factor_count = len(matched_types)
            evidence.dob_verified = dob_verified
            evidence.last_verified_at = now
            evidence.reason = IdentityEvidenceReason.MATCHED

            next_challenge: str | None = None
            if evidence.tier != IdentityTier.T2:
                if evidence.attempt_count >= evidence.max_attempts:
                    self._revoke(
                        evidence,
                        now,
                        IdentityEvidenceReason.RETRY_EXHAUSTED,
                    )
                else:
                    next_challenge = self._challenge_factory()
                    evidence.challenge_token_hash = _token_hash(next_challenge)
                    evidence.challenge_consumed_at = None
            else:
                evidence.challenge_token_hash = None
            session.flush()
            return IdentityDecision(
                allowed=False,
                tier=evidence.tier,
                reason=evidence.reason,
                evidence_id=evidence.id,
                challenge_token=next_challenge,
                policy_version=evidence.policy_version,
                revision=evidence.revision,
            )

    def preflight_factor_attempt(
        self,
        session: Session,
        *,
        clinic_id: str,
        evidence_id: str | None,
        session_id: str,
        route_id: str,
        channel: Channel,
        patient_id: str,
        challenge_token: str | None,
        factor_type: str,
    ) -> IdentityDecision:
        """Reject stale, replayed, unapproved, or misbound attempts before I/O."""
        now = self._now()
        if self._policy is None or evidence_id is None or challenge_token is None:
            return self._deny(IdentityEvidenceReason.MISSING_POLICY)
        with clinic_scope(session, clinic_id):
            evidence = self._load_evidence(session, evidence_id, for_update=True)
            if evidence is None:
                return self._deny(IdentityEvidenceReason.BINDING_MISMATCH)
            invalid = self._validate_current(
                session,
                evidence,
                clinic_id=clinic_id,
                session_id=session_id,
                route_id=route_id,
                channel=channel,
                patient_id=patient_id,
                now=now,
            )
            if invalid is None and self._policy.factor(factor_type) is None:
                invalid = IdentityEvidenceReason.INVALID_FACTOR
            if invalid is None and (
                evidence.challenge_token_hash is None
                or evidence.challenge_consumed_at is not None
                or not compare_digest(
                    evidence.challenge_token_hash,
                    _token_hash(challenge_token),
                )
            ):
                invalid = IdentityEvidenceReason.REPLAYED
            if invalid is not None:
                self._revoke(evidence, now, invalid)
                session.flush()
                return self._decision(evidence)
            evidence.attempt_count += 1
            evidence.challenge_consumed_at = now
            evidence.pending_factor_type = factor_type
            evidence.revision += 1
            session.flush()
            return IdentityDecision(
                allowed=True,
                tier=evidence.tier,
                reason=IdentityEvidenceReason.AUTHORIZED,
                evidence_id=evidence.id,
                policy_version=evidence.policy_version,
                revision=evidence.revision,
            )

    def authorize(
        self,
        session: Session,
        *,
        clinic_id: str,
        evidence_id: str | None,
        session_id: str,
        route_id: str,
        channel: Channel,
        patient_id: str,
        action: IdentityAction,
    ) -> IdentityDecision:
        required = _ACTION_TIER[action]
        if required == IdentityTier.T0:
            return IdentityDecision(
                allowed=True,
                tier=IdentityTier.T0,
                reason=IdentityEvidenceReason.AUTHORIZED,
                evidence_id=None,
            )
        if self._policy is None or evidence_id is None:
            return self._deny(IdentityEvidenceReason.MISSING_POLICY)
        now = self._now()
        with clinic_scope(session, clinic_id):
            evidence = self._load_evidence(session, evidence_id, for_update=True)
            if evidence is None:
                return self._deny(IdentityEvidenceReason.BINDING_MISMATCH)
            invalid = self._validate_current(
                session,
                evidence,
                clinic_id=clinic_id,
                session_id=session_id,
                route_id=route_id,
                channel=channel,
                patient_id=patient_id,
                now=now,
            )
            if invalid is not None:
                self._revoke(evidence, now, invalid)
                session.flush()
                return self._decision(evidence)
            allowed = _TIER_RANK[evidence.tier] >= _TIER_RANK[required]
            return IdentityDecision(
                allowed=allowed,
                tier=evidence.tier,
                reason=(
                    IdentityEvidenceReason.AUTHORIZED
                    if allowed
                    else IdentityEvidenceReason.INSUFFICIENT_TIER
                ),
                evidence_id=evidence.id,
                policy_version=evidence.policy_version,
                revision=evidence.revision,
            )

    def authorize_bound_action(
        self,
        session: Session,
        *,
        clinic_id: str,
        evidence_id: str | None,
        evidence_policy_version: str | None,
        evidence_revision: int | None,
        patient_id: str,
        channel: Channel,
        action: IdentityAction,
    ) -> IdentityDecision:
        """Recheck a durable action binding without caller-supplied identifiers."""
        required = _ACTION_TIER[action]
        if required == IdentityTier.T0:
            return IdentityDecision(
                allowed=True,
                tier=IdentityTier.T0,
                reason=IdentityEvidenceReason.AUTHORIZED,
                evidence_id=None,
            )
        if (
            self._policy is None
            or evidence_id is None
            or evidence_policy_version is None
            or evidence_revision is None
        ):
            return self._deny(IdentityEvidenceReason.MISSING_POLICY)
        now = self._now()
        with clinic_scope(session, clinic_id):
            evidence = self._load_evidence(session, evidence_id, for_update=True)
            if evidence is None:
                return self._deny(IdentityEvidenceReason.BINDING_MISMATCH)
            invalid: IdentityEvidenceReason | None = None
            if evidence.state != IdentityEvidenceState.ACTIVE:
                invalid = IdentityEvidenceReason.REVOKED
            elif (
                evidence.policy_version != self._policy.version
                or evidence_policy_version != self._policy.version
            ):
                invalid = IdentityEvidenceReason.STALE_POLICY
            elif evidence.expires_at <= now:
                invalid = IdentityEvidenceReason.EXPIRED
            elif (
                evidence.revision != evidence_revision
                or evidence.channel != channel
                or not compare_digest(
                    evidence.patient_key_hash,
                    _binding_hash("patient", clinic_id, patient_id),
                )
            ):
                invalid = IdentityEvidenceReason.BINDING_MISMATCH
            else:
                try:
                    assert_patient_writable(session, clinic_id, patient_id)
                except SubjectFrozenError:
                    invalid = IdentityEvidenceReason.RIGHTS_FROZEN
            if invalid is not None:
                if evidence.state == IdentityEvidenceState.ACTIVE:
                    self._revoke(evidence, now, invalid)
                    session.flush()
                return self._decision(evidence)
            allowed = _TIER_RANK[evidence.tier] >= _TIER_RANK[required]
            return IdentityDecision(
                allowed=allowed,
                tier=evidence.tier,
                reason=(
                    IdentityEvidenceReason.AUTHORIZED
                    if allowed
                    else IdentityEvidenceReason.INSUFFICIENT_TIER
                ),
                evidence_id=evidence.id,
                policy_version=evidence.policy_version,
                revision=evidence.revision,
            )

    def revoke(
        self,
        session: Session,
        *,
        clinic_id: str,
        evidence_id: str,
        reason: IdentityEvidenceReason,
    ) -> IdentityDecision:
        """Atomically downgrade one evidence session to T0."""
        if reason in {
            IdentityEvidenceReason.AUTHORIZED,
            IdentityEvidenceReason.MATCHED,
            IdentityEvidenceReason.ROUTE_ONLY,
        }:
            raise ValueError("revocation_reason")
        now = self._now()
        with clinic_scope(session, clinic_id):
            evidence = self._load_evidence(session, evidence_id, for_update=True)
            if evidence is None:
                return self._deny(IdentityEvidenceReason.BINDING_MISMATCH)
            if evidence.state == IdentityEvidenceState.ACTIVE:
                self._revoke(evidence, now, reason)
                session.flush()
            return self._decision(evidence)

    def _validate_current(
        self,
        session: Session,
        evidence: IdentityEvidence,
        *,
        clinic_id: str,
        session_id: str,
        route_id: str,
        channel: Channel,
        patient_id: str,
        now: datetime,
    ) -> IdentityEvidenceReason | None:
        assert self._policy is not None
        if evidence.state != IdentityEvidenceState.ACTIVE:
            return IdentityEvidenceReason.REVOKED
        if evidence.policy_version != self._policy.version:
            return IdentityEvidenceReason.STALE_POLICY
        if evidence.expires_at <= now:
            return IdentityEvidenceReason.EXPIRED
        if not (
            compare_digest(
                evidence.session_key_hash,
                _binding_hash("session", clinic_id, session_id),
            )
            and compare_digest(
                evidence.route_key_hash,
                _binding_hash("route", clinic_id, route_id),
            )
            and compare_digest(
                evidence.patient_key_hash,
                _binding_hash("patient", clinic_id, patient_id),
            )
            and evidence.channel == channel
        ):
            return IdentityEvidenceReason.BINDING_MISMATCH
        try:
            assert_patient_writable(session, clinic_id, patient_id)
        except SubjectFrozenError:
            return IdentityEvidenceReason.RIGHTS_FROZEN
        return None

    @staticmethod
    def _load_evidence(
        session: Session,
        evidence_id: str,
        *,
        for_update: bool,
    ) -> IdentityEvidence | None:
        query = tenant_select(IdentityEvidence).where(IdentityEvidence.id == evidence_id)
        if for_update and session.bind is not None and session.bind.dialect.name == "postgresql":
            query = query.with_for_update()
        return session.execute(query).scalar_one_or_none()

    @staticmethod
    def _revoke(
        evidence: IdentityEvidence,
        now: datetime,
        reason: IdentityEvidenceReason,
    ) -> None:
        evidence.tier = IdentityTier.T0
        evidence.state = IdentityEvidenceState.REVOKED
        evidence.reason = reason
        evidence.revoked_at = now
        evidence.challenge_token_hash = None
        evidence.challenge_consumed_at = None
        evidence.pending_factor_type = None
        evidence.revision += 1

    @staticmethod
    def _decision(evidence: IdentityEvidence) -> IdentityDecision:
        return IdentityDecision(
            allowed=False,
            tier=IdentityTier.T0 if evidence.state != IdentityEvidenceState.ACTIVE else evidence.tier,
            reason=evidence.reason,
            evidence_id=evidence.id,
            policy_version=evidence.policy_version,
            revision=evidence.revision,
        )

    @staticmethod
    def _deny(reason: IdentityEvidenceReason) -> IdentityDecision:
        return IdentityDecision(
            allowed=False,
            tier=IdentityTier.T0,
            reason=reason,
            evidence_id=None,
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(UTC)


def _binding_hash(kind: str, clinic_id: str, value: str) -> str:
    if not value:
        raise ValueError(f"{kind}_binding")
    encoded = json.dumps(
        {"clinic_id": clinic_id, "kind": kind, "value": value},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _token_hash(value: str) -> str:
    if not value:
        raise ValueError("challenge_token")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "IdentityAction",
    "IdentityAuthorizationContext",
    "IdentityDecision",
    "IdentityEvidenceReason",
    "IdentityEvidenceService",
    "IdentityFactorRule",
    "IdentityPolicy",
    "IdentityPolicyMode",
    "IdentityProviderField",
    "IdentityTier",
]