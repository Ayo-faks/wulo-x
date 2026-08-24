"""PR-07 durable Cliniko create, ambiguity, and confirmation grounding."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.clinic_recall.availability import AvailabilitySlotInput, upsert_availability_slots
from src.clinic_recall.booking import book_slot
from src.clinic_recall.config import ClinikoConfig
from src.clinic_recall.durable import cliniko_booking_reconciler, cliniko_booking_worker
from src.clinic_recall.durable.callbacks import (
    CallbackCorrelationError,
    receive_twilio_callback,
)
from src.clinic_recall.durable.cliniko_booking_reconciler import (
    reconcile_once as _reconcile_cliniko_bookings,
)
from src.clinic_recall.durable.cliniko_booking_state import (
    finalize_verified,
    load_dispatch_context,
    preflight_zero_match_hash,
)
from src.clinic_recall.durable.cliniko_booking_worker import (
    run_once as _run_cliniko_bookings,
)
from src.clinic_recall.durable.effects import claim_effects, mark_dispatching
from src.clinic_recall.enums import (
    BookingWriteBackState,
    Channel,
    ExternalEffectState,
    ExternalEffectType,
    ProviderCallbackKind,
)
from src.clinic_recall.identity_evidence import IdentityEvidenceService
from src.clinic_recall.models import (
    Appointment,
    Base,
    BookingAction,
    Campaign,
    Clinic,
    ExternalEffect,
    ExternalEffectHandoff,
    OutreachJob,
    Patient,
)
from src.clinic_recall.pilot_controls import PilotGateDecision
from src.clinic_recall.rights import SubjectFrozenError
from src.clinic_recall.sync.cliniko_booking import (
    ClinikoBookingClient,
    ObservedAppointment,
)

from tests.identity_evidence_support import (
    grant_synthetic_t2,
    synthetic_identity_policy,
)

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
CLINIC_ID = "clinic-pr07-synthetic"
PATIENT_ID = "patient-pr07-synthetic"
PATIENT_SOURCE_ID = "900700001"
PROVIDER_APPOINTMENT_ID = "950700001"
FIXTURES = Path("tests/fixtures/cliniko/pr07")


def _allow_pilot(*_args) -> PilotGateDecision:
    return PilotGateDecision(True, "allowed")


class _StopBeforeSettlementGate:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args) -> PilotGateDecision:
        self.calls += 1
        return PilotGateDecision(
            self.calls <= 2,
            "allowed" if self.calls <= 2 else "operational_stop",
        )


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _AcceptedThenLostTransport:
    """Stateful provider: accept one create, then lose its response."""

    def __init__(self) -> None:
        self.appointments: dict[str, dict[str, object]] = {}
        self.method_counts: dict[str, int] = {"GET": 0, "POST": 0, "PATCH": 0}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.method_counts[request.method] += 1
        if request.method == "GET" and request.url.path.endswith("/available_times"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "available_times": [
                        {"appointment_start": "2026-08-04T09:00:00Z"}
                    ],
                    "total_entries": 1,
                    "links": {},
                },
            )
        if request.method == "GET" and request.url.path == "/v1/individual_appointments":
            return httpx.Response(
                200,
                request=request,
                json={
                    "individual_appointments": list(self.appointments.values()),
                    "total_entries": len(self.appointments),
                    "links": {},
                },
            )
        if request.method == "POST" and request.url.path == "/v1/individual_appointments":
            assert self.method_counts["POST"] == 1
            body = json.loads(request.content)
            appointment = {
                "id": PROVIDER_APPOINTMENT_ID,
                "appointment_type": {
                    "links": {
                        "self": "https://api.uk2.cliniko.com/v1/appointment_types/940700001"
                    }
                },
                "business": {
                    "links": {"self": "https://api.uk2.cliniko.com/v1/businesses/920700001"}
                },
                "patient": {
                    "links": {
                        "self": f"https://api.uk2.cliniko.com/v1/patients/{PATIENT_SOURCE_ID}"
                    }
                },
                "practitioner": {
                    "links": {
                        "self": "https://api.uk2.cliniko.com/v1/practitioners/930700001"
                    }
                },
                "starts_at": body["starts_at"],
                "ends_at": body["ends_at"],
                "cancelled_at": None,
                "archived_at": None,
                "deleted_at": None,
                "updated_at": NOW.isoformat().replace("+00:00", "Z"),
                "links": {
                    "self": (
                        "https://api.uk2.cliniko.com/v1/individual_appointments/"
                        f"{PROVIDER_APPOINTMENT_ID}"
                    )
                },
            }
            self.appointments[PROVIDER_APPOINTMENT_ID] = appointment
            raise httpx.ReadTimeout("synthetic response lost", request=request)
        raise AssertionError(f"unexpected synthetic request: {request.method} {request.url.path}")


def _factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(Clinic(id=CLINIC_ID, name="Synthetic PR-07 Clinic"))
        session.add(
            Patient(
                id=PATIENT_ID,
                clinic_id=CLINIC_ID,
                source_ref=PATIENT_SOURCE_ID,
                name="Synthetic Patient",
                phone="+447700900701",
                consent_flags={"call": True, "sms": True},
                opt_out_flags={},
            )
        )
        session.add(
            Appointment(
                id="appointment-pr07-source",
                clinic_id=CLINIC_ID,
                patient_id=PATIENT_ID,
                source_ref="910700001",
                status="missed",
                start_at=NOW - timedelta(days=5),
            )
        )
        session.add(
            Campaign(
                id="campaign-pr07",
                clinic_id=CLINIC_ID,
                type="recovery",
                status="active",
            )
        )
        session.add(
            OutreachJob(
                id="job-pr07",
                clinic_id=CLINIC_ID,
                campaign_id="campaign-pr07",
                patient_id=PATIENT_ID,
                appointment_id="appointment-pr07-source",
                channel="call",
                state="no_reply",
            )
        )
        session.flush()
        slot = upsert_availability_slots(
            session,
            CLINIC_ID,
            [
                AvailabilitySlotInput(
                    source_ref="cliniko:v1:" + "7" * 64,
                    source_provider="cliniko",
                    business_id="920700001",
                    appointment_type_id="940700001",
                    clinician_id="930700001",
                    start_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
                    end_at=datetime(2026, 8, 4, 9, 30, tzinfo=UTC),
                    fetched_at=NOW,
                    expires_at=NOW + timedelta(minutes=10),
                )
            ],
            now=NOW,
        )[0]
        identity_service, identity_context = grant_synthetic_t2(
            session,
            clinic_id=CLINIC_ID,
            patient_id=PATIENT_ID,
            channel=Channel.CALL,
            now=NOW,
            suffix="pr07-worker",
        )
        booking = book_slot(
            session,
            CLINIC_ID,
            patient_id=PATIENT_ID,
            outreach_job_id="job-pr07",
            slot_id=slot.slot_id,
            now=NOW,
            write_back_enabled=True,
            identity_service=identity_service,
            identity_context=identity_context,
        )
        assert booking.booking_action_id is not None
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        assert effect.payload == {
            "intent": "create",
            "booking_action_id": booking.booking_action_id,
        }
    return factory


def _identity_service() -> IdentityEvidenceService:
    return IdentityEvidenceService(
        policy=synthetic_identity_policy(),
        clock=lambda: NOW,
        identifier_factory=lambda: "unused-identity-id",
        challenge_factory=lambda: "unused-identity-challenge",
    )


def run_cliniko_bookings(*args, **kwargs):
    kwargs.setdefault("identity_service", _identity_service())
    return _run_cliniko_bookings(*args, **kwargs)


def reconcile_cliniko_bookings(*args, **kwargs):
    kwargs.setdefault("identity_service", _identity_service())
    return _reconcile_cliniko_bookings(*args, **kwargs)


def _client(transport: _AcceptedThenLostTransport) -> ClinikoBookingClient:
    config = ClinikoConfig(
        enabled=True,
        api_key="fixture-uk2",
        shard="uk2",
        user_agent="Wulo Synthetic Tests (engineering@example.test)",
        timeout_seconds=2.0,
        per_page=100,
        max_pages=2,
        max_items=10,
    )
    return ClinikoBookingClient(
        config,
        client=httpx.Client(transport=httpx.MockTransport(transport)),
    )


class _ScenarioTransport:
    def __init__(
        self,
        *,
        preflight_status: int = 200,
        create_status: int = 201,
        read_status: int = 200,
        read_payload: dict[str, object] | None = None,
        slot_available: bool = True,
    ) -> None:
        self.preflight_status = preflight_status
        self.create_status = create_status
        self.read_status = read_status
        self.read_payload = read_payload or _fixture("create_response.json")
        self.slot_available = slot_available
        self.created = False
        self.method_counts = {"GET": 0, "POST": 0, "PATCH": 0}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.method_counts[request.method] += 1
        if request.method == "GET" and request.url.path.endswith("/available_times"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "available_times": (
                        [{"appointment_start": "2026-08-04T09:00:00Z"}]
                        if self.slot_available
                        else []
                    ),
                    "total_entries": 1 if self.slot_available else 0,
                    "links": {},
                },
            )
        if request.method == "GET" and request.url.path == "/v1/individual_appointments":
            if self.preflight_status != 200:
                headers = (
                    {"X-RateLimit-Reset": "1784981100"}
                    if self.preflight_status == 429
                    else None
                )
                return httpx.Response(
                    self.preflight_status,
                    request=request,
                    headers=headers,
                    json={},
                )
            payload = (
                {
                    "individual_appointments": [self.read_payload],
                    "total_entries": 1,
                    "links": {},
                }
                if self.created
                else _fixture("reconciliation_zero.json")
            )
            return httpx.Response(200, request=request, json=payload)
        if request.method == "POST" and request.url.path == "/v1/individual_appointments":
            if self.create_status == 201:
                request_body = json.loads(request.content)
                self.read_payload["starts_at"] = request_body["starts_at"]
                self.read_payload["ends_at"] = request_body["ends_at"]
                self.created = True
                return httpx.Response(201, request=request, json=self.read_payload)
            headers = (
                {"X-RateLimit-Reset": "1784981100"}
                if self.create_status == 429
                else None
            )
            return httpx.Response(
                self.create_status,
                request=request,
                headers=headers,
                json={"message": "synthetic private provider text"},
            )
        if (
            request.method == "GET"
            and request.url.path
            == f"/v1/individual_appointments/{PROVIDER_APPOINTMENT_ID}"
        ):
            return httpx.Response(
                self.read_status,
                request=request,
                json=self.read_payload if self.read_status == 200 else {},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")


class _StaticListTransport:
    def __init__(
        self,
        payload: dict[str, object],
        *,
        status: int = 200,
    ) -> None:
        self.payload = payload
        self.status = status
        self.method_counts = {"GET": 0, "POST": 0, "PATCH": 0}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.method_counts[request.method] += 1
        assert request.method == "GET"
        assert request.url.path == "/v1/individual_appointments"
        headers = (
            {"X-RateLimit-Reset": "1784981100"}
            if self.status == 429
            else None
        )
        return httpx.Response(
            self.status,
            request=request,
            headers=headers,
            json=self.payload,
        )


class _ExactGetTransport:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.method_counts = {"GET": 0, "POST": 0, "PATCH": 0}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.method_counts[request.method] += 1
        assert request.method == "GET"
        assert request.url.path == (
            f"/v1/individual_appointments/{PROVIDER_APPOINTMENT_ID}"
        )
        return httpx.Response(200, request=request, json=self.payload)


class _TrackingSession(Session):
    active_contexts = 0

    def __enter__(self):
        type(self).active_contexts += 1
        return super().__enter__()

    def __exit__(self, type_, value, traceback):
        try:
            return super().__exit__(type_, value, traceback)
        finally:
            type(self).active_contexts -= 1


class _NoDatabaseDuringHttpTransport(_ScenarioTransport):
    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert _TrackingSession.active_contexts == 0
        return super().__call__(request)


def test_accepted_create_with_lost_response_reconciles_without_replay() -> None:
    factory = _factory()
    transport = _AcceptedThenLostTransport()
    client = _client(transport)

    first = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-write-1",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )
    second = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-write-2",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=1),
        enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        booking_effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        confirmation_effects = list(
            session.execute(
                select(ExternalEffect).where(
                    ExternalEffect.effect_type == ExternalEffectType.SMS
                )
            ).scalars()
        )
        assert first.reconcile_required == 1
        assert second.claimed == 0
        assert transport.method_counts["POST"] == 1
        assert action.write_back_state == BookingWriteBackState.RECONCILE_REQUIRED
        assert action.written_back is False
        assert booking_effect.state == ExternalEffectState.RECONCILE_REQUIRED
        assert confirmation_effects == []

    reconciled = reconcile_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-read-1",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=2),
        enabled=True,
        confirmation_release_enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        booking_effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        confirmation_effects = list(
            session.execute(
                select(ExternalEffect).where(
                    ExternalEffect.effect_type == ExternalEffectType.SMS
                )
            ).scalars()
        )
        assert reconciled.verified == 1
        assert transport.method_counts["POST"] == 1
        assert action.write_back_state == BookingWriteBackState.VERIFIED
        assert action.written_back is True
        assert action.external_appointment_ref == PROVIDER_APPOINTMENT_ID
        assert action.read_back_verified_at == NOW + timedelta(seconds=2)
        assert booking_effect.state == ExternalEffectState.SUCCEEDED
        assert booking_effect.completion_evidence_hash
        assert len(confirmation_effects) == 1
        assert confirmation_effects[0].payload["intent"] == "booking_confirmation"


@pytest.mark.parametrize(
    ("module", "switch_name"),
    [
        (
            cliniko_booking_worker,
            "CLINIC_RECALL_DURABLE_CLINIKO_WRITE_ENABLED",
        ),
        (
            cliniko_booking_reconciler,
            "CLINIC_RECALL_CLINIKO_BOOKING_RECONCILIATION_ENABLED",
        ),
    ],
)
def test_runtime_entrypoint_is_finite_and_default_off_before_client_construction(
    module,
    switch_name: str,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("AZURE_APPCONFIG_ENDPOINT", raising=False)
    monkeypatch.delenv(switch_name, raising=False)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled entrypoint constructed runtime dependencies")

    monkeypatch.setattr(module, "get_cliniko_config", forbidden, raising=False)
    monkeypatch.setattr(module, "get_sessionmaker", forbidden, raising=False)

    assert module.main(["--clinic-id", CLINIC_ID]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "claimed": 0,
        "enabled": False,
    }


@pytest.mark.parametrize(
    ("runner", "result_type"),
    [
        (run_cliniko_bookings, "write"),
        (reconcile_cliniko_bookings, "reconcile"),
    ],
)
def test_finite_batch_limit_is_bounded(runner, result_type: str) -> None:
    with pytest.raises(ValueError, match="limit must be between 1 and 50"):
        runner(
            _factory(),
            clinic_id=CLINIC_ID,
            worker_id=f"pr07-{result_type}-limit",
            client=_client(_ScenarioTransport()),
            programme_gate=_allow_pilot,
            now=NOW,
            enabled=True,
            limit=51,
        )


@pytest.mark.parametrize(
    ("module", "switch_name", "runner_name", "result_factory"),
    [
        (
            cliniko_booking_worker,
            "durable_cliniko_write_enabled",
            "run_once",
            lambda: cliniko_booking_worker.ClinikoBookingRunResult(
                enabled=True,
                claimed=1,
            ),
        ),
        (
            cliniko_booking_reconciler,
            "cliniko_booking_reconciliation_enabled",
            "reconcile_once",
            lambda: cliniko_booking_reconciler.ClinikoBookingReconcileResult(
                enabled=True,
                claimed=1,
            ),
        ),
    ],
)
def test_runtime_entrypoint_wires_one_enabled_finite_batch(
    module,
    switch_name: str,
    runner_name: str,
    result_factory,
    monkeypatch,
    capsys,
) -> None:
    class Switches:
        def decision(self, *_args) -> PilotGateDecision:
            return PilotGateDecision(True, "allowed")

    class Transport:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    captured: dict[str, object] = {}

    def runner(session_factory, **kwargs):
        captured["session_factory"] = session_factory
        captured.update(kwargs)
        return result_factory()

    monkeypatch.setattr(module, "_bootstrap_runtime_configuration", lambda: None)
    monkeypatch.setattr(module, switch_name, lambda: True)
    monkeypatch.setattr(module, "durable_booking_confirmation_enabled", lambda: True)
    monkeypatch.setattr(
        module,
        "operational_switch_snapshot_from_environment",
        lambda: Switches(),
    )
    monkeypatch.setattr(module, "get_cliniko_config", lambda: object())
    monkeypatch.setattr(module.httpx, "Client", Transport)
    monkeypatch.setattr(
        module,
        "ClinikoBookingClient",
        lambda _config, *, client: ("client", client),
    )
    monkeypatch.setattr(module, "get_sessionmaker", lambda: "session-factory")
    monkeypatch.setattr(module, "job_gate_for_snapshot", lambda *_args: "pilot-gate")
    monkeypatch.setattr(module, runner_name, runner)

    assert module.main(
        [
            "--clinic-id",
            CLINIC_ID,
            "--worker-id",
            "pr07-runtime-test",
            "--limit",
            "3",
            "--now",
            NOW.isoformat(),
        ]
    ) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["enabled"] is True
    assert summary["claimed"] == 1
    assert captured["session_factory"] == "session-factory"
    assert captured["worker_id"] == "pr07-runtime-test"
    assert captured["limit"] == 3
    assert captured["enabled"] is True
    assert captured["confirmation_release_enabled"] is True
    assert captured["programme_gate"] == "pilot-gate"


def test_signature_reconciliation_requires_zero_match_preflight_evidence() -> None:
    factory = _factory()
    transport = _AcceptedThenLostTransport()
    client = _client(transport)
    run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-evidence-write",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )
    with factory.begin() as session:
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        effect.preflight_evidence_hash = None

    result = reconcile_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-evidence-read",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=1),
        enabled=True,
        confirmation_release_enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        assert result.conflicts == 1
        assert action.write_back_state == BookingWriteBackState.CONFLICT
        assert action.conflict_reason == "preflight_evidence_missing"
        assert session.scalar(select(ExternalEffectHandoff.id)) is not None
        assert session.scalar(
            select(ExternalEffect.id).where(
                ExternalEffect.effect_type == ExternalEffectType.SMS
            )
        ) is None


def test_exact_create_and_read_back_verify_and_release_confirmation_once() -> None:
    factory = _factory()
    transport = _ScenarioTransport()

    result = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-success",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
        confirmation_release_enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        effects = list(session.execute(select(ExternalEffect)).scalars())
        assert result.verified == 1
        assert transport.method_counts == {"GET": 3, "POST": 1, "PATCH": 0}
        assert action.write_back_state == BookingWriteBackState.VERIFIED
        assert action.written_back is True
        assert len([item for item in effects if item.effect_type == ExternalEffectType.SMS]) == 1


def test_operational_stop_before_settlement_prevents_verification_and_confirmation() -> None:
    factory = _factory()
    transport = _ScenarioTransport()
    gate = _StopBeforeSettlementGate()

    result = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-stop-before-settlement",
        client=_client(transport),
        programme_gate=gate,
        now=NOW,
        enabled=True,
        confirmation_release_enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        booking_effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        confirmation_count = session.scalar(
            select(func.count())
            .select_from(ExternalEffect)
            .where(ExternalEffect.effect_type == ExternalEffectType.SMS)
        )
        assert gate.calls == 3
        assert result.reconcile_required == 1
        assert transport.method_counts["POST"] == 1
        assert action.write_back_state == BookingWriteBackState.RECONCILE_REQUIRED
        assert action.written_back is False
        assert booking_effect.state == ExternalEffectState.RECONCILE_REQUIRED
        assert confirmation_count == 0


def test_finalizer_is_idempotent_for_same_exact_evidence() -> None:
    factory = _factory()
    transport = _ScenarioTransport()
    run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-finalizer-idempotent",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
        confirmation_release_enabled=True,
    )
    with factory() as session:
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        context = load_dispatch_context(
            session,
            clinic_id=CLINIC_ID,
            effect_id=effect.id,
            now=NOW,
            programme_gate=_allow_pilot,
            identity_service=_identity_service(),
        )
    observed = ObservedAppointment(
        provider_id=PROVIDER_APPOINTMENT_ID,
        signature=context.expected,
        active=True,
        updated_at=NOW,
    )

    with factory.begin() as session:
        changed = finalize_verified(
            session,
            clinic_id=CLINIC_ID,
            context=context,
            observed=observed,
            now=NOW + timedelta(seconds=1),
            programme_gate=_allow_pilot,
            confirmation_release_enabled=True,
            identity_service=_identity_service(),
        )

    with factory() as session:
        assert changed is False
        assert session.scalar(
            select(func.count())
            .select_from(ExternalEffect)
            .where(ExternalEffect.effect_type == ExternalEffectType.SMS)
        ) == 1


def test_frozen_subject_cannot_finalize_or_release_confirmation(monkeypatch) -> None:
    factory = _factory()
    with factory.begin() as session:
        effect = claim_effects(
            session,
            clinic_id=CLINIC_ID,
            worker_id="pr07-frozen-finalizer",
            now=NOW,
            lease_for=timedelta(minutes=5),
            effect_types=(ExternalEffectType.CLINIKO_BOOKING,),
        )[0]
        mark_dispatching(
            session,
            clinic_id=CLINIC_ID,
            effect_id=effect.id,
            worker_id="pr07-frozen-finalizer",
            now=NOW,
        )
        action = session.execute(select(BookingAction)).scalar_one()
        action.write_back_state = BookingWriteBackState.DISPATCHING
        action.provider_attempted_at = NOW
        effect.preflight_evidence_hash = preflight_zero_match_hash(
            effect.request_hash
        )
        context = load_dispatch_context(
            session,
            clinic_id=CLINIC_ID,
            effect_id=effect.id,
            now=NOW,
            programme_gate=_allow_pilot,
            identity_service=_identity_service(),
        )
    observed = ObservedAppointment(
        provider_id=PROVIDER_APPOINTMENT_ID,
        signature=context.expected,
        active=True,
        updated_at=NOW,
    )

    def frozen(*_args, **_kwargs) -> None:
        raise SubjectFrozenError()

    monkeypatch.setattr(
        "src.clinic_recall.durable.cliniko_booking_state.assert_patient_writable",
        frozen,
    )
    with factory.begin() as session, pytest.raises(SubjectFrozenError):
        finalize_verified(
            session,
            clinic_id=CLINIC_ID,
            context=context,
            observed=observed,
            now=NOW,
            programme_gate=_allow_pilot,
            confirmation_release_enabled=True,
            identity_service=_identity_service(),
        )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        assert action.write_back_state == BookingWriteBackState.DISPATCHING
        assert action.written_back is False
        assert session.scalar(
            select(func.count())
            .select_from(ExternalEffect)
            .where(ExternalEffect.effect_type == ExternalEffectType.SMS)
        ) == 0


def test_changed_job_appointment_cannot_finalize_or_release_confirmation() -> None:
    factory = _factory()
    with factory.begin() as session:
        effect = claim_effects(
            session,
            clinic_id=CLINIC_ID,
            worker_id="pr07-changed-appointment-finalizer",
            now=NOW,
            lease_for=timedelta(minutes=5),
            effect_types=(ExternalEffectType.CLINIKO_BOOKING,),
        )[0]
        mark_dispatching(
            session,
            clinic_id=CLINIC_ID,
            effect_id=effect.id,
            worker_id="pr07-changed-appointment-finalizer",
            now=NOW,
        )
        action = session.execute(select(BookingAction)).scalar_one()
        action.write_back_state = BookingWriteBackState.DISPATCHING
        action.provider_attempted_at = NOW
        effect.preflight_evidence_hash = preflight_zero_match_hash(
            effect.request_hash
        )
        context = load_dispatch_context(
            session,
            clinic_id=CLINIC_ID,
            effect_id=effect.id,
            now=NOW,
            programme_gate=_allow_pilot,
            identity_service=_identity_service(),
        )
    with factory.begin() as session:
        session.add(
            Appointment(
                id="appointment-pr07-changed",
                clinic_id=CLINIC_ID,
                patient_id=PATIENT_ID,
                source_ref="910700002",
                status="missed",
                start_at=NOW - timedelta(days=2),
            )
        )
        job = session.get(OutreachJob, "job-pr07")
        assert job is not None
        job.appointment_id = "appointment-pr07-changed"
    observed = ObservedAppointment(
        provider_id=PROVIDER_APPOINTMENT_ID,
        signature=context.expected,
        active=True,
        updated_at=NOW,
    )

    with factory.begin() as session, pytest.raises(
        ValueError,
        match="verification_context_changed",
    ):
        finalize_verified(
            session,
            clinic_id=CLINIC_ID,
            context=context,
            observed=observed,
            now=NOW,
            programme_gate=_allow_pilot,
            confirmation_release_enabled=True,
            identity_service=_identity_service(),
        )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        assert action.write_back_state == BookingWriteBackState.DISPATCHING
        assert action.written_back is False
        assert session.scalar(
            select(func.count())
            .select_from(ExternalEffect)
            .where(ExternalEffect.effect_type == ExternalEffectType.SMS)
        ) == 0


def test_finalizer_quarantines_different_provider_identity_and_cancels_confirmation() -> None:
    factory = _factory()
    run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-finalizer-conflict",
        client=_client(_ScenarioTransport()),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
        confirmation_release_enabled=True,
    )
    with factory() as session:
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        context = load_dispatch_context(
            session,
            clinic_id=CLINIC_ID,
            effect_id=effect.id,
            now=NOW,
            programme_gate=_allow_pilot,
            identity_service=_identity_service(),
        )
    conflicting = ObservedAppointment(
        provider_id="950700002",
        signature=context.expected,
        active=True,
        updated_at=NOW + timedelta(seconds=1),
    )

    with factory.begin() as session:
        changed = finalize_verified(
            session,
            clinic_id=CLINIC_ID,
            context=context,
            observed=conflicting,
            now=NOW + timedelta(seconds=1),
            programme_gate=_allow_pilot,
            confirmation_release_enabled=True,
            identity_service=_identity_service(),
        )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        booking_effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        confirmation = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.SMS
            )
        ).scalar_one()
        assert changed is False
        assert action.write_back_state == BookingWriteBackState.CONFLICT
        assert action.written_back is False
        assert booking_effect.state == ExternalEffectState.RECONCILE_REQUIRED
        assert confirmation.state == ExternalEffectState.CANCELED
        assert session.scalar(select(ExternalEffectHandoff.id)) is not None


def test_all_cliniko_http_runs_outside_database_session_contexts() -> None:
    seeded_factory = _factory()
    tracked_factory = sessionmaker(
        bind=seeded_factory.kw["bind"],
        class_=_TrackingSession,
        expire_on_commit=False,
    )
    _TrackingSession.active_contexts = 0
    transport = _NoDatabaseDuringHttpTransport()

    result = run_cliniko_bookings(
        tracked_factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-no-db-over-http",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )

    assert result.verified == 1
    assert _TrackingSession.active_contexts == 0
    assert transport.method_counts == {"GET": 3, "POST": 1, "PATCH": 0}


@pytest.mark.parametrize("field_name", ["request_hash", "idempotency_key"])
def test_tampered_effect_identity_is_canceled_before_http(field_name: str) -> None:
    factory = _factory()
    with factory.begin() as session:
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        setattr(effect, field_name, "0" * 64)
    transport = _ScenarioTransport()

    result = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-tampered-effect",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        assert result.canceled == 1
        assert transport.method_counts == {"GET": 0, "POST": 0, "PATCH": 0}
        assert action.write_back_state == BookingWriteBackState.REJECTED
        assert effect.state == ExternalEffectState.CANCELED


def test_changed_job_patient_invalidates_booking_intent_before_http() -> None:
    factory = _factory()
    with factory.begin() as session:
        session.add(
            Patient(
                id="patient-pr07-changed",
                clinic_id=CLINIC_ID,
                source_ref="900700002",
                name="Synthetic Changed Patient",
                phone="+447700900702",
                consent_flags={"call": True, "sms": True},
                opt_out_flags={},
            )
        )
        job = session.get(OutreachJob, "job-pr07")
        assert job is not None
        job.patient_id = "patient-pr07-changed"
    transport = _ScenarioTransport()

    result = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-changed-job-patient",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        assert result.canceled == 1
        assert transport.method_counts == {"GET": 0, "POST": 0, "PATCH": 0}
        assert action.write_back_state == BookingWriteBackState.REJECTED
        assert effect.state == ExternalEffectState.CANCELED


def test_missing_authoritative_slot_conflicts_before_write() -> None:
    factory = _factory()
    transport = _ScenarioTransport(slot_available=False)

    result = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-slot-missing",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        assert result.conflicts == 1
        assert transport.method_counts == {"GET": 1, "POST": 0, "PATCH": 0}
        assert action.write_back_state == BookingWriteBackState.CONFLICT
        assert action.conflict_reason == "slot_no_longer_available"
        assert effect.provider_status == "not_dispatched"


def test_preflight_server_failure_retries_read_without_write_or_attempt_timestamp() -> None:
    factory = _factory()
    transport = _ScenarioTransport(preflight_status=503)

    result = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-preflight-5xx",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        effect = session.execute(select(ExternalEffect)).scalar_one()
        assert result.retried == 1
        assert transport.method_counts["POST"] == 0
        assert action.write_back_state == BookingWriteBackState.PENDING
        assert action.provider_attempted_at is None
        assert effect.state == ExternalEffectState.PENDING
        assert _as_utc(effect.available_at) > NOW


def test_preflight_rate_limit_waits_until_documented_reset_without_write() -> None:
    factory = _factory()
    transport = _ScenarioTransport(preflight_status=429)

    result = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-preflight-rate",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        effect = session.execute(select(ExternalEffect)).scalar_one()
        assert result.retried == 1
        assert transport.method_counts["POST"] == 0
        assert action.provider_attempted_at is None
        assert effect.state == ExternalEffectState.PENDING
        assert _as_utc(effect.available_at).timestamp() >= 1784981100


def test_preflight_read_exhaustion_rejects_with_one_handoff_and_zero_writes() -> None:
    factory = _factory()
    transport = _ScenarioTransport(preflight_status=503)
    client = _client(transport)
    results = [
        run_cliniko_bookings(
            factory,
            clinic_id=CLINIC_ID,
            worker_id=f"pr07-preflight-exhaust-{index}",
            client=client,
            programme_gate=_allow_pilot,
            now=NOW + timedelta(seconds=31 * index),
            enabled=True,
        )
        for index in range(4)
    ]

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        handoffs = list(session.execute(select(ExternalEffectHandoff)).scalars())
        assert [result.retried for result in results] == [1, 1, 1, 0]
        assert results[-1].rejected == 1
        assert results[-1].dead_lettered == 1
        assert transport.method_counts["POST"] == 0
        assert action.write_back_state == BookingWriteBackState.REJECTED
        assert effect.state == ExternalEffectState.DEAD_LETTER
        assert effect.read_attempt_count == 4
        assert len(handoffs) == 1


def test_documented_validation_rejection_is_terminal_with_one_handoff() -> None:
    factory = _factory()
    transport = _ScenarioTransport(create_status=422)

    first = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-rejected",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )
    second = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-rejected-rerun",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW + timedelta(minutes=1),
        enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        handoffs = list(session.execute(select(ExternalEffectHandoff)).scalars())
        assert first.rejected == 1
        assert second.claimed == 0
        assert transport.method_counts["POST"] == 1
        assert action.write_back_state == BookingWriteBackState.REJECTED
        assert effect.state == ExternalEffectState.REJECTED
        assert len(handoffs) == 1
        assert "private" not in repr(effect.__dict__).lower()


def test_rate_limit_persists_reset_and_retries_only_after_reset() -> None:
    factory = _factory()
    transport = _ScenarioTransport(create_status=429)
    client = _client(transport)

    first = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-rate",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )
    early = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-rate-early",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=30),
        enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        effect = session.execute(select(ExternalEffect)).scalar_one()
        assert first.retried == 1
        assert early.claimed == 0
        assert action.write_back_state == BookingWriteBackState.PENDING
        assert action.provider_attempted_at == NOW
        assert effect.state == ExternalEffectState.PENDING
        assert _as_utc(effect.available_at) > NOW + timedelta(seconds=30)
        assert transport.method_counts["POST"] == 1


def test_rate_limit_exhaustion_rejects_with_one_handoff_and_no_further_write() -> None:
    factory = _factory()
    transport = _ScenarioTransport(create_status=429)
    client = _client(transport)

    first = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-rate-exhaust-1",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )
    second = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-rate-exhaust-2",
        client=client,
        programme_gate=_allow_pilot,
        now=datetime.fromtimestamp(1784981101, tz=UTC),
        enabled=True,
    )
    third = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-rate-exhaust-3",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW + timedelta(hours=1),
        enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        handoffs = list(session.execute(select(ExternalEffectHandoff)).scalars())
        assert first.retried == 1
        assert second.rejected == 1
        assert second.dead_lettered == 1
        assert third.claimed == 0
        assert transport.method_counts["POST"] == 2
        assert action.write_back_state == BookingWriteBackState.REJECTED
        assert effect.state == ExternalEffectState.DEAD_LETTER
        assert len(handoffs) == 1


def test_post_write_server_error_is_ambiguous_and_never_replayed() -> None:
    factory = _factory()
    transport = _ScenarioTransport(create_status=503)
    client = _client(transport)

    first = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-post-5xx",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )
    second = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-post-5xx-rerun",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW + timedelta(minutes=1),
        enabled=True,
    )

    assert first.reconcile_required == 1
    assert second.claimed == 0
    assert transport.method_counts["POST"] == 1


def test_known_create_id_uses_exact_get_during_reconciliation() -> None:
    factory = _factory()
    write_transport = _ScenarioTransport(read_status=503)

    written = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-known-id-write",
        client=_client(write_transport),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )

    with factory() as session:
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        assert written.reconcile_required == 1
        assert effect.provider_resource_id == PROVIDER_APPOINTMENT_ID
        assert effect.state == ExternalEffectState.RECONCILE_REQUIRED

    read_payload = _fixture("create_response.json")
    read_transport = _ExactGetTransport(read_payload)
    reconciled = reconcile_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-known-id-read",
        client=_client(read_transport),
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=1),
        enabled=True,
    )

    assert reconciled.verified == 1
    assert read_transport.method_counts == {"GET": 1, "POST": 0, "PATCH": 0}


def test_known_create_id_third_state_conflicts_without_write() -> None:
    factory = _factory()
    run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-known-conflict-write",
        client=_client(_ScenarioTransport(read_status=503)),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )
    changed = _fixture("create_response.json")
    changed["starts_at"] = "2026-08-04T09:05:00Z"
    transport = _ExactGetTransport(changed)

    result = reconcile_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-known-conflict-read",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=1),
        enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        assert result.conflicts == 1
        assert action.write_back_state == BookingWriteBackState.CONFLICT
        assert action.conflict_reason == "known_id_state_mismatch"
        assert session.scalar(select(ExternalEffectHandoff.id)) is not None
        assert transport.method_counts == {"GET": 1, "POST": 0, "PATCH": 0}


def test_exact_read_back_mismatch_conflicts_and_hands_off() -> None:
    factory = _factory()
    mismatch = _fixture("create_response.json")
    mismatch["practitioner"]["links"]["self"] = (
        "https://api.uk2.cliniko.com/v1/practitioners/930700002"
    )
    transport = _ScenarioTransport(read_payload=mismatch)

    result = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-read-mismatch",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        handoffs = list(session.execute(select(ExternalEffectHandoff)).scalars())
        assert result.conflicts == 1
        assert action.write_back_state == BookingWriteBackState.CONFLICT
        assert action.written_back is False
        assert effect.state == ExternalEffectState.RECONCILE_REQUIRED
        assert len(handoffs) == 1

    later = reconcile_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-read-mismatch-after-handoff",
        client=_client(_ExactGetTransport(_fixture("create_response.json"))),
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=1),
        enabled=True,
        confirmation_release_enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        assert later.claimed == 0
        assert action.write_back_state == BookingWriteBackState.CONFLICT
        assert action.written_back is False
        assert session.scalar(
            select(func.count())
            .select_from(ExternalEffect)
            .where(ExternalEffect.effect_type == ExternalEffectType.SMS)
        ) == 0


def test_zero_match_reconciliation_uses_persisted_schedule_then_hands_off_once() -> None:
    factory = _factory()
    write_transport = _ScenarioTransport(create_status=503)
    run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-zero-write",
        client=_client(write_transport),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )
    read_transport = _StaticListTransport(_fixture("reconciliation_zero.json"))
    client = _client(read_transport)

    first = reconcile_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-zero-1",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=1),
        enabled=True,
    )
    early = reconcile_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-zero-early",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=20),
        enabled=True,
    )
    second = reconcile_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-zero-2",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=31),
        enabled=True,
    )
    third = reconcile_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-zero-3",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=121),
        enabled=True,
    )
    fourth = reconcile_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-zero-4",
        client=client,
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=301),
        enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        handoffs = list(session.execute(select(ExternalEffectHandoff)).scalars())
        assert first.unresolved == 1
        assert early.claimed == 0
        assert second.unresolved == 1
        assert third.unresolved == 1
        assert fourth.exhausted == 1
        assert effect.read_attempt_count == 4
        assert effect.state == ExternalEffectState.RECONCILE_REQUIRED
        assert action.write_back_state == BookingWriteBackState.RECONCILE_REQUIRED
        assert len(handoffs) == 1
        assert read_transport.method_counts == {"GET": 4, "POST": 0, "PATCH": 0}


def test_reconciliation_rate_limit_waits_until_documented_reset() -> None:
    factory = _factory()
    run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-reconcile-rate-write",
        client=_client(_ScenarioTransport(create_status=503)),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )
    transport = _StaticListTransport({}, status=429)

    result = reconcile_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-reconcile-rate-read",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=1),
        enabled=True,
    )

    with factory() as session:
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        assert result.unresolved == 1
        assert transport.method_counts == {"GET": 1, "POST": 0, "PATCH": 0}
        assert _as_utc(effect.available_at).timestamp() >= 1784981100


def test_multiple_exact_reconciliation_matches_conflict_without_selection() -> None:
    factory = _factory()
    run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-multiple-write",
        client=_client(_ScenarioTransport(create_status=503)),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )
    transport = _StaticListTransport(_fixture("reconciliation_multiple.json"))

    result = reconcile_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-multiple-read",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=1),
        enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        effect = session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalar_one()
        assert result.conflicts == 1
        assert action.write_back_state == BookingWriteBackState.CONFLICT
        assert action.external_appointment_ref is None
        assert effect.state == ExternalEffectState.RECONCILE_REQUIRED
        assert session.scalar(select(ExternalEffectHandoff.id)) is not None


def test_expired_reconciliation_lease_is_recovered_read_only() -> None:
    factory = _factory()
    run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-stale-write",
        client=_client(_ScenarioTransport(create_status=503)),
        programme_gate=_allow_pilot,
        now=NOW,
        enabled=True,
    )
    with factory.begin() as session:
        effect = session.execute(select(ExternalEffect)).scalar_one()
        effect.lease_owner = "dead-reconciler"
        effect.lease_expires_at = NOW

    transport = _StaticListTransport(_fixture("reconciliation_zero.json"))
    result = reconcile_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="pr07-stale-read",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=1),
        enabled=True,
    )

    assert result.claimed == 1
    assert transport.method_counts["POST"] == 0


def test_expired_dispatch_is_projected_to_action_without_provider_replay() -> None:
    factory = _factory()
    with factory.begin() as session:
        effect = session.execute(select(ExternalEffect)).scalar_one()
        claimed = claim_effects(
            session,
            clinic_id=CLINIC_ID,
            worker_id="dead-write-worker",
            now=NOW,
            lease_for=timedelta(seconds=1),
            effect_types=(ExternalEffectType.CLINIKO_BOOKING,),
        )
        assert [item.id for item in claimed] == [effect.id]
        mark_dispatching(
            session,
            clinic_id=CLINIC_ID,
            effect_id=effect.id,
            worker_id="dead-write-worker",
            now=NOW,
        )
        action = session.execute(select(BookingAction)).scalar_one()
        action.write_back_state = BookingWriteBackState.DISPATCHING
        action.provider_attempted_at = NOW

    transport = _ScenarioTransport()
    result = run_cliniko_bookings(
        factory,
        clinic_id=CLINIC_ID,
        worker_id="recovery-write-worker",
        client=_client(transport),
        programme_gate=_allow_pilot,
        now=NOW + timedelta(seconds=2),
        enabled=True,
    )

    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        effect = session.execute(select(ExternalEffect)).scalar_one()
        assert result.recovered_dispatches == 1
        assert effect.state == ExternalEffectState.RECONCILE_REQUIRED
        assert action.write_back_state == BookingWriteBackState.RECONCILE_REQUIRED
        assert transport.method_counts == {"GET": 0, "POST": 0, "PATCH": 0}


def test_twilio_callback_cannot_settle_cliniko_booking_effect() -> None:
    factory = _factory()
    with factory() as session:
        effect = session.execute(select(ExternalEffect)).scalar_one()
        token = effect.callback_token

    with factory.begin() as session, pytest.raises(CallbackCorrelationError):
        receive_twilio_callback(
            session,
            effect_token=token,
            callback_kind=ProviderCallbackKind.SMS,
            fields={
                "MessageSid": "SM" + "1" * 32,
                "MessageStatus": "delivered",
            },
            raw_payload=b"synthetic",
            received_at=NOW,
        )

    with factory() as session:
        effect = session.execute(select(ExternalEffect)).scalar_one()
        assert effect.state == ExternalEffectState.PENDING