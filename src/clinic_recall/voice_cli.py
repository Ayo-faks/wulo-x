"""CLI entrypoint for one Clinic Recall outbound voice cadence."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime

from .db import get_sessionmaker
from .durable.planner import _bootstrap_runtime_configuration
from .enums import Channel
from .pilot_controls import (
    JobPilotGate,
    job_gate_for_snapshot,
    operational_switch_snapshot_from_environment,
)
from .voice_worker import run_voice_cadence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan one Clinic Recall voice cadence.")
    parser.add_argument("--clinic-id", required=True, help="Clinic ID to scope the cadence.")
    parser.add_argument(
        "--provider",
        choices=("auto", "acs", "art", "twilio"),
        default=None,
        help="Deprecated compatibility option; planning never constructs a provider.",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="Timezone-aware ISO-8601 timestamp; defaults to current UTC time.",
    )
    args = parser.parse_args(argv)

    now = _parse_now(args.now)
    _bootstrap_runtime_configuration()
    programme_gate = _runtime_programme_gate(now)
    if programme_gate is None:
        print(json.dumps(run_voice_cadence_disabled_summary(), sort_keys=True))
        return 0
    SessionLocal = get_sessionmaker()

    with SessionLocal() as session:
        result = run_voice_cadence(
            session,
            args.clinic_id,
            now,
            programme_gate=programme_gate,
        )
        session.commit()

    print(json.dumps(result.as_summary(), sort_keys=True))
    return 0


def _runtime_programme_gate(now: datetime) -> JobPilotGate | None:
    switches = operational_switch_snapshot_from_environment()
    if not switches.decision(Channel.CALL, now).allowed:
        return None
    return job_gate_for_snapshot(switches, Channel.CALL)


def run_voice_cadence_disabled_summary() -> dict[str, object]:
    from .voice_planner import VoiceCadenceResult

    return VoiceCadenceResult().as_summary()


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must include a timezone")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())