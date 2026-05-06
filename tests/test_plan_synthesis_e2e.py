"""End-to-end synthesis test — calls the live fleet.

Marked llm_eval; skipped without ANTHROPIC_API_KEY. The Wave 2 gate
requires this test to PASS at least once before promoting to Wave 3.

Cost: ~$5-8 per run (T3 depth, full fleet). Run sparingly.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


@pytest.mark.llm_eval
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
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
