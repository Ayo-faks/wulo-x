"""Drift guard: the ASSERT target prompt must equal the governed prompt.

Phase 0c plumbing fix. assert-ai 0.1.0 cannot include a prompt from a file, so
``pipeline.inference.target.system_prompt`` in ``assert/eval_config.yaml`` embeds
the verbatim content of ``.agentops/prompts/recall-agent.prompt.md`` (the governed
charter). This test fails if the two diverge, so the ASSERT suite always grades
the SHIPPED artifact rather than a stale paraphrase.

If you change the Recall Agent prompt, re-sync the embedded ``system_prompt`` block
in ``assert/eval_config.yaml`` (keep it byte-for-byte identical, modulo surrounding
whitespace) and this test will pass again.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_CONFIG = REPO_ROOT / "assert" / "eval_config.yaml"
GOVERNED_PROMPT = REPO_ROOT / ".agentops" / "prompts" / "recall-agent.prompt.md"


def _embedded_target_system_prompt() -> str:
    data = yaml.safe_load(EVAL_CONFIG.read_text(encoding="utf-8"))
    return data["pipeline"]["inference"]["target"]["system_prompt"]


def test_assert_target_prompt_matches_governed_prompt() -> None:
    embedded = _embedded_target_system_prompt().strip()
    governed = GOVERNED_PROMPT.read_text(encoding="utf-8").strip()
    assert embedded == governed, (
        "ASSERT target system_prompt has drifted from the governed prompt "
        f"({GOVERNED_PROMPT.relative_to(REPO_ROOT)}). Re-sync the embedded "
        "system_prompt block in assert/eval_config.yaml."
    )


def test_recall_prompt_requires_t0_only_identity_and_action_boundary() -> None:
    governed = GOVERNED_PROMPT.read_text(encoding="utf-8")

    for required in (
        "Current voice runtime is T0-only",
        "ask for a name, date of birth, or any other identity factor",
        "disclose that a patient or appointment exists",
        "call `get_availability`, `book_slot`, `reschedule`",
        "identity_t2_required",
        "Ask no identity or scheduling questions",
    ):
        assert required in governed
    assert "confirm you are speaking to the right person" not in governed
    assert "explain you are calling about a missed" not in governed
