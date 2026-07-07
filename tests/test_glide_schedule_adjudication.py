"""NVDA glide-schedule adjudication team — prompt + divergence unit tests.

No LLM calls: verifies the author/blind-reviewer prompt discipline (the blind
variant never leaks or invites guessing the author's verdict) and the
deterministic divergence comparison.
"""

from __future__ import annotations

from argosy.agents.plan_change_team import (
    GlideScheduleAdjudicatorAgent,
    GlideScheduleVerdict,
    glide_schedule_divergences,
)

FACTS = "position: 11,471 NVDA shares; schedules: 12mo/24mo/30mo"


def _verdict(**over) -> GlideScheduleVerdict:
    base = dict(
        chosen_schedule="12mo",
        horizon_months=12,
        quota_2026_shares=4136,
        quota_2027_shares=5791,
        quota_2028_shares=0,
        rationale="r",
        tradeoff_sentence="t",
        changes_current_glide=False,
    )
    base.update(over)
    return GlideScheduleVerdict(**base)


def test_author_prompt_grounds_in_facts_pack():
    agent = GlideScheduleAdjudicatorAgent(user_id="test")
    system, user = agent.build_prompt(facts_md=FACTS)
    assert "PACE" in system
    assert "Section-102" in system
    assert FACTS in user
    assert "RECONCILIATION" not in user


def test_blind_prompt_is_blind_and_same_facts():
    agent = GlideScheduleAdjudicatorAgent(user_id="test")
    system, user = agent.build_prompt(facts_md=FACTS, blind_rederive=True)
    assert "INDEPENDENT reviewer" in system
    assert "must not guess" in system
    # same raw facts, no author claims anywhere
    assert FACTS in user
    assert "author" not in user.lower()


def test_reconcile_round_carries_reviewer_rederivation():
    agent = GlideScheduleAdjudicatorAgent(user_id="test")
    _, user = agent.build_prompt(
        facts_md=FACTS, reconcile_md="reviewer chose 24mo"
    )
    assert "RECONCILIATION ROUND" in user
    assert "reviewer chose 24mo" in user
    assert "concede" in user and "refute" in user


def test_divergence_agreement_is_empty():
    assert glide_schedule_divergences(_verdict(), _verdict()) == []


def test_divergence_horizon_and_quota():
    author = _verdict()
    reviewer = _verdict(
        chosen_schedule="24mo", horizon_months=24,
        quota_2026_shares=2046, quota_2027_shares=4911,
        quota_2028_shares=2865, changes_current_glide=True,
    )
    out = glide_schedule_divergences(author, reviewer)
    assert any("horizon diverges" in d for d in out)
    assert any("2026 quota diverges" in d for d in out)
    assert any("keep-vs-change diverges" in d for d in out)


def test_divergence_within_tolerance_is_agreement():
    author = _verdict()
    reviewer = _verdict(horizon_months=13, quota_2026_shares=4136 + 400)
    assert glide_schedule_divergences(author, reviewer) == []
