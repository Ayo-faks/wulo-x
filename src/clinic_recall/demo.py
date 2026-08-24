"""Phase 1 demo: produce a candidate queue from sample clinic data.

Builds an ephemeral (in-memory SQLite by default) Clinic Recall database, syncs
a sample CSV, runs the deterministic detection + eligibility pipeline, and
prints the resulting candidate queue with per-reason and per-skip counts.

Examples::

    python -m src.clinic_recall.demo
    python -m src.clinic_recall.demo --csv infra/postgres/sample_clinic_data.csv
    python -m src.clinic_recall.demo --dsn postgresql+psycopg://u:p@host:5432/db

This is a demonstration / smoke entry point. It performs no outreach.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from .candidate_queue import generate_candidate_queue
from .db import clinic_scope, tenant_select
from .enums import Channel
from .models import Appointment, Base, Clinic, OutreachJob, Patient
from .pilot_controls import PilotGateDecision
from .rls import apply_rls_policies
from .sync import CsvSyncSource, upsert_source

DEFAULT_CSV = Path(__file__).resolve().parents[2] / "infra" / "postgres" / "sample_clinic_data.csv"
DEFAULT_NOW = "2026-06-26T12:00:00+00:00"
DEFAULT_CLINIC_ID = "demo-clinic"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clinic Recall Phase 1 candidate-queue demo")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="sample CSV path")
    parser.add_argument("--now", default=DEFAULT_NOW, help="ISO-8601 reference instant")
    parser.add_argument("--clinic-id", default=DEFAULT_CLINIC_ID)
    parser.add_argument(
        "--dsn", default=None, help="optional PostgreSQL DSN (else in-memory SQLite)"
    )
    return parser.parse_args(argv)


def _build_engine(dsn: str | None):
    """Create an engine and schema; apply RLS on PostgreSQL."""
    engine = create_engine(dsn) if dsn else create_engine("sqlite://")
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            apply_rls_policies(conn)
    return engine


def _counts_block(title: str, counts: dict[str, int]) -> list[str]:
    if not counts:
        return [f"{title}: none"]
    lines = [f"{title}:"]
    width = max(len(name) for name in counts)
    for name in sorted(counts):
        lines.append(f"  {name.ljust(width)}  {counts[name]}")
    return lines


def main(argv: list[str] | None = None) -> int:
    """Run the demo and print a summary. Returns a process exit code."""
    args = _parse_args(argv)
    now = datetime.fromisoformat(args.now)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    engine = _build_engine(args.dsn)
    with Session(engine, expire_on_commit=False) as session:
        session.add(
            Clinic(id=args.clinic_id, name="Demo Clinic", timezone="Europe/London", daily_caps=200)
        )
        session.flush()

        sync = upsert_source(session, args.clinic_id, CsvSyncSource.from_path(args.csv))
        result = generate_candidate_queue(
            session,
            args.clinic_id,
            now,
            channel=Channel.SMS,
            pilot_gate=_synthetic_demo_pilot_gate,
        )
        session.commit()

        lines = [
            "Clinic Recall - Phase 1 candidate-queue demo",
            f"Clinic: {args.clinic_id}   As of: {now.isoformat()}   Channel: sms",
            f"Synced: {sync.patients_inserted} patients, {sync.appointments_inserted} appointments",
            "",
            *_counts_block(f"Detected {result.detected_total} candidates", dict(result.detected)),
            "",
            f"Eligible -> queued: {result.queued}   (already queued: {result.already_queued})",
            *_counts_block("Held back", dict(result.skipped)),
            "",
            f"Candidate queue ({result.queued} pending outreach jobs):",
        ]
        lines.extend(_queue_lines(session, args.clinic_id))
        print("\n".join(lines))
    engine.dispose()
    return 0


def _synthetic_demo_pilot_gate(*_args) -> PilotGateDecision:
    """Allow only this local synthetic demonstration to exercise queue output."""
    return PilotGateDecision(True, "synthetic_demo")


def _queue_lines(session: Session, clinic_id: str) -> list[str]:
    """Render the pending outreach jobs with patient name and reason."""
    with clinic_scope(session, clinic_id):
        jobs = session.execute(tenant_select(OutreachJob)).scalars().all()
        patients = {p.id: p for p in session.execute(tenant_select(Patient)).scalars().all()}
        appts = {a.id: a for a in session.execute(tenant_select(Appointment)).scalars().all()}
    rows = []
    for job in jobs:
        patient = patients.get(job.patient_id)
        appt = appts.get(job.appointment_id) if job.appointment_id else None
        reason = job.reason_code.value if job.reason_code else "?"
        when = appt.start_at.date().isoformat() if appt else "?"
        name = patient.name if patient else job.patient_id
        rows.append(f"  [{reason.ljust(18)}] {name.ljust(20)} appt {when}  via {job.channel.value}")
    return rows


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
