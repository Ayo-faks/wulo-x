"""Deterministic provider source matching and review for CSV imports (PR-08).

Read-only toward providers: callers materialize candidate snapshots outside
database scope and pass them in. Only an exact provider source-reference
equality may auto-link; zero and multiple candidates always enter operator
review. No fuzzy matching, no first-match fallback, and never a booking,
outreach, or enrollment side effect.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from ..db import clinic_scope, tenant_select
from ..enums import (
    AuditAction,
    ImportMatchReviewState,
    MatchStrategy,
    SourceLinkState,
    SourceSystem,
)
from ..messaging.audit import audit_action
from ..models import ImportMatchReview, Patient, PatientSourceLink
from ..rights import (
    SubjectKeyring,
    assert_patient_writable,
    assert_source_writable,
)

MATCH_STRATEGY_VERSION = "v1"
CANDIDATE_TOKEN_TTL = timedelta(minutes=5)

_RESOLVED_STATES = frozenset({ImportMatchReviewState.LINKED, ImportMatchReviewState.DISMISSED})


class SourceMatchError(RuntimeError):
    """A bounded, reason-coded matching/review failure."""

    _REASONS = frozenset(
        {
            "review_not_found",
            "review_already_resolved",
            "candidate_mismatch",
            "subject_frozen",
            "link_conflict",
            "invalid_action",
            "matching_disabled",
        }
    )

    def __init__(self, reason: str) -> None:
        if reason not in self._REASONS:
            raise ValueError(f"unknown matching failure reason: {reason!r}")
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ProviderPatientSnapshot:
    """One minimal, already-materialized provider candidate."""

    provider: SourceSystem
    source_ref: str
    active: bool = True


@dataclass(frozen=True)
class CandidateTokenOption:
    """One opaque, short-lived candidate choice returned to an operator."""

    token: str
    ordinal: int
    active: bool
    expires_at: datetime


@dataclass(frozen=True)
class MatchingRunResult:
    """Aggregate outcome of one matching pass over an imported batch."""

    reviewed: int
    linked: int
    unmatched: int
    ambiguous: int
    pending: int


def candidate_evidence_hash(candidates: Sequence[ProviderPatientSnapshot]) -> str:
    """One-way canonical hash of a candidate set (no raw refs persisted)."""
    payload = json.dumps(
        sorted(
            (candidate.provider.value, candidate.source_ref, candidate.active)
            for candidate in candidates
        ),
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def issue_candidate_tokens(
    review: ImportMatchReview,
    candidates: Sequence[ProviderPatientSnapshot],
    *,
    keyring: SubjectKeyring,
    now: datetime,
    ttl: timedelta = CANDIDATE_TOKEN_TTL,
) -> tuple[CandidateTokenOption, ...]:
    """Issue opaque tokens bound to the current reviewed candidate evidence.

    The token payload contains clinic/review/provider/evidence/ordinal/expiry,
    but never the provider source reference. Its signature includes that
    reference; resolution can therefore recover membership only by comparing
    against a freshly materialized provider candidate set.
    """
    now = _aware(now, "now")
    if ttl <= timedelta(0):
        raise ValueError("candidate token ttl must be positive")
    evidence = candidate_evidence_hash(candidates)
    if (
        not candidates
        or review.candidate_evidence_hash != evidence
        or review.candidate_count != len(candidates)
        or any(candidate.provider != review.provider for candidate in candidates)
    ):
        raise SourceMatchError("candidate_mismatch")
    expires_at = now + ttl
    return tuple(
        CandidateTokenOption(
            token=_candidate_token(
                review,
                candidate,
                ordinal=ordinal,
                expires_at=expires_at,
                key=keyring.current,
            ),
            ordinal=ordinal,
            active=candidate.active,
            expires_at=expires_at,
        )
        for ordinal, candidate in enumerate(candidates, start=1)
    )


def resolve_candidate_token(
    review: ImportMatchReview,
    token: str,
    candidates: Sequence[ProviderPatientSnapshot],
    *,
    keyring: SubjectKeyring,
    now: datetime,
) -> str:
    """Resolve a token only against the current, evidence-identical snapshot."""
    now = _aware(now, "now")
    if len(token) > 2048:
        raise SourceMatchError("candidate_mismatch")
    try:
        encoded, supplied_signature = token.split(".", 1)
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        if set(payload) != {
            "v",
            "clinic_id",
            "review_id",
            "provider",
            "evidence",
            "ordinal",
            "expires_at",
            "key_version",
        }:
            raise ValueError
        expires_at = datetime.fromtimestamp(int(payload["expires_at"]), tz=UTC)
        ordinal = int(payload["ordinal"])
    except (ValueError, TypeError, KeyError, binascii.Error):
        raise SourceMatchError("candidate_mismatch") from None
    if (
        payload["v"] != 1
        or payload["clinic_id"] != review.clinic_id
        or payload["review_id"] != review.id
        or payload["provider"] != review.provider.value
        or payload["evidence"] != review.candidate_evidence_hash
        or expires_at <= now
        or ordinal < 1
        or ordinal > len(candidates)
        or candidate_evidence_hash(candidates) != review.candidate_evidence_hash
        or review.candidate_count != len(candidates)
    ):
        raise SourceMatchError("candidate_mismatch")
    key = next(
        (key for key in keyring.keys if key.version == payload["key_version"]),
        None,
    )
    if key is None:
        raise SourceMatchError("candidate_mismatch")
    candidate = candidates[ordinal - 1]
    if candidate.provider != review.provider:
        raise SourceMatchError("candidate_mismatch")
    expected = _candidate_signature(encoded, candidate.source_ref, key.secret)
    if not hmac.compare_digest(supplied_signature, expected):
        raise SourceMatchError("candidate_mismatch")
    return candidate.source_ref


def _candidate_token(
    review: ImportMatchReview,
    candidate: ProviderPatientSnapshot,
    *,
    ordinal: int,
    expires_at: datetime,
    key,
) -> str:
    payload = {
        "v": 1,
        "clinic_id": review.clinic_id,
        "review_id": review.id,
        "provider": review.provider.value,
        "evidence": review.candidate_evidence_hash,
        "ordinal": ordinal,
        "expires_at": int(expires_at.timestamp()),
        "key_version": key.version,
    }
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    return f"{encoded}.{_candidate_signature(encoded, candidate.source_ref, key.secret)}"


def _candidate_signature(encoded: str, source_ref: str, secret: bytes) -> str:
    return hmac.new(
        secret,
        f"clinic-recall:match-candidate:v1:{encoded}:{source_ref}".encode(),
        hashlib.sha256,
    ).hexdigest()


def create_patient_source_link(
    session: Session,
    clinic_id: str,
    patient_id: str,
    *,
    provider: SourceSystem,
    source_ref: str,
    strategy: MatchStrategy,
    evidence_hash: str,
    actor: str,
    now: datetime,
    keyring: SubjectKeyring,
    import_batch_id: str | None = None,
) -> PatientSourceLink:
    """Create one active provider alias after rights and tenant guards.

    Raises ``SubjectFrozenError`` (via the rights guards) for erased subjects
    and ``SourceMatchError('link_conflict')`` when the reference or an active
    link for the patient/provider already exists.
    """
    now = _aware(now, "now")
    with clinic_scope(session, clinic_id):
        patient = assert_patient_writable(session, clinic_id, patient_id)
        assert_source_writable(session, clinic_id, source_ref, keyring)
        assert_source_writable(session, clinic_id, patient.source_ref, keyring)
        existing = session.execute(
            tenant_select(PatientSourceLink).where(
                PatientSourceLink.provider == provider,
                PatientSourceLink.source_ref == source_ref,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.patient_id == patient_id and existing.state == SourceLinkState.ACTIVE:
                return existing
            raise SourceMatchError("link_conflict")
        active = session.execute(
            tenant_select(PatientSourceLink).where(
                PatientSourceLink.patient_id == patient_id,
                PatientSourceLink.provider == provider,
                PatientSourceLink.state == SourceLinkState.ACTIVE,
            )
        ).scalar_one_or_none()
        if active is not None:
            raise SourceMatchError("link_conflict")
        link = PatientSourceLink(
            id=f"pslink-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            patient_id=patient_id,
            provider=provider,
            source_ref=source_ref,
            import_batch_id=import_batch_id,
            state=SourceLinkState.ACTIVE,
            strategy=strategy,
            strategy_version=MATCH_STRATEGY_VERSION,
            evidence_hash=evidence_hash,
            resolved_by=actor,
            resolved_at=now,
        )
        session.add(link)
        session.flush()
        return link


def run_source_matching(
    session: Session,
    clinic_id: str,
    batch_id: str,
    *,
    provider: SourceSystem,
    patient_source_refs: Sequence[str],
    candidates_by_ref: Mapping[str, Sequence[ProviderPatientSnapshot]] | None,
    keyring: SubjectKeyring,
    actor: str,
    now: datetime,
    auto_link: bool,
) -> MatchingRunResult:
    """One deterministic matching pass after a completed import.

    ``candidates_by_ref`` is materialized by the caller before database scope;
    ``None`` means the provider was unavailable and every review stays
    ``pending``. Exactly one exact candidate may auto-link only when
    ``auto_link`` is true (runtime default off); zero or multiple candidates
    enter review. A failed link never rolls back the completed import.
    """
    now = _aware(now, "now")
    reviewed = linked = unmatched = ambiguous = pending = 0
    with clinic_scope(session, clinic_id):
        for source_ref in dict.fromkeys(patient_source_refs):
            patient = session.execute(
                tenant_select(Patient).where(Patient.source_ref == source_ref)
            ).scalar_one_or_none()
            if patient is None:
                continue
            review = _review_for(session, clinic_id, batch_id, patient.id, provider, now)
            if review.state in _RESOLVED_STATES:
                continue
            reviewed += 1
            if candidates_by_ref is None:
                review.state = ImportMatchReviewState.PENDING
                review.reason = "provider_unavailable"
                pending += 1
                continue
            candidates = [
                candidate
                for candidate in candidates_by_ref.get(source_ref, ())
                if candidate.provider == provider and candidate.source_ref == source_ref
            ]
            review.candidate_count = len(candidates)
            review.candidate_evidence_hash = (
                candidate_evidence_hash(candidates) if candidates else None
            )
            if not candidates:
                review.state = ImportMatchReviewState.UNMATCHED
                review.reason = "no_exact_match"
                unmatched += 1
            elif len(candidates) > 1:
                review.state = ImportMatchReviewState.AMBIGUOUS
                review.reason = "multiple_exact_matches"
                ambiguous += 1
            elif not auto_link:
                review.state = ImportMatchReviewState.PENDING
                review.reason = "auto_link_disabled"
                pending += 1
            else:
                try:
                    link = create_patient_source_link(
                        session,
                        clinic_id,
                        patient.id,
                        provider=provider,
                        source_ref=candidates[0].source_ref,
                        strategy=MatchStrategy.EXACT_SOURCE_REF,
                        evidence_hash=review.candidate_evidence_hash
                        or candidate_evidence_hash(candidates),
                        actor=actor,
                        now=now,
                        keyring=keyring,
                        import_batch_id=batch_id,
                    )
                except SourceMatchError as exc:
                    review.state = ImportMatchReviewState.FAILED
                    review.reason = exc.reason
                    pending += 1
                else:
                    review.state = ImportMatchReviewState.LINKED
                    review.reason = None
                    review.resolved_by = actor
                    review.resolved_at = now
                    review.source_link_id = link.id
                    linked += 1
        if reviewed:
            audit_action(
                session,
                clinic_id,
                AuditAction.CSV_IMPORT_MATCH,
                batch_id,
                {
                    "batch_id": batch_id,
                    "reviewed": reviewed,
                    "linked": linked,
                    "unmatched": unmatched,
                    "ambiguous": ambiguous,
                    "pending": pending,
                    "occurred_at": now,
                },
                actor=actor,
            )
        session.flush()
    return MatchingRunResult(
        reviewed=reviewed,
        linked=linked,
        unmatched=unmatched,
        ambiguous=ambiguous,
        pending=pending,
    )


def list_match_reviews(
    session: Session, clinic_id: str, *, limit: int = 50
) -> list[ImportMatchReview]:
    """Most-recent-first unresolved-first review queue for one clinic."""
    with clinic_scope(session, clinic_id):
        rows = list(
            session.execute(
                tenant_select(ImportMatchReview)
                .order_by(ImportMatchReview.created_at.desc(), ImportMatchReview.id)
                .limit(max(1, min(limit, 200)))
            ).scalars()
        )
        return sorted(rows, key=lambda review: review.state in _RESOLVED_STATES)


def get_match_review(session: Session, clinic_id: str, review_id: str) -> ImportMatchReview | None:
    """Return one tenant-scoped review without leaking cross-tenant existence."""
    with clinic_scope(session, clinic_id):
        review = session.execute(
            tenant_select(ImportMatchReview).where(ImportMatchReview.id == review_id)
        ).scalar_one_or_none()
        if review is None or review.clinic_id != clinic_id:
            return None
        return review


def refresh_import_match(
    session: Session,
    clinic_id: str,
    review_id: str,
    *,
    candidates: Sequence[ProviderPatientSnapshot],
    now: datetime,
) -> tuple[ImportMatchReview, tuple[ProviderPatientSnapshot, ...]]:
    """Bind a freshly materialized exact candidate set to an open review.

    Provider I/O happens before this function. The database stores only the
    count and canonical evidence hash; candidate values remain request-scoped.
    """
    _aware(now, "now")
    with clinic_scope(session, clinic_id):
        statement = tenant_select(ImportMatchReview).where(ImportMatchReview.id == review_id)
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update()
        review = session.execute(statement).scalar_one_or_none()
        if review is None or review.clinic_id != clinic_id:
            raise SourceMatchError("review_not_found")
        if review.state in _RESOLVED_STATES:
            raise SourceMatchError("review_already_resolved")
        patient = assert_patient_writable(session, clinic_id, review.patient_id)
        exact = tuple(
            candidate
            for candidate in candidates
            if candidate.provider == review.provider and candidate.source_ref == patient.source_ref
        )
        review.candidate_count = len(exact)
        review.candidate_evidence_hash = candidate_evidence_hash(exact) if exact else None
        review.resolved_by = None
        review.resolved_at = None
        review.source_link_id = None
        if not exact:
            review.state = ImportMatchReviewState.UNMATCHED
            review.reason = "no_exact_match"
        elif len(exact) > 1:
            review.state = ImportMatchReviewState.AMBIGUOUS
            review.reason = "multiple_exact_matches"
        else:
            review.state = ImportMatchReviewState.PENDING
            review.reason = "operator_review_required"
        session.flush()
        return review, exact


def resolve_import_match(
    session: Session,
    clinic_id: str,
    review_id: str,
    *,
    action: str,
    keyring: SubjectKeyring,
    actor: str,
    now: datetime,
    candidate_token: str | None = None,
    candidates: Sequence[ProviderPatientSnapshot] | None = None,
) -> ImportMatchReview:
    """Explicit, idempotent operator resolution of one review.

    ``link`` requires a short-lived token plus a freshly materialized,
    evidence-identical candidate set. The browser never supplies a provider
    reference. ``dismiss`` closes without linking. Repeating the same
    resolution returns the review unchanged; conflicting resolutions fail.
    """
    now = _aware(now, "now")
    if action not in {"link", "dismiss"}:
        raise SourceMatchError("invalid_action")
    with clinic_scope(session, clinic_id):
        review = session.execute(
            tenant_select(ImportMatchReview).where(ImportMatchReview.id == review_id)
        ).scalar_one_or_none()
        if review is None or review.clinic_id != clinic_id:
            raise SourceMatchError("review_not_found")
        if review.state in _RESOLVED_STATES:
            already_linked = review.state == ImportMatchReviewState.LINKED
            if (action == "link") == already_linked:
                return review  # idempotent replay
            raise SourceMatchError("review_already_resolved")
        if action == "dismiss":
            review.state = ImportMatchReviewState.DISMISSED
            review.resolved_by = actor
            review.resolved_at = now
            session.flush()
            return review
        if not candidate_token or candidates is None:
            raise SourceMatchError("candidate_mismatch")
        candidate_source_ref = resolve_candidate_token(
            review,
            candidate_token,
            candidates,
            keyring=keyring,
            now=now,
        )
        link = create_patient_source_link(
            session,
            clinic_id,
            review.patient_id,
            provider=review.provider,
            source_ref=candidate_source_ref,
            strategy=MatchStrategy.OPERATOR_RESOLVED,
            evidence_hash=review.candidate_evidence_hash,
            actor=actor,
            now=now,
            keyring=keyring,
            import_batch_id=review.import_batch_id,
        )
        review.state = ImportMatchReviewState.LINKED
        review.resolved_by = actor
        review.resolved_at = now
        review.source_link_id = link.id
        session.flush()
        return review


def _review_for(
    session: Session,
    clinic_id: str,
    batch_id: str,
    patient_id: str,
    provider: SourceSystem,
    now: datetime,
) -> ImportMatchReview:
    review = session.execute(
        tenant_select(ImportMatchReview).where(
            ImportMatchReview.import_batch_id == batch_id,
            ImportMatchReview.patient_id == patient_id,
            ImportMatchReview.provider == provider,
        )
    ).scalar_one_or_none()
    if review is not None:
        return review
    review = ImportMatchReview(
        id=f"imr-{uuid.uuid4().hex}",
        clinic_id=clinic_id,
        import_batch_id=batch_id,
        patient_id=patient_id,
        provider=provider,
        strategy=MatchStrategy.EXACT_SOURCE_REF,
        strategy_version=MATCH_STRATEGY_VERSION,
        state=ImportMatchReviewState.PENDING,
    )
    session.add(review)
    session.flush()
    return review


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)
