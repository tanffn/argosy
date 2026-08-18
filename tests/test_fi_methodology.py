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


def test_component_ledger_decomposes_permanent_and_finite(session):
    """The component ledger must be auditable: the permanent rows sum to the
    permanent-equivalent spend and the finite rows sum to the reserve. A blind
    reviewer reconciles the headline figures from these rows alone, so each row
    carries an amount + a permanent/finite tag + a source."""
    m = compute_fi_target(session, user_id="ariel")
    assert m is not None
    permanent = [c for c in m.components if c.kind == "permanent"]
    finite = [c for c in m.components if c.kind == "finite"]
    # Permanent rows reconcile to the permanent-equivalent spend.
    assert sum(c.annual_nis for c in permanent) == pytest.approx(
        m.permanent_annual_spend_nis
    )
    # Finite rows reconcile to the liquidity reserve.
    assert sum(c.reserve_nis for c in finite) == pytest.approx(
        m.finite_liability_reserve_nis
    )
    # Every component is tagged + sourced (auditable, not a magic number).
    for c in m.components:
        assert c.kind in ("permanent", "finite")
        assert c.source


def test_tracked_to_permanent_bridge_is_explicit_in_components(session):
    """The tracked-spend → permanent-equivalent bridge must be VISIBLE in the
    component ledger, not folded into a pre-netted 'ex-mortgage' row. A blind
    reviewer that sees tracked spend ₪277,008 elsewhere must be able to
    reconcile it to the ₪311,584 permanent-equivalent from the rows:
    tracked baseline (+277,008) − mortgage runoff (−35,424) + life events."""
    m = compute_fi_target(session, user_id="ariel")
    permanent = [c for c in m.components if c.kind == "permanent"]
    labels = [c.label.lower() for c in permanent]
    # An explicit, additive tracked-baseline opening row at the FULL tracked
    # figure (not the pre-netted ex-mortgage figure).
    tracked_rows = [
        c for c in permanent if c.annual_nis == pytest.approx(m.baseline_annual_nis)
    ]
    assert tracked_rows, (
        "permanent ledger must open with the full tracked baseline "
        f"(₪{m.baseline_annual_nis:,.0f}) as its own row"
    )
    # An explicit subtractive mortgage-runoff row (negative annual).
    mortgage_rows = [
        c for c in permanent if "mortgage" in c.label.lower() and c.annual_nis < 0
    ]
    assert mortgage_rows, "permanent ledger must show the mortgage runoff as a negative row"
    # The bridge still reconciles to the headline permanent-equivalent spend.
    assert sum(c.annual_nis for c in permanent) == pytest.approx(
        m.permanent_annual_spend_nis
    )


def test_derivations_appendix_renders_auditable_breakdown(session):
    """The plan's number-derivations appendix (which rides into the assembled
    artifact a blind reviewer reads) must render the REAL SpendComponent ledger
    so the headline FI figures are reconcilable line-by-line — the run-102
    `fi_target` UNVERIFIABLE block. Asserts: (a) every component appears with
    its amount + permanent/finite framing; (b) the permanent rows sum to the
    permanent-equivalent spend; (c) the finite rows sum to the reserve; (d) the
    tracked→permanent-equivalent bridge is present."""
    from argosy.orchestrator.flows.plan_synthesis.render import (
        render_number_derivations_appendix,
    )

    m = compute_fi_target(session, user_id="ariel")
    md = render_number_derivations_appendix(session=session, user_id="ariel", resolved=None)

    # (a) Every component label appears in the rendered ledger.
    for c in m.components:
        assert c.label in md, f"component {c.label!r} missing from the appendix"

    # (d) The tracked → permanent-equivalent bridge is explicit: the full
    # tracked T12, the subtractive mortgage runoff, and the headline totals.
    assert f"{m.baseline_annual_nis:,.0f}" in md          # tracked T12 (277,008)
    assert "bridge tracked T12" in md
    assert "+277,008" in md                                # additive tracked row
    assert "-35,424" in md                                 # subtractive mortgage runoff

    # (b) The permanent subtotal reconciles to the methodology's permanent spend.
    assert f"**{m.permanent_annual_spend_nis:,.0f}**" in md   # 311,584
    # (c) The finite subtotal reconciles to the reserve.
    assert f"**{m.finite_liability_reserve_nis:,.0f}**" in md  # 1,450,000

    # The rendered subtotals MUST equal the methodology's computed figures.
    assert sum(
        c.annual_nis for c in m.components if c.kind == "permanent"
    ) == pytest.approx(m.permanent_annual_spend_nis)
    assert sum(
        c.reserve_nis for c in m.components if c.kind == "finite"
    ) == pytest.approx(m.finite_liability_reserve_nis)


def test_derivations_appendix_bridge_keeps_half_year_preservation_age(session):
    """A half-year age must render at full precision in the FIRE-bridge block.
    The drun-156 reader BLOCK: the sentence read '(60 − 46) = 14 yrs ×
    ₪311,584 = ₪4,206,384' — the amount was sized over 13.5 yrs but the display
    floored both the age and the span, so the stated arithmetic didn't cohere.

    SUBJECT MOVED (2026-08-18): per Ariel's ruling (commit b7927bd),
    `retirement.fi_age` — an LLM-authored, donor-inherited estimate — must
    NEVER size the FIRE bridge or drive a published age. Published ages now
    come only from the deterministic Monte Carlo engine, as a pair:
    `retirement.preservation_age` (mandate case) sizes
    `retirement.fire_bridge_nis`, and `retirement.drawdown_scenario_age`
    (off-mandate case) sizes `retirement.fire_bridge_offmandate_nis`. This
    test now exercises the MANDATE-case bridge, which is sized from
    `preservation_age` — the age that actually drives the rendered bridge
    arithmetic today. The flooring hazard the test guards against is
    unchanged: a half-year age must not silently round to an integer while
    the span/amount stay computed on the true fractional value.
    """
    from argosy.orchestrator.flows.plan_synthesis.render import (
        render_number_derivations_appendix,
    )

    class _RV:
        def __init__(self, value, status="resolved"):
            self.value, self.status = value, status
            self.unit = "age"
            self.source_locator = "test"
            self.formula = "test"
            self.confidence = "HIGH"

    class _Resolved:
        def __init__(self, d):
            self._d = d

        def get(self, key):
            return self._d.get(key)

    # 60 − 46.5 = 13.5 yrs × permanent-equivalent spend 311,584 = 4,206,384 —
    # the same figures as the original drun-156 incident, now sized from
    # preservation_age (the deterministic mandate-case age) instead of the
    # superseded fi_age.
    resolved = _Resolved(
        {
            "retirement.preservation_age": _RV(46.5),
            "retirement.fire_bridge_nis": _RV(4_206_384),
        }
    )
    md = render_number_derivations_appendix(
        session=session, user_id="ariel", resolved=resolved
    )

    # The bridge sentence states the true half-year span and coherent arithmetic:
    # (60 − 46.5) = 13.5 yrs × 311,584 = 4,206,384.
    assert "(60 − 46.5) " in md
    assert "= 13.5 yrs" in md
    assert "₪4,206,384" in md
    assert "age 46.5→60" in md
    # The incoherent floored forms must be gone.
    assert "(60 − 46)" not in md
    assert "= 14 yrs" not in md


def test_baseline_breakdown_rows_always_reconcile_to_total_with_residual(session):
    """RED-1 (adversarial review): the displayed category rows must ALWAYS sum
    to the printed tracked-T12 total. The `session` fixture's identity_yaml
    only itemizes the mortgage line under `monthly_expenses_breakdown`, so the
    rolled-up categories (mortgage -> Housing only) sum far short of the
    authoritative `monthly_expenses_total_nis * 12` total — exactly the
    unitemized-residual shape the reviewer found. An explicit residual row
    must close the gap, and the tracked-baseline component confidence must be
    downgraded to MEDIUM while it exists."""
    m = compute_fi_target(session, user_id="ariel")
    assert m is not None
    rows_sum = sum(amt for _, amt in m.baseline_breakdown)
    assert rows_sum == pytest.approx(m.baseline_annual_nis), (
        "displayed category rows must reconcile exactly to the asserted total"
    )
    # A real, non-zero residual exists for this fixture (277,008 tracked total
    # vs. 35,424 itemized mortgage-only breakdown).
    assert m.baseline_residual_nis == pytest.approx(277_008 - 35_424)
    labels = [label for label, _ in m.baseline_breakdown]
    assert "Unitemized tracked-spend residual" in labels
    # The residual row's own value equals the computed residual (never
    # hardcoded, always derived from total - itemized rows).
    residual_rows = [amt for label, amt in m.baseline_breakdown if label == "Unitemized tracked-spend residual"]
    assert residual_rows == [pytest.approx(m.baseline_residual_nis)]
    # Confidence on the tracked-baseline component is downgraded while a
    # residual exists.
    tracked = [c for c in m.components if c.label == "Tracked baseline living (full T12)"][0]
    assert tracked.confidence == "MEDIUM"


def test_baseline_breakdown_reconciles_exactly_with_no_residual(tmp_path):
    """Counterpart to the residual case: when the itemized category rows
    already sum to the tracked total, no residual row is injected and the
    tracked-baseline component keeps its HIGH confidence."""
    identity = textwrap.dedent(
        """
        monthly_expenses_total_nis: 1000
        monthly_expenses_breakdown:
          mortgage_nis: 500
          groceries: 500
        mortgage_balance:
          keret_1_nis: 0
        """
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'reconciled.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        s.add(User(id="ariel", plan="free"))
        s.add(UserContext(user_id="ariel", identity_yaml=identity, goals_yaml=""))
        s.commit()
        m = compute_fi_target(s, user_id="ariel")
        assert m is not None
        rows_sum = sum(amt for _, amt in m.baseline_breakdown)
        assert rows_sum == pytest.approx(m.baseline_annual_nis)
        assert m.baseline_residual_nis == pytest.approx(0.0)
        labels = [label for label, _ in m.baseline_breakdown]
        assert "Unitemized tracked-spend residual" not in labels
        tracked = [c for c in m.components if c.label == "Tracked baseline living (full T12)"][0]
        assert tracked.confidence == "HIGH"
    finally:
        s.close()
        engine.dispose()


def test_derivations_appendix_residual_row_reconciles_and_says_unitemized(session):
    """The rendered appendix table (the artifact a blind reviewer actually
    reads) must show category rows that sum to the printed total, and must
    say in prose that the gap is unitemized — not just fix the underlying
    dataclass while leaving the rendered markdown dishonest."""
    from argosy.orchestrator.flows.plan_synthesis.render import (
        render_number_derivations_appendix,
    )

    m = compute_fi_target(session, user_id="ariel")
    md = render_number_derivations_appendix(session=session, user_id="ariel", resolved=None)

    assert "Unitemized tracked-spend residual" in md
    assert "unitemized" in md.lower()
    assert f"{m.baseline_residual_nis:,.0f}" in md

    # Extract the "| Category | ₪/yr | share |" table and check the rows
    # (everything before the bold Total row) sum to the bold total.
    lines = md.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("| Category"))
    total_idx = next(
        i for i, l in enumerate(lines[start:], start) if l.startswith("| **Total tracked T12**")
    )
    row_sum = 0.0
    for l in lines[start + 2:total_idx]:
        amt_str = l.split("|")[2].strip()
        row_sum += float(amt_str.replace(",", ""))
    assert row_sum == pytest.approx(m.baseline_annual_nis)


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
