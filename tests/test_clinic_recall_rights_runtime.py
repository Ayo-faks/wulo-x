"""Runtime command contracts for PR-10 rights and retention Jobs."""

from __future__ import annotations

import json
from contextlib import nullcontext

from src.clinic_recall.durable import retention_scheduler, rights_worker
from src.clinic_recall.retention import RetentionScheduleResult


def test_rights_command_defaults_off_before_adapter_or_database_access(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RIGHTS_ENABLED", "false")
    monkeypatch.setattr(rights_worker, "_bootstrap_runtime_configuration", lambda: None)
    monkeypatch.setattr(
        rights_worker,
        "_runtime_adapters",
        lambda clinic_id: (_ for _ in ()).throw(AssertionError("adapter access")),
    )
    monkeypatch.setattr(
        rights_worker,
        "get_privacy_sessionmaker",
        lambda: (_ for _ in ()).throw(AssertionError("database access")),
    )

    exit_code = rights_worker.main(
        ["--clinic-id", "clinic-runtime", "--now", "2026-07-23T12:00:00Z"]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "claimed": 0,
        "configuration_blocked": 0,
        "enabled": False,
        "reconcile_required": 0,
        "residual": 0,
        "retried": 0,
        "verified": 0,
    }


def test_rights_command_blocks_enabled_run_with_incomplete_adapter_configuration(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RIGHTS_ENABLED", "true")
    monkeypatch.setattr(rights_worker, "_bootstrap_runtime_configuration", lambda: None)
    monkeypatch.setattr(rights_worker, "_runtime_adapters", lambda clinic_id: None)

    exit_code = rights_worker.main(["--clinic-id", "clinic-runtime"])

    assert exit_code == 2
    output = capsys.readouterr().out
    assert json.loads(output)["configuration_blocked"] == 1
    assert "TWILIO" not in output
    assert "secret" not in output.lower()


def test_rights_command_blocks_before_database_when_completion_policy_is_missing(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RIGHTS_ENABLED", "true")
    monkeypatch.setattr(rights_worker, "_bootstrap_runtime_configuration", lambda: None)
    monkeypatch.setattr(rights_worker, "_runtime_adapters", lambda clinic_id: {})
    monkeypatch.setattr(
        rights_worker,
        "_runtime_completion_configuration",
        lambda: None,
    )
    monkeypatch.setattr(
        rights_worker,
        "get_privacy_sessionmaker",
        lambda: (_ for _ in ()).throw(AssertionError("database access")),
    )

    exit_code = rights_worker.main(["--clinic-id", "clinic-runtime"])

    assert exit_code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["enabled"] is False
    assert output["configuration_blocked"] == 1


def test_rights_command_both_dispatches_then_reconciles(monkeypatch, capsys) -> None:
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RIGHTS_ENABLED", "true")
    monkeypatch.setattr(rights_worker, "_bootstrap_runtime_configuration", lambda: None)
    monkeypatch.setattr(rights_worker, "_runtime_adapters", lambda clinic_id: {})
    keyring = object()
    approvals = {object(): object()}
    monkeypatch.setattr(
        rights_worker,
        "_runtime_completion_configuration",
        lambda: (keyring, approvals),
    )
    session_factory = object()
    monkeypatch.setattr(
        rights_worker,
        "get_privacy_sessionmaker",
        lambda: session_factory,
    )
    calls: list[str] = []

    def dispatch(factory, **kwargs):
        assert factory is session_factory
        calls.append("dispatch")
        return rights_worker.RightsRunOnceResult(enabled=True, claimed=2, verified=1)

    def reconcile(factory, **kwargs):
        assert factory is session_factory
        calls.append("reconcile")
        return rights_worker.RightsReconcileResult(
            enabled=True,
            inspected=1,
            verified=1,
        )

    monkeypatch.setattr(rights_worker, "run_once", dispatch)
    monkeypatch.setattr(rights_worker, "reconcile_once", reconcile)

    def finalize(factory, **kwargs):
        assert factory is session_factory
        assert kwargs["keyring"] is keyring
        assert kwargs["approvals"] is approvals
        calls.append("finalize")
        return rights_worker.RightsFinalizationResult(
            inspected_count=2,
            completed_count=1,
            blocked_count=1,
            approvals_applied=7,
        )

    def maintain(factory, **kwargs):
        assert factory is session_factory
        assert kwargs["approvals"] is approvals
        calls.append("maintain")
        return rights_worker.RightsResidualMaintenanceResult(
            inspected_count=7,
            approvals_applied=7,
            overdue_count=0,
        )

    monkeypatch.setattr(rights_worker, "_maintain_runtime", maintain)
    monkeypatch.setattr(rights_worker, "_finalize_runtime", finalize)

    exit_code = rights_worker.main(
        ["--clinic-id", "clinic-runtime", "--mode", "both"]
    )

    assert exit_code == 0
    assert calls == ["dispatch", "reconcile", "maintain", "finalize"]
    assert json.loads(capsys.readouterr().out) == {
        "dispatch": {
            "claimed": 2,
            "configuration_blocked": 0,
            "enabled": True,
            "reconcile_required": 0,
            "residual": 0,
            "retried": 0,
            "verified": 1,
        },
        "enabled": True,
        "finalize": {
            "approvals_applied": 7,
            "blocked_count": 1,
            "completed_count": 1,
            "inspected_count": 2,
        },
        "residual_maintenance": {
            "approvals_applied": 7,
            "inspected_count": 7,
            "overdue_count": 0,
        },
        "reconcile": {
            "configuration_blocked": 0,
            "enabled": True,
            "handoffs_queued": 0,
            "inspected": 1,
            "reconcile_required": 0,
            "residual": 0,
            "retried": 0,
            "verified": 1,
        },
    }


def test_retention_command_defaults_off_before_policy_or_database_access(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("CLINIC_RECALL_RETENTION_SCHEDULER_ENABLED", "false")
    monkeypatch.setattr(
        retention_scheduler,
        "_bootstrap_runtime_configuration",
        lambda: None,
    )
    monkeypatch.setattr(
        retention_scheduler,
        "get_retention_policy",
        lambda: (_ for _ in ()).throw(AssertionError("policy access")),
    )
    monkeypatch.setattr(
        retention_scheduler,
        "get_privacy_sessionmaker",
        lambda: (_ for _ in ()).throw(AssertionError("database access")),
    )

    exit_code = retention_scheduler.main(["--clinic-id", "clinic-runtime"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "configuration_blocked": 0,
        "created_count": 0,
        "enabled": False,
        "existing_count": 0,
    }


def test_retention_command_runs_one_transaction_with_explicit_configuration(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("CLINIC_RECALL_RETENTION_SCHEDULER_ENABLED", "true")
    monkeypatch.setattr(
        retention_scheduler,
        "_bootstrap_runtime_configuration",
        lambda: None,
    )
    keyring = object()
    policy = object()
    session = object()

    class _Factory:
        def begin(self):
            return nullcontext(session)

    monkeypatch.setattr(retention_scheduler, "get_privacy_sessionmaker", _Factory)
    monkeypatch.setattr(retention_scheduler, "get_rights_subject_keyring", lambda: keyring)
    monkeypatch.setattr(retention_scheduler, "get_retention_policy", lambda: policy)

    def schedule(active_session, **kwargs):
        assert active_session is session
        assert kwargs["clinic_id"] == "clinic-runtime"
        assert kwargs["keyring"] is keyring
        assert kwargs["policy"] is policy
        assert kwargs["enabled"] is True
        return RetentionScheduleResult(created_count=3, existing_count=2)

    monkeypatch.setattr(retention_scheduler, "schedule_retention_requests", schedule)

    exit_code = retention_scheduler.main(
        ["--clinic-id", "clinic-runtime", "--now", "2026-07-23T12:00:00Z"]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "configuration_blocked": 0,
        "created_count": 3,
        "enabled": True,
        "existing_count": 2,
    }