"""Finite, fail-closed scheduler for durable retention requests."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime

from ..config import get_retention_policy, get_rights_subject_keyring
from ..db import get_privacy_sessionmaker
from ..retention import RetentionScheduleResult, schedule_retention_requests
from .config import retention_scheduling_enabled


def main(argv: Sequence[str] | None = None) -> int:
    """Run one finite retention scheduling transaction for one clinic."""
    parser = argparse.ArgumentParser(
        description="Schedule one bounded Clinic Recall retention inventory."
    )
    parser.add_argument("--clinic-id", required=True, help="Internal clinic scope identifier.")
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum due interactions to inventory (1-1000).",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="Timezone-aware ISO-8601 timestamp; defaults to current UTC time.",
    )
    args = parser.parse_args(argv)

    now = _parse_now(args.now)
    _bootstrap_runtime_configuration()
    if not retention_scheduling_enabled():
        _print_result(enabled=False, result=RetentionScheduleResult(0, 0))
        return 0
    try:
        keyring = get_rights_subject_keyring()
        policy = get_retention_policy()
    except (RuntimeError, ValueError):
        _print_result(
            enabled=False,
            result=RetentionScheduleResult(0, 0),
            configuration_blocked=1,
        )
        return 2

    try:
        with get_privacy_sessionmaker().begin() as session:
            result = schedule_retention_requests(
                session,
                clinic_id=args.clinic_id,
                keyring=keyring,
                policy=policy,
                now=now,
                enabled=True,
                limit=args.limit,
            )
    except (LookupError, RuntimeError, ValueError):
        _print_result(
            enabled=False,
            result=RetentionScheduleResult(0, 0),
            configuration_blocked=1,
        )
        return 2
    _print_result(enabled=True, result=result)
    return 0


def _print_result(
    *,
    enabled: bool,
    result: RetentionScheduleResult,
    configuration_blocked: int = 0,
) -> None:
    summary: dict[str, int | bool] = {
        "enabled": enabled,
        **asdict(result),
        "configuration_blocked": configuration_blocked,
    }
    print(json.dumps(summary, sort_keys=True))


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must include a timezone")
    return parsed.astimezone(UTC)


def _bootstrap_runtime_configuration(
    bootstrap: Callable[[], bool] | None = None,
) -> None:
    if not os.getenv("AZURE_APPCONFIG_ENDPOINT", "").strip():
        return
    if bootstrap is None:
        from apps.artagent.backend.config.appconfig_provider import bootstrap_appconfig

        bootstrap = bootstrap_appconfig
    if not bootstrap():
        raise RuntimeError(
            "Azure App Configuration failed to load; retention scheduler stopped"
        )


if __name__ == "__main__":
    raise SystemExit(main())