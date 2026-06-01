"""Fetcher for the FM plan-revision prompt's PRIOR-RESOLVED CONCERNS block.

Given a session + user_id + current_plan_version_id, return the
list of ``PriorResolvedConcern`` items the orchestrator threads
into ``FundManagerAgent.build_prompt`` so the FM cannot silently
re-raise objections the user already answered.

The fetcher reads from three tables:

  * ``plan_versions``        — find the most recent prior draft
                               (excluding ``current_plan_version_id``)
  * ``fm_objection_user_state`` — pull the user's stances on that
                                  prior draft (skip DEFER)
  * ``agent_reports``        — parse the prior draft's
                               ``fund_manager`` ``response_text`` to
                               recover topic/detail/severity per
                               ``objection_index``

These tests cover the join logic + the "most recent prior" choice
without paying the FM-LLM cost.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from argosy.state.models import (
    AgentReport,
    Base,
    FMObjectionUserState,
    PlanVersion,
    User,
)


def _make_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


def _seed_user(session) -> None:
    session.add(User(id="ariel", plan="free"))
    session.commit()


def _add_draft(session, *, created_at: datetime, label: str) -> int:
    pv = PlanVersion(
        user_id="ariel", role="draft", version_label=label,
        raw_markdown="", imported_at=created_at,
    )
    session.add(pv)
    session.commit()
    session.refresh(pv)
    return pv.id


def _add_fund_manager_report(
    session, *, plan_version_id: int, decision_id: str, reasons: list[str],
    approved: bool = False,
) -> None:
    payload = {"approved": approved, "reasons": reasons, "cited_sources": []}
    session.add(
        AgentReport(
            user_id="ariel",
            agent_role="fund_manager",
            decision_id=decision_id,
            response_text=json.dumps(payload),
            model="claude-opus-4-7",
        )
    )
    session.commit()


def _add_user_state(
    session, *, plan_version_id: int, objection_index: int, stance: str,
    counter_position: str | None = None, topic: str = "t", detail: str = "d",
) -> None:
    from argosy.api.routes.plan_objection_state import _hash_objection_topic

    session.add(
        FMObjectionUserState(
            user_id="ariel",
            plan_version_id=plan_version_id,
            objection_index=objection_index,
            topic_hash=_hash_objection_topic(topic, detail),
            stance=stance,
            counter_position=counter_position,
        )
    )
    session.commit()


# ----------------------------------------------------------------------
# Empty / negative cases
# ----------------------------------------------------------------------


def test_no_prior_draft_returns_empty():
    """User has only the current draft — no prior to carry from."""
    from argosy.services.prior_resolved_concerns import (
        get_prior_resolved_concerns,
    )

    session = _make_session()
    _seed_user(session)
    current_id = _add_draft(
        session,
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        label="synth-current",
    )
    result = get_prior_resolved_concerns(
        session, user_id="ariel", current_plan_version_id=current_id
    )
    assert result == []


def test_prior_draft_without_user_state_returns_empty():
    """Prior draft exists but the user never marked any stance on it."""
    from argosy.services.prior_resolved_concerns import (
        get_prior_resolved_concerns,
    )

    session = _make_session()
    _seed_user(session)
    prior_id = _add_draft(
        session,
        created_at=datetime(2026, 5, 30, tzinfo=timezone.utc),
        label="synth-prior",
    )
    _add_fund_manager_report(
        session,
        plan_version_id=prior_id,
        decision_id="plan-synth-prior",
        reasons=["topic A — detail A"],
    )
    current_id = _add_draft(
        session,
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        label="synth-current",
    )
    result = get_prior_resolved_concerns(
        session, user_id="ariel", current_plan_version_id=current_id
    )
    assert result == []


def test_only_defer_stances_returns_empty():
    """User marked everything DEFER on the prior — nothing to carry."""
    from argosy.services.prior_resolved_concerns import (
        get_prior_resolved_concerns,
    )

    session = _make_session()
    _seed_user(session)
    prior_id = _add_draft(
        session,
        created_at=datetime(2026, 5, 30, tzinfo=timezone.utc),
        label="synth-prior",
    )
    _add_fund_manager_report(
        session,
        plan_version_id=prior_id,
        decision_id=f"plan-synth-{prior_id}",
        reasons=["topic A — detail A", "topic B — detail B"],
    )
    for i in range(2):
        _add_user_state(
            session,
            plan_version_id=prior_id,
            objection_index=i,
            stance="DEFER",
        )
    current_id = _add_draft(
        session,
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        label="synth-current",
    )
    result = get_prior_resolved_concerns(
        session, user_id="ariel", current_plan_version_id=current_id
    )
    assert result == []


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


def test_mixed_stances_returns_only_agree_and_disagree():
    """AGREE + DISAGREE are carried; DEFER is filtered out."""
    from argosy.agents.fund_manager import PriorResolvedConcern
    from argosy.services.prior_resolved_concerns import (
        get_prior_resolved_concerns,
    )

    session = _make_session()
    _seed_user(session)
    prior_id = _add_draft(
        session,
        created_at=datetime(2026, 5, 30, tzinfo=timezone.utc),
        label="synth-prior",
    )
    _add_fund_manager_report(
        session,
        plan_version_id=prior_id,
        decision_id=f"plan-synth-{prior_id}",
        reasons=[
            "BLOCKER — NVDA cap breach: position at 64.9%, cap is 55%.",
            "AMBER — tax substrate ambiguity in Section 102 sequencing.",
            "YELLOW — small concern that doesn't matter.",
        ],
    )
    _add_user_state(
        session, plan_version_id=prior_id, objection_index=0,
        stance="AGREE",
        counter_position="Push tranche to 2026-06-17 per estate gate.",
    )
    _add_user_state(
        session, plan_version_id=prior_id, objection_index=1,
        stance="DISAGREE",
        counter_position="Tax-loss-harvest defer to Q4 instead.",
    )
    _add_user_state(
        session, plan_version_id=prior_id, objection_index=2,
        stance="DEFER",
    )
    current_id = _add_draft(
        session,
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        label="synth-current",
    )

    result = get_prior_resolved_concerns(
        session, user_id="ariel", current_plan_version_id=current_id
    )

    assert len(result) == 2
    by_stance = {c.stance: c for c in result}
    assert "AGREE" in by_stance
    assert "DISAGREE" in by_stance
    assert "DEFER" not in by_stance

    agreed = by_stance["AGREE"]
    assert isinstance(agreed, PriorResolvedConcern)
    assert "NVDA cap breach" in agreed.topic + agreed.detail
    assert agreed.counter_position == "Push tranche to 2026-06-17 per estate gate."

    disagreed = by_stance["DISAGREE"]
    assert "tax substrate" in disagreed.topic + disagreed.detail
    assert disagreed.counter_position == "Tax-loss-harvest defer to Q4 instead."


# ----------------------------------------------------------------------
# Multiple prior drafts — must pick the most recent
# ----------------------------------------------------------------------


def test_most_recent_prior_draft_wins():
    """Two prior drafts both have resolved stances. The fetcher must
    pull from the MOST RECENT prior (excluding the current draft)."""
    from argosy.services.prior_resolved_concerns import (
        get_prior_resolved_concerns,
    )

    session = _make_session()
    _seed_user(session)

    # Older prior — should be ignored.
    older_id = _add_draft(
        session,
        created_at=datetime(2026, 5, 25, tzinfo=timezone.utc),
        label="synth-older",
    )
    _add_fund_manager_report(
        session, plan_version_id=older_id,
        decision_id=f"plan-synth-{older_id}",
        reasons=["OLDER topic — OLDER detail"],
    )
    _add_user_state(
        session, plan_version_id=older_id, objection_index=0,
        stance="AGREE", counter_position="older note",
        topic="OLDER topic", detail="OLDER detail",
    )

    # Newer prior — should be the one selected.
    newer_id = _add_draft(
        session,
        created_at=datetime(2026, 5, 30, tzinfo=timezone.utc),
        label="synth-newer",
    )
    _add_fund_manager_report(
        session, plan_version_id=newer_id,
        decision_id=f"plan-synth-{newer_id}",
        reasons=["NEWER topic — NEWER detail"],
    )
    _add_user_state(
        session, plan_version_id=newer_id, objection_index=0,
        stance="AGREE", counter_position="newer note",
        topic="NEWER topic", detail="NEWER detail",
    )

    current_id = _add_draft(
        session,
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        label="synth-current",
    )

    result = get_prior_resolved_concerns(
        session, user_id="ariel", current_plan_version_id=current_id
    )

    assert len(result) == 1
    assert result[0].counter_position == "newer note"
    assert "NEWER" in result[0].topic + result[0].detail


def test_current_draft_excluded_even_if_it_has_stances():
    """The user might have marked stances on the CURRENT draft already
    (e.g., page reload mid-triage). Those must NOT be carried into the
    same draft's FM prompt — the carry-forward is for prior drafts only."""
    from argosy.services.prior_resolved_concerns import (
        get_prior_resolved_concerns,
    )

    session = _make_session()
    _seed_user(session)
    current_id = _add_draft(
        session,
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        label="synth-current",
    )
    _add_fund_manager_report(
        session, plan_version_id=current_id,
        decision_id=f"plan-synth-{current_id}",
        reasons=["CURRENT topic — detail"],
    )
    _add_user_state(
        session, plan_version_id=current_id, objection_index=0,
        stance="AGREE", counter_position="don't carry this",
    )

    result = get_prior_resolved_concerns(
        session, user_id="ariel", current_plan_version_id=current_id
    )
    assert result == []
