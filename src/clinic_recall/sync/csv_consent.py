"""Server-owned CSV import consent policy and presence-aware evaluation (PR-08).

The clinic's structured attestation never becomes authority by itself: positive
imported consent exists only when a controller-approved policy defines the
acceptable evidence age, the attestation is current, and the channel was both
attested and explicitly ``true`` in the file. Missing or ambiguous authority
stays unknown; nothing here can weaken an existing opt-out.

No default evidence age is invented: a policy without ``max_evidence_age``
grants no positive consent at all (the PR-08 conditional posture).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from ..enums import SourceSystem
from .base import NormalizedPatient

_CHANNELS = ("sms", "email", "call")


@dataclass(frozen=True)
class CsvImportPolicy:
    """Versioned, trusted import policy supplied by configuration, not requests."""

    version: str
    statement_hash: str
    attestation_versions: tuple[str, ...]
    channels: tuple[str, ...]
    max_evidence_age: timedelta | None
    preview_ttl: timedelta
    allowed_source_systems: tuple[SourceSystem, ...]

    def __post_init__(self) -> None:
        if not self.version or len(self.version) > 128:
            raise ValueError("policy version must contain 1 to 128 characters")
        if not re.fullmatch(r"[0-9a-f]{64}", self.statement_hash):
            raise ValueError("statement_hash must be a SHA-256 digest")
        if not self.attestation_versions or any(
            not value or len(value) > 64 for value in self.attestation_versions
        ):
            raise ValueError("attestation_versions must contain bounded versions")
        if len(self.attestation_versions) != len(set(self.attestation_versions)):
            raise ValueError("attestation_versions must be unique")
        if self.preview_ttl <= timedelta(0):
            raise ValueError("preview_ttl must be positive")
        if self.max_evidence_age is not None and self.max_evidence_age <= timedelta(0):
            raise ValueError("max_evidence_age must be positive when approved")
        if any(channel not in _CHANNELS for channel in self.channels):
            raise ValueError("policy channels must be a subset of sms/email/call")
        if len(self.channels) != len(set(self.channels)):
            raise ValueError("policy channels must be unique")
        if not self.allowed_source_systems:
            raise ValueError("allowed_source_systems must not be empty")


@dataclass(frozen=True)
class ConsentEvaluation:
    """Aggregate outcome of applying the policy to materialized patients."""

    patients: tuple[NormalizedPatient, ...]
    granted_count: int
    unknown_count: int
    opt_out_count: int
    authority_granted: bool


def grantable_channels(
    policy: CsvImportPolicy,
    *,
    attestation_version: str,
    attested_channels: tuple[str, ...],
    export_at: datetime,
    now: datetime,
) -> frozenset[str]:
    """Channels whose explicit ``true`` may become positive consent.

    Empty when the attestation is stale/unknown or when no controller-approved
    evidence-age policy exists (``max_evidence_age is None``).
    """
    if policy.max_evidence_age is None:
        return frozenset()
    if attestation_version not in policy.attestation_versions:
        return frozenset()
    if now - export_at > policy.max_evidence_age:
        return frozenset()
    return frozenset(attested_channels) & frozenset(policy.channels)


def apply_consent_policy(
    patients: tuple[NormalizedPatient, ...],
    *,
    grantable: frozenset[str],
) -> ConsentEvaluation:
    """Strip ungrantable positive consent; count grants, unknowns, and opt-outs.

    Opt-out flags pass through untouched: a CSV may only strengthen
    suppression (the monotonic merge in the upsert enforces that side).
    """
    adjusted: list[NormalizedPatient] = []
    granted = 0
    unknown = 0
    opt_outs = 0
    for patient in patients:
        positive_opt_out = {
            channel: True for channel, value in (patient.opt_out_flags or {}).items() if value
        }
        consent = patient.consent_flags
        if consent is not None:
            kept: dict[str, bool] = {}
            for channel, value in consent.items():
                if value and channel in grantable and channel not in positive_opt_out:
                    kept[channel] = True
                    granted += 1
                elif value:
                    # Unattested, stale, or contradicted positive evidence is unknown.
                    unknown += 1
            consent = kept or None
        opt_outs += len(positive_opt_out)
        adjusted.append(
            replace(
                patient,
                consent_flags=consent,
                opt_out_flags=positive_opt_out or None,
            )
        )
    return ConsentEvaluation(
        patients=tuple(adjusted),
        granted_count=granted,
        unknown_count=unknown,
        opt_out_count=opt_outs,
        authority_granted=bool(grantable),
    )
