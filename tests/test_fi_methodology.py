"""Unit tests for the deterministic FI methodology."""

from __future__ import annotations

import textwrap

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.fi_methodology import (
    SWR_REAL_CENTRAL_PCT,
    compute_fi_target,
)
from argosy.state.models import Base, User, UserContext

IDENTITY = textwrap.dedent(
    """
    monthly_expenses_total_nis: 23084
    monthly_expenses_breakdown:
      mortgage_nis: 2952
    mortgage_balance:
      keret_1_nis: 350000
    """
)

GOALS = textwrap.dedent(
    """
    education_funding_targets:
      combined_household_contribution_nis: 1000000
    retirement_drawdown_style: capital_preservation_returns_only
    """
)


@pytest.fixture
def session(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'fi.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        s.add(User(id="ariel", plan="free"))
        s.add(UserContext(user_id="ariel", identity_yaml=IDENTITY, goals_yaml=GOALS))
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


def test_permanent_spend_excludes_mortgage_adds_lifeevents(session):
    m = compute_fi_target(session, user_id="ariel")
    assert m is not None
    # baseline 277,008; ex-mortgage 277,008 - 35,424 = 241,584
    # + car 40,000 + healthcare 15,000 + home 15,000 = 311,584
    assert m.baseline_annual_nis == pytest.approx(277_008)
    assert m.permanent_annual_spend_nis == pytest.approx(311_584)


def test_perpetuity_uses_swr_not_return(session):
    m = compute_fi_target(session, user_id="ariel")
    assert m.swr_real_pct == SWR_REAL_CENTRAL_PCT == 0.030
    # perpetuity = permanent_spend / swr, NOT / 0.045 return assumption
    assert m.fi_perpetuity_nis == pytest.approx(311_584 / 0.030, rel=1e-6)
    # The wrong (old) methodology would be 277,008 / 0.045 = 6.15M — assert
    # we are nowhere near it.
    assert m.fi_perpetuity_nis > 9_000_000


def test_finite_liabilities_in_reserve_not_perpetuity(session):
    m = compute_fi_target(session, user_id="ariel")
    # education 1,000,000 + mortgage 350,000 + wedding 100,000
    assert m.finite_liability_reserve_nis == pytest.approx(1_450_000)
    assert m.fi_total_capital_nis == pytest.approx(
        m.fi_perpetuity_nis + 1_450_000
    )
    # Education must NOT be capitalized into the perpetuity (perpetuity is
    # spend/SWR exactly — keeps the FiBase yield identity intact).
    assert m.fi_perpetuity_nis == pytest.approx(
        m.permanent_annual_spend_nis / m.swr_real_pct, rel=1e-9
    )


def test_swr_override_and_band(session):
    m = compute_fi_target(session, user_id="ariel", swr_real_pct=0.024)
    assert m.swr_real_pct == 0.024
    assert m.fi_perpetuity_nis == pytest.approx(311_584 / 0.024, rel=1e-6)
    assert m.swr_band == (0.024, 0.035)


def test_spend_override(session):
    # household_budget agent's monthly_burn*12 wins when supplied.
    m = compute_fi_target(session, user_id="ariel", spend_t12_nis=300_000)
    # 300,000 - 35,424 mortgage + 40,000 + 15,000 + 15,000 = 334,576
    assert m.permanent_annual_spend_nis == pytest.approx(334_576)


def test_no_baseline_returns_none(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    # No UserContext at all → cannot source baseline → None (never fabricate).
    assert compute_fi_target(s, user_id="ariel") is None
    s.close()
    engine.dispose()
