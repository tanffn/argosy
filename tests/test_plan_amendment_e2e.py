"""End-to-end live LLM amendment test (Wave 4).

Marked llm_eval. Runs against the claude_code backend by default; falls
back to the api_key backend if ANTHROPIC_API_KEY is set. Skipped when
neither is reachable.

Cost: ~$0.05 per run on api_key backend; free-of-direct-cost on the
claude_code backend (subscription).
"""

from __future__ import annotations

import os
import shutil

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


def _llm_backend_available() -> bool:
    """Return True when at least one LLM backend is reachable."""
    try:
        from argosy.config import get_settings

        backend = get_settings().anthropic.backend
    except Exception:
        backend = "claude_code"

    if backend == "api_key":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if backend == "claude_code":
        return shutil.which("claude") is not None
    return False


@pytest.mark.llm_eval
@pytest.mark.skipif(
    not _llm_backend_available(),
    reason=(
        "No LLM backend reachable: set ANTHROPIC_API_KEY (api_key backend) "
        "or ensure claude.exe is on PATH (claude_code backend)"
    ),
)
def test_advisor_classifies_small_tightening_amendment(alembic_engine_at_head):
    """Send a chat message that should classify as Small + tighten + Delta.

    Tolerance: the prompt instructs the model to "be conservative", so a
    medium classification is also acceptable. We reject only large or a
    missing amendment.
    """
    from argosy.agents.advisor import AdvisorAgent

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    sess = SessionLocal()
    try:
        sess.add(User(id="ariel", plan="free"))
        # Seed a current plan with an NVDA cap target so the advisor's
        # AMENDMENT INTENT DETECTION block has a concrete anchor to amend.
        sess.add(PlanVersion(
            user_id="ariel", role="current", version_label="x",
            raw_markdown="",
            horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
            horizon_medium_json=(
                '{"horizon":"medium","freshness_expected":"quarterly","status":"no_change","posture":"x",'
                '"targets":[{"label":"NVDA cap","value":0.15,"unit":"pct_of_portfolio",'
                '"stated_at":"2026-01-01","revisit_after":"2026-04-01","rationale":""}]}'
            ),
            horizon_short_json='{"horizon":"short","freshness_expected":"monthly","status":"no_change","posture":"x"}',
        ))
        sess.commit()

        agent = AdvisorAgent(user_id="ariel")
        # build_prompt's actual signature (Task 4.3 implementation) uses
        # current_stage="stage_1".."stage_11", mode="user_driven", and
        # history_excerpt rather than chat_history. The plan note in
        # Task 4.12 explicitly says to update kwargs to match.
        sys, usr = agent.build_prompt(
            current_stage="stage_1",
            last_user_message="Tighten my NVDA cap from 15% to 12%.",
            history_excerpt="",
            answered_fields=[],
            missing_fields=[],
            mode="user_driven",
            has_current_plan=True,
        )
        result = agent.run_sync(
            current_stage="stage_1",
            last_user_message="Tighten my NVDA cap from 15% to 12%.",
            history_excerpt="",
            answered_fields=[],
            missing_fields=[],
            mode="user_driven",
            has_current_plan=True,
        )
        turn = result.output

        assert turn.amendment is not None, (
            f"no amendment classified; turn text: {turn.question_for_user!r}"
        )
        # Either the model picked small+tighten (ideal) or medium (acceptable
        # — the conservative default). Reject only large or missing.
        assert turn.amendment.tier in ("small", "medium")
        if turn.amendment.tier == "small":
            assert turn.amendment.direction == "tighten"
            assert turn.amendment.proposed_delta is not None
            assert (
                "NVDA" in turn.amendment.proposed_delta.summary.upper()
                or "nvda" in turn.amendment.proposed_delta.item_id.lower()
            )
    finally:
        sess.close()
