"""Closed PR-14 pilot observability signal registry.

Every required pilot operational invariant binds to exactly one approved
aggregate source event and one scheduled-query alert contract. The four
handoff keys reuse the PR-12 producers and alerts unchanged. Contract tests
in ``tests/test_clinic_recall_pr14_observability_assets.py`` enforce this
registry against telemetry, Terraform, the Workbook, the runbook, and the
rehearsal probe.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

CALLBACK_LAG_ALERT_BUCKETS = ("15m_to_1h", "1h_to_4h", "over_4h")
CONFIGURATION_BLOCK_REASONS = (
    "configuration_identity_missing",
    "configuration_evidence_missing",
    "configuration_stale",
)
PILOT_VIOLATION_REASON_CODES = (
    "cohort_limit_exceeded",
    "cumulative_limit_invalid",
    "participant_wave_invalid",
    "programme_release_conflict",
    "programme_state_invalid",
)


@dataclass(frozen=True)
class PilotSignalContract:
    """One pilot invariant bound to one source event and one alert."""

    key: str
    event: str
    owner: str
    source_state: str
    alert_key: str
    severity: int
    dimensions: frozenset[str]
    threshold: int
    comparator: str
    window: str
    frequency: str
    source_freshness: str
    workbook_view: str
    runbook_response: str
    fixture_key: str
    reused: bool = False


def _contract(
    key: str,
    *,
    event: str,
    owner: str,
    source_state: str,
    alert_key: str,
    severity: int,
    dimensions: frozenset[str],
    source_freshness: str = "event occurrence within the 15-minute alert window",
    workbook_view: str = "pilotops-signal-posture",
    runbook_response: str = "runbook section 7.1 alert fire/resolve and triage",
    reused: bool = False,
) -> tuple[str, PilotSignalContract]:
    return key, PilotSignalContract(
        key=key,
        event=event,
        owner=owner,
        source_state=source_state,
        alert_key=alert_key,
        severity=severity,
        dimensions=dimensions,
        threshold=0,
        comparator="GreaterThan",
        window="PT15M",
        frequency="PT5M",
        source_freshness=source_freshness,
        workbook_view=workbook_view,
        runbook_response=runbook_response,
        fixture_key=key,
        reused=reused,
    )


PILOT_OBSERVABILITY_REGISTRY: Mapping[str, PilotSignalContract] = MappingProxyType(
    dict(
        (
            _contract(
                "ambiguous_external_effect",
                event="worker.cycle.summary",
                owner="src.clinic_recall.durable worker return boundaries",
                source_state=(
                    "completed worker counters reconcile_required, unresolved, or exhausted"
                ),
                alert_key="effect_ambiguity_backlog",
                severity=1,
                dimensions=frozenset({"worker", "outcome", "count"}),
                runbook_response="runbook section 7.1 reconcile without replay",
            ),
            _contract(
                "ambiguous_callback",
                event="worker.cycle.summary",
                owner="src.clinic_recall.durable.callbacks.reconcile_once",
                source_state="completed callback reconciler conflicts counter",
                alert_key="callback_ambiguity",
                severity=1,
                dimensions=frozenset({"worker", "outcome", "count"}),
                runbook_response="runbook section 7.1 callback reconciliation",
            ),
            _contract(
                "dead_letter_effect",
                event="worker.cycle.summary",
                owner=(
                    "src.clinic_recall.durable.worker and cliniko_booking_worker return boundaries"
                ),
                source_state="completed SMS or Cliniko worker dead_lettered counter",
                alert_key="effect_dead_letter",
                severity=1,
                dimensions=frozenset({"worker", "outcome", "count"}),
                runbook_response="runbook section 7.1 dead-letter handoff triage",
            ),
            _contract(
                "callback_processing_lag",
                event="callbacks.queue.snapshot",
                owner="src.clinic_recall.operational_snapshot",
                source_state=(
                    "provider_callback_receipt state pending or processing aged past "
                    "three 5-minute reconciliation leases"
                ),
                alert_key="callback_processing_lag",
                severity=1,
                dimensions=frozenset({"state", "oldest_age_bucket", "count"}),
                source_freshness="latest queue snapshot must be no older than 15 minutes",
                workbook_view="pilotops-queue-ageing",
                runbook_response="runbook section 7.1 callback lag triage",
            ),
            _contract(
                "cliniko_readback_conflict",
                event="worker.cycle.summary",
                owner=(
                    "src.clinic_recall.durable.cliniko_booking_worker and "
                    "cliniko_booking_reconciler"
                ),
                source_state="completed Cliniko worker conflicts counter",
                alert_key="cliniko_readback_conflict",
                severity=0,
                dimensions=frozenset({"worker", "outcome", "count"}),
                workbook_view="pilotops-invariant-breaches",
                runbook_response="runbook section 7.4 pause before Cliniko reconciliation",
            ),
            _contract(
                "booking_confirmation_grounding_failure",
                event="booking.confirmation.blocked",
                owner="src.clinic_recall.operational_snapshot",
                source_state=(
                    "external_effect canceled with last_error_code == "
                    "'booking_confirmation_authority_invalid'"
                ),
                alert_key="booking_confirmation_grounding",
                severity=0,
                dimensions=frozenset({"reason_code", "count"}),
                workbook_view="pilotops-invariant-breaches",
                runbook_response="runbook section 7.4 keep confirmation blocked",
            ),
            _contract(
                "recording_consent_provider_mismatch",
                event="recording.consent.mismatch",
                owner="src.clinic_recall.operational_snapshot",
                source_state=(
                    "recording effect last_error_code == "
                    "'provider_outcome_conflict' or call_record."
                    "recording_status == 'reconcile_required'"
                ),
                alert_key="recording_consent_mismatch",
                severity=0,
                dimensions=frozenset({"reason_code", "count"}),
                workbook_view="pilotops-invariant-breaches",
                runbook_response="runbook section 7.4 stop recording before rollback",
            ),
            _contract(
                "rights_deletion_overdue",
                event="rights.deletion.overdue",
                owner="src.clinic_recall.operational_snapshot",
                source_state=(
                    "rights request/target due_at < now while non-terminal, "
                    "or residual_due_at < now"
                ),
                alert_key="rights_deletion_overdue",
                severity=0,
                dimensions=frozenset({"kind", "count"}),
                workbook_view="pilotops-invariant-breaches",
                runbook_response="runbook section 7.4 preserve pending deletion state",
            ),
            _contract(
                "handoff_sla_breach",
                event="handoff.sla.breach",
                owner="src.clinic_recall.handoff_ageing",
                source_state=("handoff_receipt unacknowledged past immutable due_at"),
                alert_key="handoff_sla_breach",
                severity=0,
                dimensions=frozenset({"severity", "count"}),
                workbook_view="pilotops-invariant-breaches",
                runbook_response="PR-12 handoff SLA response; runbook section 7.4",
                reused=True,
            ),
            _contract(
                "handoff_destination_failure",
                event="handoff.notification.outcome",
                owner="src.clinic_recall.durable.handoff_worker",
                source_state=("handoff notification outcome == 'destination_unavailable'"),
                alert_key="handoff_destination_unavailable",
                severity=0,
                dimensions=frozenset({"outcome", "count"}),
                runbook_response="PR-12 destination response; keep outreach paused",
                reused=True,
            ),
            _contract(
                "handoff_alternate_page",
                event="handoff.alternate.requested",
                owner="src.clinic_recall.handoffs",
                source_state="deterministic alternate on-call page requested",
                alert_key="handoff_alternate_page_requested",
                severity=1,
                dimensions=frozenset({"severity", "reason_code"}),
                runbook_response="PR-12 approved alternate paging response",
                reused=True,
            ),
            _contract(
                "handoff_pause",
                event="handoff.programme.pause",
                owner="src.clinic_recall.handoffs",
                source_state="pilot programme paused for handoff safety",
                alert_key="handoff_programme_pause",
                severity=0,
                dimensions=frozenset({"reason_code", "outcome"}),
                runbook_response="runbook section 7.3 database pause verification",
                reused=True,
            ),
            _contract(
                "pilot_cohort_invariant_violation",
                event="pilot.invariant.violation",
                owner="src.clinic_recall.pilot_controls",
                source_state=(
                    "PilotControlError raised for a cohort, wave, release, or "
                    "programme-state invariant"
                ),
                alert_key="pilot_cohort_invariant_violation",
                severity=0,
                dimensions=frozenset({"reason_code", "count"}),
                workbook_view="pilotops-invariant-breaches",
                runbook_response="runbook sections 7.3-7.4 control-first pause",
            ),
            _contract(
                "app_configuration_stale_or_missing",
                event="pilot.configuration.status",
                owner="src.clinic_recall.operational_snapshot",
                source_state=(
                    "OperationalSwitchSnapshot configuration block reason (PR-13 TTL) != 'fresh'"
                ),
                alert_key="pilot_configuration_stale",
                severity=1,
                dimensions=frozenset({"reason", "count"}),
                source_freshness="PR-13 configured TTL, capped at one hour; snapshot every 5 minutes",
                workbook_view="pilotops-invariant-breaches",
                runbook_response="runbook section 7.2 configuration staleness diagnosis",
            ),
            _contract(
                "release_environment_mismatch",
                event="pilot.release.mismatch",
                owner="src.clinic_recall.operational_snapshot",
                source_state=(
                    "pilot_programme environment/release identity disagrees "
                    "with the runtime configuration snapshot; dark, active, "
                    "and paused programmes remain visible until closed"
                ),
                alert_key="pilot_release_mismatch",
                severity=1,
                dimensions=frozenset({"count"}),
                source_freshness="latest identity snapshot must be no older than 15 minutes",
                workbook_view="pilotops-invariant-breaches",
                runbook_response="runbook section 7.2 reconcile release identity before switches",
            ),
        )
    )
)
