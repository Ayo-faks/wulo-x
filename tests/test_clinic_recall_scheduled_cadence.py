"""Finite, tenant-scoped scheduled cadence planning contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from src.clinic_recall.durable import planner as planner_module
from src.clinic_recall.durable.config import cadence_planning_enabled
from src.clinic_recall.durable.enqueue import enqueue_sms_effect
from src.clinic_recall.durable.planner import (
    PlanningPassResult,
)
from src.clinic_recall.durable.planner import (
    run_planning_pass as _run_planning_pass,
)
from src.clinic_recall.enums import CampaignStatus, CampaignType, Channel, OutreachState
from src.clinic_recall.models import (
    TENANT_TABLES,
    Appointment,
    CadenceCursor,
    Campaign,
    Clinic,
    ExternalEffect,
    OutreachJob,
    Patient,
)
from src.clinic_recall.pilot_controls import PilotGateDecision

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def _allow_patient(*_args) -> PilotGateDecision:
    return PilotGateDecision(True, "allowed")


def _allow_programme(*_args) -> PilotGateDecision:
    return PilotGateDecision(True, "allowed")


def run_planning_pass(*args, **kwargs):
    kwargs.setdefault("sms_pilot_gate", _allow_patient)
    kwargs.setdefault("programme_gate", _allow_programme)
    return _run_planning_pass(*args, **kwargs)


def _seed_due_sms(session: Session) -> str:
    clinic_id = "clinic-scheduled-cadence"
    session.add(Clinic(id=clinic_id, name="Scheduled Cadence Clinic"))
    session.add(
        Patient(
            id="patient-scheduled-cadence",
            clinic_id=clinic_id,
            source_ref="patient-scheduled-cadence",
            name="Synthetic Patient",
            phone="+447700900001",
            consent_flags={"sms": True, "call": True},
            opt_out_flags={},
        )
    )
    session.add(
        Appointment(
            id="appointment-scheduled-cadence",
            clinic_id=clinic_id,
            patient_id="patient-scheduled-cadence",
            source_ref="appointment-scheduled-cadence",
            status="missed",
            start_at=NOW - timedelta(days=2),
        )
    )
    session.add(
        Campaign(
            id="campaign-scheduled-cadence",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    session.add(
        OutreachJob(
            id="job-scheduled-cadence",
            clinic_id=clinic_id,
            campaign_id="campaign-scheduled-cadence",
            patient_id="patient-scheduled-cadence",
            appointment_id="appointment-scheduled-cadence",
            channel=Channel.SMS,
            state=OutreachState.QUEUED,
        )
    )
    session.commit()
    return clinic_id


def test_cadence_cursor_is_minimized_and_rls_registered() -> None:
    assert "cadence_cursor" in TENANT_TABLES
    assert set(CadenceCursor.__table__.c.keys()) == {
        "id",
        "clinic_id",
        "planner_name",
        "watermark_at",
        "last_started_at",
        "last_completed_at",
        "last_run_id",
        "created_at",
        "updated_at",
    }
    assert set(CadenceCursor.__table__.c.keys()).isdisjoint(
        {
            "patient_id",
            "name",
            "phone",
            "email",
            "message_body",
            "provider_id",
            "provider_error",
        }
    )


def test_planning_pass_commits_effect_and_cursor_once(sqlite_session: Session) -> None:
    clinic_id = _seed_due_sms(sqlite_session)
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)

    first = run_planning_pass(
        factory,
        clinic_id=clinic_id,
        now=NOW,
        enabled=True,
        window=timedelta(hours=1),
        batch_size=10,
    )
    second = run_planning_pass(
        factory,
        clinic_id=clinic_id,
        now=NOW,
        enabled=True,
        window=timedelta(hours=1),
        batch_size=10,
    )

    sqlite_session.expire_all()
    cursor = sqlite_session.execute(select(CadenceCursor)).scalar_one()
    assert first.sms_enqueued == 1
    assert first.cursor_advanced is True
    assert second.sms_enqueued == 0
    assert second.cursor_advanced is False
    assert sqlite_session.scalar(select(func.count()).select_from(ExternalEffect)) == 1
    assert cursor.watermark_at.replace(tzinfo=UTC) == NOW
    assert cursor.last_completed_at is not None


def test_planning_pass_rollback_leaves_no_effect_or_cursor(sqlite_session: Session) -> None:
    clinic_id = _seed_due_sms(sqlite_session)
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)

    def failing_sms_planner(
        session: Session,
        scoped_clinic_id: str,
        now: datetime,
        *,
        limit: int,
        pilot_gate,
    ) -> PlanningPassResult:
        del limit, pilot_gate
        enqueue_sms_effect(
            session,
            clinic_id=scoped_clinic_id,
            outreach_job_id="job-scheduled-cadence",
            idempotency_key="cadence:sms:job-scheduled-cadence",
            available_at=now,
        )
        raise RuntimeError("synthetic planner rollback")

    with pytest.raises(RuntimeError, match="synthetic planner rollback"):
        run_planning_pass(
            factory,
            clinic_id=clinic_id,
            now=NOW,
            enabled=True,
            window=timedelta(hours=1),
            batch_size=10,
            sms_planner=failing_sms_planner,
        )

    sqlite_session.expire_all()
    assert sqlite_session.scalar(select(func.count()).select_from(ExternalEffect)) == 0
    assert sqlite_session.scalar(select(func.count()).select_from(CadenceCursor)) == 0


def test_planning_pass_resumes_from_last_committed_watermark_without_duplicate(
    sqlite_session: Session,
) -> None:
    clinic_id = _seed_due_sms(sqlite_session)
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    first = run_planning_pass(
        factory,
        clinic_id=clinic_id,
        now=NOW,
        enabled=True,
    )

    def failing_sms_planner(
        session: Session,
        scoped_clinic_id: str,
        now: datetime,
        *,
        limit: int,
        pilot_gate,
    ) -> PlanningPassResult:
        del limit, pilot_gate
        enqueue_sms_effect(
            session,
            clinic_id=scoped_clinic_id,
            outreach_job_id="job-scheduled-cadence",
            idempotency_key="cadence:sms:job-scheduled-cadence",
            available_at=now,
        )
        raise RuntimeError("synthetic interrupted interval")

    with pytest.raises(RuntimeError, match="synthetic interrupted interval"):
        run_planning_pass(
            factory,
            clinic_id=clinic_id,
            now=NOW + timedelta(hours=1),
            enabled=True,
            sms_planner=failing_sms_planner,
        )

    sqlite_session.expire_all()
    cursor = sqlite_session.execute(select(CadenceCursor)).scalar_one()
    assert cursor.watermark_at.replace(tzinfo=UTC) == NOW

    observed_watermarks: list[datetime] = []

    def observing_sms_planner(
        session: Session,
        _clinic_id: str,
        _now: datetime,
        *,
        limit: int,
        pilot_gate,
    ) -> PlanningPassResult:
        del limit, pilot_gate
        current = session.execute(select(CadenceCursor)).scalar_one()
        observed_watermarks.append(current.watermark_at.replace(tzinfo=UTC))
        return PlanningPassResult(enabled=True)

    resumed = run_planning_pass(
        factory,
        clinic_id=clinic_id,
        now=NOW + timedelta(hours=2),
        enabled=True,
        sms_planner=observing_sms_planner,
        voice_planner=lambda *_args, **_kwargs: PlanningPassResult(enabled=True),
    )

    sqlite_session.expire_all()
    cursor = sqlite_session.execute(select(CadenceCursor)).scalar_one()
    assert first.sms_enqueued == 1
    assert observed_watermarks == [NOW]
    assert resumed.cursor_advanced is True
    assert cursor.watermark_at.replace(tzinfo=UTC) == NOW + timedelta(hours=2)
    assert sqlite_session.scalar(select(func.count()).select_from(ExternalEffect)) == 1


def test_cadence_planning_gate_requires_fresh_complete_configuration(monkeypatch) -> None:
    for name in (
        "CLINIC_RECALL_CADENCE_PLANNING_ENABLED",
        "CLINIC_RECALL_CADENCE_CONFIG_REFRESHED_AT",
        "CLINIC_RECALL_CADENCE_CONFIG_MAX_AGE_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert cadence_planning_enabled(NOW) is False

    monkeypatch.setenv("CLINIC_RECALL_CADENCE_PLANNING_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_CADENCE_CONFIG_MAX_AGE_SECONDS", "300")
    assert cadence_planning_enabled(NOW) is False

    monkeypatch.setenv("CLINIC_RECALL_CADENCE_CONFIG_REFRESHED_AT", "malformed")
    assert cadence_planning_enabled(NOW) is False

    monkeypatch.setenv(
        "CLINIC_RECALL_CADENCE_CONFIG_REFRESHED_AT",
        (NOW - timedelta(seconds=301)).isoformat(),
    )
    assert cadence_planning_enabled(NOW) is False

    monkeypatch.setenv(
        "CLINIC_RECALL_CADENCE_CONFIG_REFRESHED_AT",
        (NOW + timedelta(seconds=1)).isoformat(),
    )
    assert cadence_planning_enabled(NOW) is False

    monkeypatch.setenv(
        "CLINIC_RECALL_CADENCE_CONFIG_REFRESHED_AT",
        (NOW - timedelta(seconds=30)).isoformat(),
    )
    assert cadence_planning_enabled(NOW) is True

    monkeypatch.setenv("CLINIC_RECALL_CADENCE_PLANNING_ENABLED", "unexpected")
    assert cadence_planning_enabled(NOW) is False


def test_disabled_planner_cli_does_not_open_database(monkeypatch, capsys) -> None:
    monkeypatch.delenv("CLINIC_RECALL_CADENCE_PLANNING_ENABLED", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_CADENCE_CONFIG_REFRESHED_AT", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_CADENCE_CONFIG_MAX_AGE_SECONDS", raising=False)
    monkeypatch.setattr(
        planner_module,
        "get_sessionmaker",
        lambda: (_ for _ in ()).throw(AssertionError("disabled planner opened database")),
        raising=False,
    )

    assert (
        planner_module.main(
            [
                "--clinic-id",
                "clinic-internal-test",
                "--now",
                NOW.isoformat(),
                "--batch-size",
                "10",
                "--window-minutes",
                "60",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == (
        '{"call_existing": 0, "calls_canceled": 0, "calls_enqueued": 0, '
        '"cursor_advanced": false, "email_policy_excluded": 0, "enabled": false, '
        '"sms_canceled": 0, "sms_enqueued": 0, "sms_existing": 0}'
    )


def test_planner_source_has_no_provider_client_boundary() -> None:
    source = Path("src/clinic_recall/durable/planner.py").read_text(encoding="utf-8")
    for forbidden in (
        "AcsSmsSender",
        "TwilioSmsSender",
        "CallInitiator",
        "build_call_initiator",
        "send_sms",
        "send_email",
        "httpx",
    ):
        assert forbidden not in source


def test_enabled_planner_bootstraps_config_before_database(monkeypatch) -> None:
    monkeypatch.setenv("CLINIC_RECALL_CADENCE_PLANNING_ENABLED", "true")
    monkeypatch.setenv(
        "CLINIC_RECALL_CADENCE_CONFIG_REFRESHED_AT",
        (NOW - timedelta(seconds=30)).isoformat(),
    )
    monkeypatch.setenv("CLINIC_RECALL_CADENCE_CONFIG_MAX_AGE_SECONDS", "300")
    monkeypatch.setattr(
        planner_module,
        "_bootstrap_runtime_configuration",
        lambda: (_ for _ in ()).throw(RuntimeError("synthetic bootstrap failure")),
        raising=False,
    )
    monkeypatch.setattr(
        planner_module,
        "get_sessionmaker",
        lambda: (_ for _ in ()).throw(AssertionError("database opened before bootstrap")),
    )

    with pytest.raises(RuntimeError, match="synthetic bootstrap failure"):
        planner_module.main(["--clinic-id", "clinic-internal-test", "--now", NOW.isoformat()])


def test_appconfig_can_enable_planning_before_database_is_opened(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("CLINIC_RECALL_CADENCE_PLANNING_ENABLED", "false")
    monkeypatch.delenv("CLINIC_RECALL_CADENCE_CONFIG_REFRESHED_AT", raising=False)
    monkeypatch.setenv("CLINIC_RECALL_CADENCE_CONFIG_MAX_AGE_SECONDS", "300")
    events: list[str] = []

    def bootstrap() -> None:
        events.append("bootstrap")
        monkeypatch.setenv("CLINIC_RECALL_CADENCE_PLANNING_ENABLED", "true")
        monkeypatch.setenv(
            "CLINIC_RECALL_CADENCE_CONFIG_REFRESHED_AT",
            (NOW - timedelta(seconds=30)).isoformat(),
        )
        monkeypatch.setenv("CLINIC_RECALL_PILOT_OUTREACH_ENABLED", "true")
        monkeypatch.setenv("CLINIC_RECALL_PILOT_VOICE_ENABLED", "true")
        monkeypatch.setenv("CLINIC_RECALL_PILOT_RECORDING_ENABLED", "false")
        monkeypatch.setenv(
            "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT",
            (NOW - timedelta(seconds=30)).isoformat(),
        )
        monkeypatch.setenv("CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS", "300")
        monkeypatch.setenv("CLINIC_RECALL_PILOT_ENVIRONMENT", "production")
        monkeypatch.setenv(
            "CLINIC_RECALL_PILOT_RELEASE_IDENTITY",
            "sha256:test-release",
        )

    def get_factory():
        events.append("database")
        return object()

    monkeypatch.setattr(planner_module, "_bootstrap_runtime_configuration", bootstrap)
    monkeypatch.setattr(planner_module, "get_sessionmaker", get_factory)
    monkeypatch.setattr(
        planner_module,
        "run_planning_pass",
        lambda *_args, **_kwargs: PlanningPassResult(enabled=True),
    )

    assert (
        planner_module.main(["--clinic-id", "clinic-internal-test", "--now", NOW.isoformat()]) == 0
    )
    assert events == ["bootstrap", "database"]
    assert '"enabled": true' in capsys.readouterr().out
