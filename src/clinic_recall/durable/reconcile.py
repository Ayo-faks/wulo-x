"""Finite command for applying durable provider callback receipts."""

from __future__ import annotations

import argparse
import json
import os
import socket
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from ..db import get_sessionmaker
from ..telemetry import configure_job_telemetry
from .callbacks import ReconciliationResult, reconcile_once


def main(argv: Sequence[str] | None = None) -> int:
    """Apply one bounded callback-receipt batch when explicitly enabled."""
    parser = argparse.ArgumentParser(
        description="Apply one durable Clinic Recall callback receipt batch."
    )
    parser.add_argument("--clinic-id", required=True, help="Internal clinic scope identifier.")
    parser.add_argument(
        "--worker-id", default=None, help="Lease owner; defaults to execution host."
    )
    parser.add_argument("--limit", type=int, default=50, help="Maximum receipts to claim (1-100).")
    parser.add_argument(
        "--now",
        default=None,
        help="Timezone-aware ISO-8601 timestamp; defaults to current UTC time.",
    )
    args = parser.parse_args(argv)

    enabled = _reconciliation_enabled()
    if not enabled:
        print(json.dumps(ReconciliationResult(enabled=False).as_summary(), sort_keys=True))
        return 0

    _bootstrap_runtime_configuration()
    result = reconcile_once(
        get_sessionmaker(),
        clinic_id=args.clinic_id,
        worker_id=args.worker_id or _default_worker_id(),
        now=_parse_now(args.now),
        enabled=True,
        limit=args.limit,
    )
    print(json.dumps(result.as_summary(), sort_keys=True))
    return 0


def _reconciliation_enabled() -> bool:
    return os.getenv(
        "CLINIC_RECALL_CALLBACK_RECONCILIATION_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}


def _default_worker_id() -> str:
    value = os.getenv("CONTAINER_APP_JOB_EXECUTION_NAME") or socket.gethostname()
    return value.strip()[:128] or "clinic-recall-reconciler"


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must include a timezone")
    return parsed


def _bootstrap_runtime_configuration(
    bootstrap: Callable[[], bool] | None = None,
) -> None:
    if os.getenv("AZURE_APPCONFIG_ENDPOINT", "").strip():
        if bootstrap is None:
            from apps.artagent.backend.config.appconfig_provider import bootstrap_appconfig

            bootstrap = bootstrap_appconfig
        if not bootstrap():
            raise RuntimeError("Azure App Configuration failed to load; reconciliation stopped")
    configure_job_telemetry()


if __name__ == "__main__":
    raise SystemExit(main())
