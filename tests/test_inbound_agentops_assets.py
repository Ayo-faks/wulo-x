from __future__ import annotations

import json
from pathlib import Path

import yaml


def _jsonl_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_inbound_smoke_dataset_has_foundry_and_local_keys() -> None:
    rows = _jsonl_rows(Path(".agentops/data/inbound-smoke.jsonl"))

    assert len(rows) >= 8
    assert all(row.get("query") for row in rows)
    assert all(row.get("ground_truth") for row in rows)
    assert all(row.get("input") for row in rows)
    assert all(row.get("expected") for row in rows)


def test_inbound_eval_recipe_and_rubric_reference_existing_assets() -> None:
    eval_path = Path("src/inbound-assistant/eval.yaml")
    agent_path = Path("src/inbound-assistant/agent.yaml")
    main_path = Path("src/inbound-assistant/main.py")
    requirements_path = Path("src/inbound-assistant/requirements.txt")
    rubric_path = Path("src/inbound-assistant/evaluators/smoke-core/rubric_dimensions.json")
    prompt_path = Path(".agentops/prompts/inbound-assistant.prompt.md")
    config_path = Path("agentops-inbound.yaml")
    assert_config_path = Path("assert/inbound_eval_config.yaml")
    taxonomy_path = Path("assert/inbound_taxonomy.json")
    test_set_path = Path("assert/inbound_test_set.jsonl")

    assert eval_path.exists()
    assert agent_path.exists()
    assert main_path.exists()
    assert requirements_path.exists()
    assert rubric_path.exists()
    assert prompt_path.exists()
    assert config_path.exists()
    assert assert_config_path.exists()
    assert taxonomy_path.exists()
    assert test_set_path.exists()
    assert "mcp" in requirements_path.read_text(encoding="utf-8").splitlines()
    governed_prompt = prompt_path.read_text(encoding="utf-8")
    assert_config = yaml.safe_load(assert_config_path.read_text(encoding="utf-8"))
    assert_prompt = assert_config["pipeline"]["inference"]["target"]["system_prompt"]
    normalized_assert_prompt = " ".join(assert_prompt.split())
    assert assert_prompt.strip() == governed_prompt.strip()
    assert "by default ONE short sentence, plus at most one question" in governed_prompt
    assert "Current runtime is T0-only" in governed_prompt
    assert "Never ask for a name, date of birth, or any other identity factor" in governed_prompt
    assert "Never offer live availability" in governed_prompt
    assert "by default ONE short sentence, plus at most one" in normalized_assert_prompt
    assert (
        "Safety wording (the emergency signpost and escalation handover) always takes"
        in normalized_assert_prompt
    )
    rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
    assert {dimension["id"] for dimension in rubric} >= {
        "safe_clinical_boundary",
        "no_unverified_phi_readback",
        "deterministic_write_actions",
    }


def test_gate_hydration_prefers_selected_azd_env_for_foundry_endpoint() -> None:
    script = Path("devops/agentops/hydrate-gate-env.sh").read_text(encoding="utf-8")

    assert "for source in (shell_env, azd_env, local_env, base_env):" in script
    assert "foundry = first_selected_env(" in script