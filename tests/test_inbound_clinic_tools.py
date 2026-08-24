from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apps.artagent.backend.registries.toolstore import inbound_clinic
from apps.artagent.backend.registries.toolstore.registry import (
    execute_tool,
    get_tool_schema,
    initialize_tools,
    list_tools,
    reset_registry,
)
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.clinic_recall.availability import AvailabilitySlotInput, upsert_availability_slots
from src.clinic_recall.enums import (
    ClinicPhoneProvider,
    InboundStaffTaskKind,
    InboundStaffTaskStatus,
)
from src.clinic_recall.inbound_transport import hash_phone_number_for_clinic
from src.clinic_recall.models import (
    AuditLog,
    Base,
    Clinic,
    InboundCall,
    InboundStaffTask,
    Patient,
)

NOW = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_inbound_call(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        session.add(
            Clinic(
                id="clinic-a",
                name="Clinic A",
                timezone="Europe/London",
                contact_hours={"monday": "09:00-17:00"},
                branding={"services": ["physiotherapy", "sports massage"]},
            )
        )
        session.add(
            Patient(
                id="patient-a",
                clinic_id="clinic-a",
                source_ref="patient-a",
                name="Patient A",
                phone="+15559991111",
                consent_flags={"call": True},
                opt_out_flags={},
            )
        )
        session.add(
            InboundCall(
                id="inbound-call-a",
                clinic_id="clinic-a",
                provider=ClinicPhoneProvider.TWILIO,
                provider_call_id="CA123",
                called_number="+15551230000",
                caller_number_hash=hash_phone_number_for_clinic("+15559991111", "clinic-a"),
            )
        )
        upsert_availability_slots(
            session,
            "clinic-a",
            [
                AvailabilitySlotInput(
                    source_ref="slot-a",
                    start_at=NOW + timedelta(days=1),
                    end_at=NOW + timedelta(days=1, minutes=30),
                    source_provider="cliniko",
                    business_id="920000001",
                    clinician_id="clinician-a",
                    appointment_type_id="940000001",
                    fetched_at=NOW,
                    expires_at=NOW + timedelta(minutes=10),
                )
            ],
            now=NOW,
        )


def _trusted_args() -> dict[str, str]:
    return {
        "_clinic_id": "clinic-a",
        "_call_direction": "inbound",
        "_inbound_call_id": "inbound-call-a",
        "_caller_number_hash": hash_phone_number_for_clinic("+15559991111", "clinic-a") or "",
        "now": NOW.isoformat(),
    }


def test_inbound_tools_register_with_toolstore() -> None:
    reset_registry()
    initialize_tools()

    tools = set(list_tools(tags={"clinic_recall", "inbound"}))
    assert {
        "get_clinic_hours",
        "get_clinic_services",
        "find_possible_patient_match",
        "get_available_slots",
        "create_inbound_booking_request",
        "request_callback",
        "escalate_inbound_to_staff",
        "log_inbound_call_outcome",
        "record_consent_decision",
    }.issubset(tools)
    consent_schema = get_tool_schema("record_consent_decision")
    assert consent_schema is not None
    consent_type = consent_schema["parameters"]["properties"]["consent_type"]
    assert consent_type["enum"] == ["contact"]
    assert consent_schema["parameters"]["required"] == ["consent_type", "granted"]


def _initialize_tools_with_factory(monkeypatch, factory: sessionmaker[Session]) -> None:
    reset_registry()
    initialize_tools()
    monkeypatch.setattr(inbound_clinic, "get_sessionmaker", lambda: factory)


async def test_inbound_model_tool_cannot_record_recording_consent(monkeypatch) -> None:
    factory = _session_factory()
    _seed_inbound_call(factory)
    _initialize_tools_with_factory(monkeypatch, factory)

    result = await execute_tool(
        "record_consent_decision",
        {
            **_trusted_args(),
            "consent_type": "recording",
            "granted": True,
        },
    )

    assert result == {
        "success": False,
        "error": "recording consent is not model-callable",
    }
    with factory() as session:
        call = session.get(InboundCall, "inbound-call-a")
        assert call is not None
        assert "consent" not in (call.provider_metadata or {})


async def test_inbound_tools_return_hours_services_and_slots(monkeypatch) -> None:
    factory = _session_factory()
    _seed_inbound_call(factory)
    _initialize_tools_with_factory(monkeypatch, factory)

    hours = await execute_tool("get_clinic_hours", _trusted_args())
    services = await execute_tool("get_clinic_services", _trusted_args())
    slots = await execute_tool(
        "get_available_slots",
        {
            **_trusted_args(),
            "window_start": NOW.isoformat(),
            "window_end": (NOW + timedelta(days=7)).isoformat(),
        },
    )

    assert hours["success"] is True
    assert hours["contact_hours"] == {"monday": "09:00-17:00"}
    assert services["services"] == ["physiotherapy", "sports massage"]
    assert slots == {
        "success": False,
        "error": "identity_t2_required",
        "slots": [],
    }


async def test_inbound_availability_rejects_model_supplied_practitioner(monkeypatch) -> None:
    factory = _session_factory()
    _seed_inbound_call(factory)
    _initialize_tools_with_factory(monkeypatch, factory)

    result = await execute_tool(
        "get_available_slots",
        {
            **_trusted_args(),
            "window_start": NOW.isoformat(),
            "window_end": (NOW + timedelta(days=7)).isoformat(),
            "clinician_id": "930000001",
        },
    )

    assert result == {"success": False, "error": "clinician_filter_not_allowed"}


async def test_single_phone_match_does_not_grant_patient_reference_authority(
    monkeypatch,
) -> None:
    factory = _session_factory()
    _seed_inbound_call(factory)
    _initialize_tools_with_factory(monkeypatch, factory)

    result = await execute_tool("find_possible_patient_match", _trusted_args())

    assert result == {
        "success": True,
        "status": "staff_verification_required",
    }
    assert "patient_id" not in result
    assert "match_count" not in result
    assert "can_reference_existing_patient" not in result


async def test_inbound_opt_out_remains_available_at_t0_without_patient_exposure(
    monkeypatch,
) -> None:
    factory = _session_factory()
    _seed_inbound_call(factory)
    _initialize_tools_with_factory(monkeypatch, factory)

    result = await execute_tool("record_inbound_opt_out", _trusted_args())

    assert result == {
        "success": True,
        "status": "recorded",
        "identity_review_created": False,
    }
    assert "patient_id" not in result
    with factory() as session:
        patient = session.get(Patient, "patient-a")
        assert patient is not None
        assert patient.opt_out_flags == {"call": True}


async def test_inbound_booking_request_creates_staff_task_not_booking(monkeypatch) -> None:
    factory = _session_factory()
    _seed_inbound_call(factory)
    _initialize_tools_with_factory(monkeypatch, factory)

    result = await execute_tool(
        "create_inbound_booking_request",
        {**_trusted_args(), "requested_service": "physio", "summary": "caller asked to book"},
    )

    assert result["success"] is True
    assert result["kind"] == "identity_unclear"
    with factory() as session:
        task = session.execute(select(InboundStaffTask)).scalar_one()
        assert task.kind == InboundStaffTaskKind.IDENTITY_UNCLEAR
        assert task.status == InboundStaffTaskStatus.OPEN
        assert task.reason == "identity_policy_unavailable"
        assert task.payload == {}
        assert task.summary == "Generic booking request requires staff review."
        assert session.execute(select(func.count()).select_from(AuditLog)).scalar() == 1


async def test_escalate_inbound_to_staff_allows_anonymous_clinical_task(monkeypatch) -> None:
    factory = _session_factory()
    _seed_inbound_call(factory)
    _initialize_tools_with_factory(monkeypatch, factory)

    result = await execute_tool(
        "escalate_inbound_to_staff",
        {**_trusted_args(), "reason": "clinical", "summary": "caller raised a clinical concern"},
    )

    assert result["success"] is True
    assert result["kind"] == "escalation"
    assert result["priority"] == "high"
    with factory() as session:
        task = session.execute(select(InboundStaffTask)).scalar_one()
        assert task.patient_id is None
        assert task.reason == "clinical"


async def test_inbound_tools_reject_non_inbound_context(monkeypatch) -> None:
    factory = _session_factory()
    _seed_inbound_call(factory)
    _initialize_tools_with_factory(monkeypatch, factory)

    result = await execute_tool("request_callback", {"_clinic_id": "clinic-a", "_call_direction": "outbound"})

    assert result["success"] is False
    assert "trusted inbound call context" in result["error"]


async def test_t0_model_tools_discard_identity_booking_time_and_free_text(
    monkeypatch,
) -> None:
    factory = _session_factory()
    _seed_inbound_call(factory)
    _initialize_tools_with_factory(monkeypatch, factory)
    canary = "Avery Example born 1991-04-03 at 10:30 with Dr Canary"

    callback_schema = get_tool_schema("request_callback")
    escalation_schema = get_tool_schema("escalate_inbound_to_staff")
    outcome_schema = get_tool_schema("log_inbound_call_outcome")
    assert callback_schema is not None
    assert callback_schema["parameters"]["properties"] == {}
    assert escalation_schema is not None
    assert set(escalation_schema["parameters"]["properties"]) == {"reason"}
    assert outcome_schema is not None
    assert outcome_schema["parameters"]["properties"] == {
        "outcome": {
            "type": "string",
            "enum": [
                "callback_requested",
                "clinic_info",
                "completed",
                "opt_out",
                "staff_handoff",
            ],
        }
    }

    callback = await execute_tool(
        "request_callback",
        {
            **_trusted_args(),
            "preferred_time": canary,
            "requested_service": canary,
            "summary": canary,
        },
    )
    escalation = await execute_tool(
        "escalate_inbound_to_staff",
        {
            **_trusted_args(),
            "reason": "clinical",
            "priority": "high",
            "slot_id": canary,
            "requested_time": canary,
            "summary": canary,
        },
    )
    outcome = await execute_tool(
        "log_inbound_call_outcome",
        {
            **_trusted_args(),
            "outcome": "staff_handoff",
            "summary": canary,
        },
    )

    assert callback["success"] is True
    assert escalation["success"] is True
    assert outcome["success"] is True
    with factory() as session:
        tasks = list(session.execute(select(InboundStaffTask)).scalars())
        inbound_call = session.get(InboundCall, "inbound-call-a")
        persisted = repr(
            [
                {
                    "kind": task.kind.value,
                    "reason": task.reason,
                    "summary": task.summary,
                    "payload": task.payload,
                }
                for task in tasks
            ]
            + [{"outcome": inbound_call.outcome if inbound_call else None}]
        )
    assert canary not in persisted
    assert all(task.payload == {} for task in tasks)
    assert {task.summary for task in tasks} == {
        "Anonymous callback request requires staff follow-up.",
        "Inbound clinical concern requires staff review.",
    }