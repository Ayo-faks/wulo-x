from pathlib import Path

import pytest
from apps.artagent.backend.registries.agentstore.loader import discover_agents
from apps.artagent.backend.registries.toolstore.registry import (
    execute_tool,
    initialize_tools,
    list_tools,
    reset_registry,
)


def test_recall_agent_loads_governed_prompt() -> None:
    agents = discover_agents()

    recall_agent = agents["RecallAgent"]
    governed_prompt = Path(".agentops/prompts/recall-agent.prompt.md").read_text(encoding="utf-8")
    expected_tools = {
        "get_clinic_faq",
        "get_availability",
        "book_slot",
        "reschedule",
        "send_sms",
        "send_email",
        "escalate_to_staff",
        "record_opt_out",
        "log_outcome",
    }

    assert recall_agent.prompt_template == governed_prompt
    assert expected_tools.issubset(set(recall_agent.tool_names))
    assert "Current voice runtime is T0-only" in governed_prompt
    assert "- ask for a name, date of birth, or any other identity factor" in governed_prompt
    assert "- disclose that a patient or appointment exists" in governed_prompt

    agent_yaml = Path(
        "apps/artagent/backend/registries/agentstore/recall_agent/agent.yaml"
    ).read_text(encoding="utf-8")
    assert (
        "Hello, this is Clinic Recall calling on behalf of your clinic. "
        "Is now a good time to talk?"
    ) in agent_yaml
    assert "about an appointment" not in agent_yaml


def test_inbound_clinic_agent_loads_governed_prompt() -> None:
    agents = discover_agents()

    inbound_agent = agents["InboundClinicAgent"]
    governed_prompt = Path(".agentops/prompts/inbound-assistant.prompt.md").read_text(encoding="utf-8")
    expected_tools = {
        "get_clinic_hours",
        "get_clinic_services",
        "request_callback",
        "record_inbound_opt_out",
        "escalate_inbound_to_staff",
        "log_inbound_call_outcome",
    }

    assert inbound_agent.prompt_template == governed_prompt
    assert "by default ONE short sentence, plus at most one question" in governed_prompt
    assert "Safety wording (the emergency signpost and escalation handover) always takes priority" in governed_prompt
    assert expected_tools.issubset(set(inbound_agent.tool_names))
    assert {
        "find_possible_patient_match",
        "get_available_slots",
        "create_inbound_booking_request",
        "record_consent_decision",
    }.isdisjoint(set(inbound_agent.tool_names))
    assert "Current runtime is T0-only" in governed_prompt
    assert "Never ask for a name, date of birth, or any other identity factor" in governed_prompt


@pytest.mark.asyncio
async def test_log_outcome_tool_registered_and_executes() -> None:
    reset_registry()
    initialize_tools()

    assert "log_outcome" in list_tools(tags={"clinic_recall"})

    result = await execute_tool(
        "log_outcome",
        {"session_id": "call-test", "outcome": "test", "summary": "Phase 0 proof."},
    )

    assert result["success"] is True
    assert result["event"]["outcome"] == "test"


def test_rebooking_scenario_loads_recall_agent_and_tools() -> None:
    from apps.artagent.backend.registries.scenariostore.loader import load_scenario

    scenario = load_scenario("rebooking")

    assert scenario is not None
    assert scenario.start_agent == "RecallAgent"
    assert scenario.agents == ["RecallAgent"]
    assert "book_slot" in scenario.tools
    assert "escalate_to_staff" in scenario.tools


def test_inbound_clinic_scenario_loads_front_door_and_recall_specialist() -> None:
    from apps.artagent.backend.registries.scenariostore.loader import load_scenario

    scenario = load_scenario("inbound_clinic")

    assert scenario is not None
    assert scenario.start_agent == "InboundClinicAgent"
    assert scenario.agents == ["InboundClinicAgent", "RecallAgent"]
    assert "request_callback" in scenario.tools
    assert "escalate_inbound_to_staff" in scenario.tools