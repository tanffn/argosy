"""A user-settled FI spend basis governs — and stays auditable.

Ariel settled the headline FI spend at ₪300,000/yr on 2026-08-24. Recording
that as GUIDANCE alone would have the fleet write ₪300,000 into the prose while
``{{fact:retirement.fi_target_nis}}`` kept resolving to the derived ₪311,584 —
manufacturing exactly the "one concept, two published values" contradiction
codex blocks on (C2_NO_CONTRADICTION). So it lives in ``goals_yaml`` and the
resolver reads it.

The second requirement is that it not become a black box: the derived line
items must remain visible and the itemisation must still sum to the published
basis, via an explicit adjustment row that cites the directive.
"""
from __future__ import annotations

import pytest

from argosy.services.fi_methodology import (
    SWR_REAL_CENTRAL_PCT,
    compute_fi_target,
)


@pytest.fixture
def ctx_session(tmp_path):
    """A real session with a minimal UserContext — no mocked seams."""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import Base, UserContext

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(UserContext(
        user_id="ariel",
        identity_yaml=(
            "monthly_expenses_total_nis: 23084\n"
            "monthly_expenses_breakdown:\n"
            "  mortgage_nis: 2952\n"
        ),
        goals_yaml="",
    ))
    session.commit()
    return session


def _set_goal(session, key, value):
    import yaml
    from argosy.state.models import UserContext
    import sqlalchemy as sa

    ctx = session.execute(
        sa.select(UserContext).where(UserContext.user_id == "ariel")
    ).scalar_one()
    data = yaml.safe_load(ctx.goals_yaml or "") or {}
    data[key] = value
    ctx.goals_yaml = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    session.commit()
    session.expire_all()


def test_derived_basis_when_no_directive(ctx_session):
    m = compute_fi_target(ctx_session, user_id="ariel")
    assert m is not None
    # tracked 276,996 - mortgage 35,424 + car 40k + health 15k + home 15k
    assert m.permanent_annual_spend_nis == pytest.approx(311_572, abs=50)
    assert not any(
        "User-settled" in c.label for c in m.components
    ), "no adjustment row without a directive"


def test_settled_basis_governs_the_published_target(ctx_session):
    _set_goal(ctx_session, "fi_target_annual_spend_nis", 300_000.0)
    m = compute_fi_target(ctx_session, user_id="ariel")
    assert m.permanent_annual_spend_nis == pytest.approx(300_000.0)
    assert m.fi_perpetuity_nis == pytest.approx(300_000.0 / SWR_REAL_CENTRAL_PCT)
    assert m.fi_perpetuity_nis == pytest.approx(10_000_000.0)


def test_itemisation_still_reconciles_to_the_published_basis(ctx_session):
    """The audit trail must not be replaced by the directive — only extended."""
    _set_goal(ctx_session, "fi_target_annual_spend_nis", 300_000.0)
    m = compute_fi_target(ctx_session, user_id="ariel")

    permanent = [c for c in m.components if c.kind == "permanent"]
    assert sum(c.annual_nis for c in permanent) == pytest.approx(300_000.0)

    # The derived lines survive.
    labels = [c.label for c in permanent]
    assert any("Tracked baseline living" in x for x in labels)
    assert any("Car replacement" in x for x in labels)

    # The delta is explicit and attributed.
    adj = [c for c in permanent if "User-settled" in c.label]
    assert len(adj) == 1
    assert adj[0].annual_nis == pytest.approx(-11_572, abs=50)
    assert "goals_yaml.fi_target_annual_spend_nis" in adj[0].source
    assert "300,000" in m.itemized_spend_derivation()


def test_reserve_is_untouched_by_the_spend_directive(ctx_session):
    """The finite-liability reserve is a separate bucket, not capitalised."""
    before = compute_fi_target(ctx_session, user_id="ariel")
    _set_goal(ctx_session, "fi_target_annual_spend_nis", 300_000.0)
    after = compute_fi_target(ctx_session, user_id="ariel")
    assert (after.finite_liability_reserve_nis
            == before.finite_liability_reserve_nis)
    assert after.fi_total_capital_nis == pytest.approx(
        after.fi_perpetuity_nis + after.finite_liability_reserve_nis
    )


def test_method_string_discloses_the_directive(ctx_session):
    _set_goal(ctx_session, "fi_target_annual_spend_nis", 300_000.0)
    m = compute_fi_target(ctx_session, user_id="ariel")
    assert "user-settled basis" in m.method


@pytest.mark.parametrize("bad", [0, -1, None])
def test_non_positive_directive_is_ignored(ctx_session, bad):
    _set_goal(ctx_session, "fi_target_annual_spend_nis", bad)
    m = compute_fi_target(ctx_session, user_id="ariel")
    assert m.permanent_annual_spend_nis == pytest.approx(311_572, abs=50)
