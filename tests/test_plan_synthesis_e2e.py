"""End-to-end synthesis test — calls the live fleet.

Marked llm_eval. The Wave 2 gate requires this test to PASS at least
once before promoting to Wave 3.

The test auto-skips when:
  - backend == "api_key" and ANTHROPIC_API_KEY is not set
  - backend == "claude_code" and ``claude.exe`` is not on PATH

Cost: ~$5-8 per run on api_key backend (T3 depth, full fleet);
free-of-direct-cost on claude_code backend (charged to the user's
Claude Code subscription instead). Run sparingly either way.
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
def test_synthesis_e2e_jacobs_baseline(alembic_engine_at_head):
    """Run synthesis end-to-end against a Jacobs-style baseline."""
    from argosy.orchestrator.flows.plan_synthesis import run_synthesis
    from argosy.services.plan_distiller_service import distill_baseline_plan

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    sess = SessionLocal()
    try:
        sess.add(User(id="ariel", plan="free"))
        # Use the same excerpt Wave 1's golden test uses.
        from pathlib import Path
        plan_md = Path("tests/golden/jacobs_plan_excerpt.md").read_text(encoding="utf-8")
        pv = PlanVersion(
            user_id="ariel",
            role="baseline",
            version_label="Jacobs v2.0 (excerpt)",
            raw_markdown=plan_md,
        )
        sess.add(pv)
        sess.commit()

        # Distill the baseline first.
        distill_baseline_plan(sess, plan_version_id=pv.id, user_id="ariel")

        # Run synthesis (full live fleet).
        result = run_synthesis(sess, user_id="ariel", trigger="check_in")

        draft = sess.get(PlanVersion, result.draft_id)
        assert draft is not None
        assert draft.role == "draft"
        assert draft.horizon_long_json
        assert draft.horizon_medium_json
        assert draft.horizon_short_json
        assert draft.synthesis_inputs_json
        assert draft.decision_run_id == result.decision_run_id
    finally:
        sess.close()
