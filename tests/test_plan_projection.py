"""Compact-projection generator — pure Python, no LLM call.

Reads a synthesized PlanVersion (role='current') and emits a
~500-800 token markdown block for injection into advisor prompts.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


def _make_horizon(horizon, status="no_change", **kw):
    from argosy.agents.plan_synthesizer_types import HorizonSection

    base = dict(
        horizon=horizon,
        freshness_expected={"long": "annual", "medium": "quarterly", "short": "monthly"}[horizon],
        status=status,
        posture=f"{horizon} posture",
        targets=[],
        themes=[],
        actions=[],
        speculative_candidates=[],
        deltas_from_prior=[],
        rationale="",
        cited_sources=[],
    )
    base.update(kw)
    return HorizonSection(**base)


@pytest.fixture
def session_with_current(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.commit()

    long = _make_horizon("long", status="no_change", posture="Wealth maximization; retirement target 2031.")
    medium = _make_horizon("medium", status="minor_revision", posture="Continue NVDA reduction; growth tilt.")
    short = _make_horizon("short", status="major_revision", posture="Sell NVDA tranche; harvest IBIT.")

    pv = PlanVersion(
        user_id="ariel",
        role="current",
        version_label="synth-2026-05",
        raw_markdown="",
        horizon_long_json=long.model_dump_json(),
        horizon_medium_json=medium.model_dump_json(),
        horizon_short_json=short.model_dump_json(),
    )
    s.add(pv)
    s.commit()
    s.refresh(pv)
    yield s, pv
    s.close()


def test_compact_projection_includes_all_three_horizons(session_with_current):
    from argosy.agents._plan_projection import compact_projection

    s, pv = session_with_current
    md = compact_projection(s, user_id="ariel")
    assert md is not None
    assert "[long" in md and "[medium" in md and "[short" in md
    assert "Wealth maximization" in md
    assert "Continue NVDA reduction" in md
    assert "Sell NVDA tranche" in md


def test_compact_projection_returns_none_when_no_current(alembic_engine_at_head):
    from argosy.agents._plan_projection import compact_projection
    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    try:
        s.add(User(id="dana", plan="free"))
        s.commit()
        assert compact_projection(s, user_id="dana") is None
    finally:
        s.close()


def test_compact_projection_under_token_budget(session_with_current):
    """Spec §6.4: compact projection must stay under ~1500 tokens.

    We approximate tokens as len/4 and assert <= 1500 chars * 4 = 6000
    (loose bound; the projection is bigger when full of targets).
    """
    from argosy.agents._plan_projection import compact_projection

    s, pv = session_with_current
    md = compact_projection(s, user_id="ariel")
    assert md is not None
    # Loose bound: projection should not blow past 6000 chars.
    assert len(md) < 6000, f"projection too long: {len(md)} chars"
