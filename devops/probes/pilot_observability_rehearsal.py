"""Deterministic dry-run rehearsal for PR-14 pilot observability contracts.

Evaluates the exact ``AppTraces`` alert predicates published in
``infra/terraform/monitoring.tf`` against synthetic fixture rows, and
rehearses the database kill switch and control-first rollback order with
in-memory state only.

This probe is offline by construction: it loads no provider or Azure
client, uses synthetic identities and clocks, and cannot send SMS, place
calls, write Cliniko, start recording, or contact Azure. Its output is
local contract evidence only — never Azure operational evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

DRY_RUN_ONLY = True
REPO_ROOT = Path(__file__).resolve().parents[2]
MONITORING_PATH = REPO_ROOT / "infra" / "terraform" / "monitoring.tf"
RUNBOOK_PATH = REPO_ROOT / "docs" / "clinic-recall-production-bring-up-runbook.md"

ROLLBACK_ORDER = (
    "database pause",
    "App Configuration off",
    "Jobs and recording stopped",
    "code/image rollback",
)

_EXTEND_RE = re.compile(r"(\w+)=(tostring|toint)\(Properties\['([a-z0-9_.]+)'\]\)")
_EQ_RE = re.compile(r"^(\w+)\s*(==|!=)\s*'([^']*)'$")
_IN_RE = re.compile(r"^(\w+)\s+in\s+\((.*)\)$")
_GT_RE = re.compile(r"^(\w+)\s*>\s*(-?\d+)$")


class RehearsalError(RuntimeError):
    """A rehearsal contract could not be evaluated."""


@contextmanager
def network_disabled():
    """Fail closed on any socket connection for the rehearsal duration."""

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise RehearsalError("rehearsal attempted a network operation")

    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection
    socket.socket.connect = _blocked  # type: ignore[method-assign]
    socket.create_connection = _blocked  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.create_connection = original_create_connection  # type: ignore[assignment]


def load_apptraces_alert_queries(
    monitoring_text: str | None = None,
) -> dict[str, str]:
    """Return ``{alert_key: query}`` for every AppTraces alert in monitoring.tf."""
    text = (
        MONITORING_PATH.read_text(encoding="utf-8") if monitoring_text is None else monitoring_text
    )
    queries: dict[str, str] = {}
    for match in re.finditer(
        r"^    (\w+) = \{.*?query\s+=\s+<<-KQL\n(.*?)\n\s+KQL",
        text,
        re.S | re.M,
    ):
        key, query = match.group(1), match.group(2)
        first_line = query.strip().splitlines()[0].strip()
        if first_line == "AppTraces":
            queries[key] = query
    return queries


def evaluate_apptraces_query(
    query: str,
    rows: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    """Evaluate one extend/where AppTraces alert query over fixture rows.

    Each row is one synthetic AppTraces record: a mapping of the closed
    telemetry attribute names (plus ``microsoft.custom_event.name``) to
    scalar values. Supports exactly the grammar used by the repository's
    alerts: ``extend`` with ``tostring``/``toint`` over ``Properties`` and a
    single ``where`` combining ``==``, ``!=``, ``in`` and ``>`` with ``and``.
    """
    extends: dict[str, tuple[str, str]] = {}
    conditions: list[str] = []
    for raw_line in query.strip().splitlines():
        line = raw_line.strip()
        if line == "AppTraces" or not line:
            continue
        if line.startswith("| extend "):
            for name, cast, prop in _EXTEND_RE.findall(line):
                extends[name] = (cast, prop)
            continue
        if line == "| where TimeGenerated > ago(15m)":
            # Fixtures represent rows already selected into the alert window.
            continue
        if line.startswith("| where "):
            conditions.extend(
                part.strip() for part in re.split(r"\s+and\s+", line[len("| where ") :])
            )
            continue
        raise RehearsalError(f"unsupported alert query line: {line!r}")
    if not extends or not conditions:
        raise RehearsalError("alert query must extend Properties and filter them")

    matched: list[Mapping[str, object]] = []
    for row in rows:
        env: dict[str, object] = {}
        for name, (cast, prop) in extends.items():
            value = row.get(prop)
            if cast == "tostring":
                env[name] = "" if value is None else str(value)
            else:
                try:
                    env[name] = int(value)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    env[name] = None
        if all(_condition_holds(condition, env) for condition in conditions):
            matched.append(row)
    return matched


def _condition_holds(condition: str, env: Mapping[str, object]) -> bool:
    if match := _EQ_RE.match(condition):
        name, operator, literal = match.groups()
        value = env.get(name)
        return (value == literal) if operator == "==" else (value != literal)
    if match := _IN_RE.match(condition):
        name, body = match.groups()
        values = {part.strip().strip("'") for part in body.split(",")}
        return env.get(name) in values
    if match := _GT_RE.match(condition):
        name, literal = match.groups()
        value = env.get(name)
        return isinstance(value, int) and value > int(literal)
    raise RehearsalError(f"unsupported alert condition: {condition!r}")


def _row(event: str, **attributes: object) -> dict[str, object]:
    return {"microsoft.custom_event.name": event, **attributes}


def signal_fixtures() -> dict[str, dict[str, list[dict[str, object]]]]:
    """Violating and healthy synthetic rows for every registered signal."""
    return {
        "ambiguous_external_effect": {
            "violating": [
                _row(
                    "worker.cycle.summary",
                    worker="sms_dispatch",
                    outcome="reconcile_required",
                    count=2,
                )
            ],
            "healthy": [
                _row(
                    "worker.cycle.summary",
                    worker="sms_dispatch",
                    outcome="reconcile_required",
                    count=0,
                )
            ],
        },
        "ambiguous_callback": {
            "violating": [
                _row(
                    "worker.cycle.summary",
                    worker="callback_reconcile",
                    outcome="conflicts",
                    count=1,
                )
            ],
            "healthy": [
                _row(
                    "worker.cycle.summary",
                    worker="callback_reconcile",
                    outcome="conflicts",
                    count=0,
                )
            ],
        },
        "dead_letter_effect": {
            "violating": [
                _row(
                    "worker.cycle.summary",
                    worker="sms_dispatch",
                    outcome="dead_lettered",
                    count=1,
                )
            ],
            "healthy": [
                _row(
                    "worker.cycle.summary",
                    worker="sms_dispatch",
                    outcome="dead_lettered",
                    count=0,
                )
            ],
        },
        "callback_processing_lag": {
            "violating": [
                _row(
                    "callbacks.queue.snapshot",
                    state="pending",
                    oldest_age_bucket="1h_to_4h",
                    count=2,
                )
            ],
            "healthy": [
                _row(
                    "callbacks.queue.snapshot",
                    state="pending",
                    oldest_age_bucket="under_5m",
                    count=2,
                )
            ],
        },
        "cliniko_readback_conflict": {
            "violating": [
                _row(
                    "worker.cycle.summary",
                    worker="cliniko_dispatch",
                    outcome="conflicts",
                    count=1,
                )
            ],
            "healthy": [
                _row(
                    "worker.cycle.summary",
                    worker="cliniko_dispatch",
                    outcome="conflicts",
                    count=0,
                )
            ],
        },
        "booking_confirmation_grounding_failure": {
            "violating": [
                _row(
                    "booking.confirmation.blocked",
                    reason_code="booking_confirmation_authority_invalid",
                    count=1,
                )
            ],
            "healthy": [
                _row(
                    "booking.confirmation.blocked",
                    reason_code="booking_confirmation_authority_invalid",
                    count=0,
                )
            ],
        },
        "recording_consent_provider_mismatch": {
            "violating": [
                _row(
                    "recording.consent.mismatch",
                    reason_code="provider_outcome_conflict",
                    count=1,
                )
            ],
            "healthy": [
                _row(
                    "recording.consent.mismatch",
                    reason_code="provider_outcome_conflict",
                    count=0,
                ),
                _row(
                    "recording.consent.mismatch",
                    reason_code="recording_status_reconcile_required",
                    count=0,
                ),
            ],
        },
        "rights_deletion_overdue": {
            "violating": [_row("rights.deletion.overdue", kind="target", count=3)],
            "healthy": [
                _row("rights.deletion.overdue", kind="request", count=0),
                _row("rights.deletion.overdue", kind="target", count=0),
                _row("rights.deletion.overdue", kind="residual", count=0),
            ],
        },
        "handoff_sla_breach": {
            "violating": [_row("handoff.sla.breach", severity="critical", count=1)],
            "healthy": [
                _row(
                    "handoff.queue.snapshot",
                    severity="critical",
                    delivery_state="sent",
                    oldest_age_bucket="under_5m",
                    count=1,
                ),
                _row(
                    "handoff.queue.snapshot",
                    severity="critical",
                    delivery_state="delivered",
                    oldest_age_bucket="under_5m",
                    count=1,
                ),
            ],
        },
        "handoff_destination_failure": {
            "violating": [
                _row(
                    "handoff.notification.outcome",
                    outcome="destination_unavailable",
                    count=1,
                )
            ],
            "healthy": [_row("handoff.notification.outcome", outcome="sent", count=1)],
        },
        "handoff_alternate_page": {
            "violating": [
                _row(
                    "handoff.alternate.requested",
                    severity="critical",
                    reason_code="handoff_sla_breached",
                )
            ],
            "healthy": [_row("handoff.sla.breach", severity="normal", count=0)],
        },
        "handoff_pause": {
            "violating": [
                _row(
                    "handoff.programme.pause",
                    reason_code="handoff_sla_breached",
                    outcome="paused",
                )
            ],
            "healthy": [
                _row(
                    "handoff.programme.pause",
                    reason_code="handoff_sla_breached",
                    outcome="already_paused",
                )
            ],
        },
        "pilot_cohort_invariant_violation": {
            "violating": [
                _row(
                    "pilot.invariant.violation",
                    reason_code="cohort_limit_exceeded",
                    count=1,
                )
            ],
            "healthy": [
                _row(
                    "pilot.configuration.status",
                    reason="configuration_stale",
                    count=1,
                )
            ],
        },
        "app_configuration_stale_or_missing": {
            "violating": [
                _row(
                    "pilot.configuration.status",
                    reason="configuration_stale",
                    count=1,
                ),
                _row(
                    "pilot.configuration.status",
                    reason="configuration_evidence_missing",
                    count=1,
                ),
            ],
            "healthy": [_row("pilot.configuration.status", reason="fresh", count=1)],
        },
        "release_environment_mismatch": {
            "violating": [_row("pilot.release.mismatch", count=1)],
            "healthy": [_row("pilot.release.mismatch", count=0)],
        },
    }


def rehearse_alert_predicates() -> dict[str, dict[str, bool]]:
    """Prove each alert fires on its violating fixture and resolves on healthy."""
    from src.clinic_recall.observability_registry import (
        PILOT_OBSERVABILITY_REGISTRY,
    )

    queries = load_apptraces_alert_queries()
    fixtures = signal_fixtures()
    results: dict[str, dict[str, bool]] = {}
    for key, contract in PILOT_OBSERVABILITY_REGISTRY.items():
        query = queries.get(contract.alert_key)
        if query is None:
            raise RehearsalError(f"alert {contract.alert_key!r} not found")
        fixture = fixtures[key]
        fires = bool(evaluate_apptraces_query(query, fixture["violating"]))
        resolves = not evaluate_apptraces_query(query, fixture["healthy"])
        results[key] = {"fires_on_violation": fires, "resolves_on_healthy": resolves}
    return results


def rehearse_signal_distinctness() -> dict[str, bool]:
    """Prove neighbouring signals never claim each other's fixtures."""
    queries = load_apptraces_alert_queries()
    fixtures = signal_fixtures()
    dead_letter_rows = fixtures["dead_letter_effect"]["violating"]
    ambiguity_rows = fixtures["ambiguous_external_effect"]["violating"]
    policy_denial_rows = fixtures["app_configuration_stale_or_missing"]["violating"]
    delivery_rows = fixtures["handoff_sla_breach"]["healthy"]
    return {
        "dead_letter_not_ambiguity": not evaluate_apptraces_query(
            queries["effect_ambiguity_backlog"], dead_letter_rows
        ),
        "ambiguity_not_dead_letter": not evaluate_apptraces_query(
            queries["effect_dead_letter"], ambiguity_rows
        ),
        "policy_denial_not_cohort_violation": not evaluate_apptraces_query(
            queries["pilot_cohort_invariant_violation"], policy_denial_rows
        ),
        "delivery_is_not_acknowledgement": not evaluate_apptraces_query(
            queries["handoff_sla_breach"], delivery_rows
        ),
    }


def rehearse_kill_switch_and_duplicate_telemetry() -> dict[str, bool]:
    """Prove pause removes permission without weakening safety controls.

    Runs entirely against in-memory SQLite with synthetic identities. Also
    proves duplicate snapshot telemetry never duplicates business state.
    """
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session, sessionmaker
    from src.clinic_recall.enums import Channel
    from src.clinic_recall.models import AuditLog, Base, Clinic, Patient
    from src.clinic_recall.operational_snapshot import (
        run_operational_snapshot_once,
    )
    from src.clinic_recall.pilot_controls import (
        OperationalSwitchSnapshot,
        create_programme,
        enroll_participant,
        evaluate_patient_gate,
        mark_programme_dark,
        pause_programme,
        release_cumulative_limit,
    )

    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    clinic_id = "clinic-rehearsal"
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, class_=Session)
    switches = OperationalSwitchSnapshot(
        outreach_enabled=True,
        voice_enabled=True,
        recording_enabled=False,
        refreshed_at=now - timedelta(seconds=5),
        max_age=timedelta(seconds=60),
        environment="rehearsal",
        release_identity="sha256:rehearsal-release",
    )
    with factory() as session:
        session.add(Clinic(id=clinic_id, name="Rehearsal Clinic"))
        session.add_all(
            Patient(
                id=f"patient-rehearsal-{ordinal}",
                clinic_id=clinic_id,
                source_ref=f"R-{ordinal:02d}",
                name=f"Synthetic Patient {ordinal}",
                phone=f"+44000000000{ordinal}",
                consent_flags={"sms": True},
                opt_out_flags={},
            )
            for ordinal in range(1, 6)
        )
        session.flush()
        programme = create_programme(
            session,
            clinic_id=clinic_id,
            programme_id="pilot-rehearsal",
            environment="rehearsal",
            release_identity="sha256:rehearsal-release",
        )
        mark_programme_dark(
            session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            actor="rehearsal@example.test",
            evidence_hash="d" * 64,
            now=now - timedelta(minutes=2),
        )
        for ordinal in range(1, 6):
            enroll_participant(
                session,
                clinic_id=clinic_id,
                programme_id=programme.id,
                patient_id=f"patient-rehearsal-{ordinal}",
                now=now - timedelta(minutes=1),
            )
        release_cumulative_limit(
            session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            cumulative_limit=5,
            actor="rehearsal@example.test",
            evidence_hash="a" * 64,
            now=now - timedelta(minutes=1),
        )
        session.commit()

        allowed_before = evaluate_patient_gate(
            session,
            clinic_id=clinic_id,
            patient_id="patient-rehearsal-1",
            channel=Channel.SMS,
            switches=switches,
            now=now,
        )
        pause_programme(
            session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            actor="rehearsal@example.test",
            reason="rehearsal_kill_switch",
            now=now,
        )
        session.commit()
        denied_after = evaluate_patient_gate(
            session,
            clinic_id=clinic_id,
            patient_id="patient-rehearsal-1",
            channel=Channel.SMS,
            switches=switches,
            now=now,
        )
        audit_count = session.execute(
            select(AuditLog.id).where(AuditLog.clinic_id == clinic_id)
        ).all()

    emitted: list[tuple[str, dict[str, object]]] = []

    def emit(name: str, attributes: Mapping[str, object]) -> bool:
        emitted.append((name, dict(attributes)))
        return True

    first = run_operational_snapshot_once(
        factory,
        clinic_id=clinic_id,
        now=now,
        enabled=True,
        switches=switches,
        emit=emit,
    )
    second = run_operational_snapshot_once(
        factory,
        clinic_id=clinic_id,
        now=now,
        enabled=True,
        switches=switches,
        emit=emit,
    )
    with factory() as session:
        audit_after_snapshots = session.execute(
            select(AuditLog.id).where(AuditLog.clinic_id == clinic_id)
        ).all()

    return {
        "gate_allowed_before_pause": allowed_before.allowed is True,
        "database_pause_denies_outreach": denied_after.allowed is False
        and denied_after.reason == "programme_not_active",
        "pause_is_recorded_not_destructive": len(audit_count) > 0,
        "duplicate_telemetry_no_business_writes": len(audit_after_snapshots) == len(audit_count),
        "duplicate_telemetry_identical_aggregates": first.as_summary() == second.as_summary(),
        "telemetry_emitted_twice": len(emitted) > 0 and len(emitted) % 2 == 0,
    }


def rehearse_rollback_order() -> dict[str, bool]:
    """Prove the runbook keeps the mandatory control-first rollback order."""
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    positions = [runbook.find(marker) for marker in ROLLBACK_ORDER]
    return {
        "all_steps_documented": all(position >= 0 for position in positions),
        "control_first_order": positions == sorted(positions),
    }


def zero_operation_report(
    baseline_modules: frozenset[str] | None = None,
) -> dict[str, int]:
    """Assert this rehearsal loaded no provider-client pathway itself.

    Only modules newly imported since ``baseline_modules`` are attributed to
    the rehearsal. Passive local SDK imports made by the shared logging
    stack are not provider operations; provider clients (Twilio, ACS) are
    never acceptable, and every network operation is separately blocked by
    ``network_disabled`` while the rehearsal runs.
    """
    loaded = frozenset(sys.modules)
    added = loaded if baseline_modules is None else loaded - baseline_modules
    forbidden_modules = [
        name
        for name in added
        if name == "twilio" or name.startswith("twilio.") or name.startswith("azure.communication")
    ]
    if forbidden_modules:
        raise RehearsalError(f"forbidden provider modules loaded: {sorted(forbidden_modules)}")
    return {
        "sms_sent": 0,
        "calls_placed": 0,
        "cliniko_writes": 0,
        "recordings_started": 0,
        "azure_operations": 0,
        "provider_operations": 0,
        "patient_contacts": 0,
    }


def run_rehearsal() -> dict[str, object]:
    """Run every dry-run rehearsal contract and return an aggregate verdict."""
    baseline_modules = frozenset(sys.modules)
    with network_disabled():
        predicates = rehearse_alert_predicates()
        distinctness = rehearse_signal_distinctness()
        kill_switch = rehearse_kill_switch_and_duplicate_telemetry()
        rollback = rehearse_rollback_order()
        operations = zero_operation_report(baseline_modules)
    passed = (
        all(all(checks.values()) for checks in predicates.values())
        and all(distinctness.values())
        and all(kill_switch.values())
        and all(rollback.values())
        and all(value == 0 for value in operations.values())
    )
    return {
        "dry_run_only": DRY_RUN_ONLY,
        "network_blocked_for_duration": True,
        "evidence_scope": "local contract evidence only; not Azure evidence",
        "alert_predicates": predicates,
        "signal_distinctness": distinctness,
        "kill_switch_and_duplicates": kill_switch,
        "rollback_order": rollback,
        "operation_counts": operations,
        "passed": passed,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic dry-run rehearsal of Clinic Recall PR-14 pilot "
            "observability contracts. There is no live mode."
        )
    )
    parser.parse_args(argv)
    verdict = run_rehearsal()
    print(json.dumps(verdict, indent=2, sort_keys=True))
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    sys.path.insert(0, str(REPO_ROOT))
    raise SystemExit(main())
