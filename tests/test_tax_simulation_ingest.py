from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.tax_simulation_ingest import (
    LotTaxAggregate,
    dated_eligible_shares,
    eligibility_schedule,
    eligible_shares,
    ingest_report,
    realization_tax_summary,
    section_102_eligible_date,
)
from argosy.services.tax_simulation_parser import TaxSimLot, TaxSimReport
from argosy.state.models import TaxSimulationLot


def _db():
    eng = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    TaxSimulationLot.__table__.create(eng)
    return sessionmaker(bind=eng)()


def _report(sim_date):
    return TaxSimReport(simulation_date=sim_date, lots=[
        TaxSimLot(plan_type="RSU", shares=100, holding_period="OK", eligible=True, grant_id="213000"),
        TaxSimLot(plan_type="RSU", shares=30, holding_period="Breaking", eligible=False, grant_id="331375"),
        TaxSimLot(plan_type="ESPP", shares=20, holding_period="OK", eligible=True),
    ])


def test_ingest_and_eligibility():
    s = _db()
    res = ingest_report(s, user_id="ariel", report=_report("18/06/2026"))
    assert res["lots"] == 3 and res["eligible_shares"] == 120 and res["breaking_shares"] == 30
    assert eligible_shares(s, "ariel") == 120
    assert eligible_shares(s, "ariel", eligible=False) == 30
    assert eligible_shares(s, "ariel", plan_type="RSU") == 100


def test_reingest_same_date_is_idempotent():
    s = _db()
    ingest_report(s, user_id="ariel", report=_report("18/06/2026"))
    ingest_report(s, user_id="ariel", report=_report("18/06/2026"))  # re-ingest
    assert s.execute(sa.select(sa.func.count()).select_from(TaxSimulationLot)).scalar() == 3


def test_latest_report_wins():
    s = _db()
    ingest_report(s, user_id="ariel", report=_report("01/01/2026"))
    rep2 = TaxSimReport(simulation_date="18/06/2026", lots=[
        TaxSimLot(plan_type="RSU", shares=500, holding_period="OK", eligible=True, grant_id="213000")])
    ingest_report(s, user_id="ariel", report=rep2)
    assert eligible_shares(s, "ariel") == 500  # latest ingested report


# ---------------------------------------------------------------------------
# realization_tax_summary — embedded tax aggregation and price revaluation
# ---------------------------------------------------------------------------

def _report_with_prices(sim_date="18/06/2026"):
    """Two lots: one eligible (30% CGT effective), one breaking (implied ~54% effective).

    Eligible lot:   100 sh @ $200, cost_basis $50 → cap_gain $15,000, ord_inc $5,000
                    capital_tax = 30% × $15,000 = $4,500
                    ordinary_tax = 50% × $5,000 = $2,500
                    net_proceeds = $20,000 − $4,500 − $2,500 = $13,000
                    gross = $20,000, embedded_tax = $7,000

    Breaking lot:   50 sh @ $200, ordinary_income = $10,000 (full gross), net = $4,600
                    implied_rate = ($10,000 − $4,600) / $10,000 = 54%
    """
    return TaxSimReport(simulation_date=sim_date, lots=[
        TaxSimLot(
            plan_type="RSU", shares=100, holding_period="OK", eligible=True,
            grant_id="A1",
            sale_price_usd=200.0, cost_basis_usd=50.0,
            capital_income_usd=15_000.0,
            ordinary_income_usd=5_000.0,
            net_proceeds_usd=13_000.0,
        ),
        TaxSimLot(
            plan_type="RSU", shares=50, holding_period="Breaking", eligible=False,
            grant_id="B1",
            sale_price_usd=200.0, cost_basis_usd=0.0,
            capital_income_usd=0.0,
            ordinary_income_usd=10_000.0,
            net_proceeds_usd=4_600.0,
        ),
    ])


def test_realization_tax_summary_no_report():
    s = _db()
    assert realization_tax_summary(s, "ariel") is None


def test_realization_tax_summary_at_sim_price():
    """At-simulation figures must match the ingested net_proceeds exactly."""
    s = _db()
    ingest_report(s, user_id="ariel", report=_report_with_prices())
    agg = realization_tax_summary(s, "ariel")
    assert isinstance(agg, LotTaxAggregate)
    assert agg.simulation_date == "18/06/2026"
    assert agg.sim_sale_price_usd == pytest.approx(200.0)
    assert agg.total_shares == pytest.approx(150.0)
    # Gross = 100×200 + 50×200 = 30,000
    assert agg.gross_at_sim_usd == pytest.approx(30_000.0)
    # Net = 13,000 + 4,600 = 17,600
    assert agg.net_at_sim_usd == pytest.approx(17_600.0)
    # Embedded tax = 7,000 + 5,400 = 12,400
    assert agg.embedded_tax_at_sim_usd == pytest.approx(12_400.0)
    # No revaluation requested → mirrors sim price
    assert not agg.uses_current_price
    assert agg.revalue_price_usd == pytest.approx(200.0)
    assert agg.embedded_tax_at_revalue_usd == pytest.approx(12_400.0)


def test_realization_tax_summary_revalue_eligible_lot():
    """Revaluing eligible lots: only capital slice shifts; ordinary income fixed.

    At new price $220 (Δ=$20):
    - Eligible lot (100 sh): Δembedded_tax = 30% × $20 × 100 = $600
      new embedded_tax = $7,000 + $600 = $7,600; new_gross = $22,000
    - Breaking lot (50 sh, cost_basis=0 so ordinary_income=gross):
      r_effective = tax / ordinary_income = 5,400 / 10,000 = 0.54
      ordinary_rev = 10,000 + (220-200) × 50 = 11,000
      lot_tax_rev = 0.54 × 11,000 = $5,940
    Total new_embedded_tax = $7,600 + $5,940 = $13,540
    Total new_gross = $22,000 + $11,000 = $33,000
    Total new_net = $33,000 − $13,540 = $19,460
    """
    s = _db()
    ingest_report(s, user_id="ariel", report=_report_with_prices())
    agg = realization_tax_summary(s, "ariel", current_nvda_price_usd=220.0)
    assert agg.uses_current_price
    assert agg.revalue_price_usd == pytest.approx(220.0)
    assert agg.gross_at_revalue_usd == pytest.approx(33_000.0)
    # Eligible lot: $7,000 + 0.30 × $20 × 100 = $7,600
    # Breaking lot: (5,400/10,000) × 11,000 = 0.54 × 11,000 = $5,940
    assert agg.embedded_tax_at_revalue_usd == pytest.approx(13_540.0)
    assert agg.net_at_revalue_usd == pytest.approx(19_460.0)


def test_realization_tax_summary_revalue_higher_tax_than_sim():
    """At a higher NVDA price, embedded tax grows → net margin worsens (conservative)."""
    s = _db()
    ingest_report(s, user_id="ariel", report=_report_with_prices())
    agg_sim = realization_tax_summary(s, "ariel")
    agg_higher = realization_tax_summary(s, "ariel", current_nvda_price_usd=250.0)
    # Higher price → higher embedded tax → understating at sim price flatters user
    assert agg_higher.embedded_tax_at_revalue_usd > agg_sim.embedded_tax_at_sim_usd
    # Also: net proceeds at higher price > sim (you get more even after bigger tax)
    assert agg_higher.net_at_revalue_usd > agg_sim.net_at_sim_usd


# ---------------------------------------------------------------------------
# Blocker 3 — price-fall clamp: tax must never go negative for eligible lots
# ---------------------------------------------------------------------------

def test_blocker3_price_fall_clamp_prevents_negative_tax():
    """Blocker 3: when the price falls below the sim price, the eligible-lot capital-gain
    delta (30% × Δprice × shares) reduces the tax — but it must NEVER fall below the
    ordinary-income tax floor (50% × ordinary_income_usd), because the ordinary slice
    is owed regardless of the capital-gain outcome.  Without this clamp, a deep price
    fall drives the raw formula negative, making net proceeds exceed gross (impossible).

    Setup: eligible lot, 100 sh @ $200, cost_basis $180:
      capital_income = ($200 − $180) × 100 = $2,000
      ordinary_income = $1,800 (fixed at grant date)
      net = $20,000 − 30% × $2,000 − 50% × $1,800 = $20,000 − $600 − $900 = $18,500
      embedded_tax_sim = $1,500.  ordinary_tax_floor = 50% × $1,800 = $900.

    The clamp kicks in whenever:
      raw = sim_tax + 30% × Δprice × shares < ordinary_tax_floor = $900
      ⟺ 1,500 + 30% × Δ × 100 < 900
      ⟺ Δ < −20, i.e. new_price < $180 (= cost_basis).

    At $175 (Δ=−$25 → raw=$750 < floor=$900): clamped to $900.
    At $140 (Δ=−$60 → raw=−$300 < floor=$900): also clamped to $900.
    At $185 (Δ=−$15 → raw=$1,050 ≥ floor=$900): NOT clamped (uses raw).
    """
    s = _db()
    rep = TaxSimReport(simulation_date="18/06/2026", lots=[
        TaxSimLot(
            plan_type="RSU", shares=100, holding_period="OK", eligible=True,
            grant_id="A1",
            sale_price_usd=200.0, cost_basis_usd=180.0,
            capital_income_usd=2_000.0,
            ordinary_income_usd=1_800.0,
            net_proceeds_usd=18_500.0,
        ),
    ])
    ingest_report(s, user_id="ariel", report=rep)

    # At $185 (Δ=−$15): raw = 1,500 + 30%(−15)(100) = 1,500 − 450 = $1,050 ≥ $900 floor.
    agg_185 = realization_tax_summary(s, "ariel", current_nvda_price_usd=185.0)
    assert agg_185.embedded_tax_at_revalue_usd == pytest.approx(1_050.0)
    assert agg_185.net_at_revalue_usd == pytest.approx(185.0 * 100 - 1_050.0)

    # At $175 (Δ=−$25): raw = 1,500 − 750 = $750 < floor $900 → clamped to $900.
    agg_175 = realization_tax_summary(s, "ariel", current_nvda_price_usd=175.0)
    assert agg_175.embedded_tax_at_revalue_usd == pytest.approx(900.0), (
        "clamp must fire: raw $750 < ordinary-tax floor $900"
    )
    # Net proceeds = $17,500 − $900 = $16,600 < gross ($17,500). ✓
    assert agg_175.net_at_revalue_usd < agg_175.gross_at_revalue_usd
    assert agg_175.net_at_revalue_usd == pytest.approx(175.0 * 100 - 900.0)

    # At $140 (Δ=−$60): raw = 1,500 − 1,800 = −$300 → clamped to $900.
    agg_140 = realization_tax_summary(s, "ariel", current_nvda_price_usd=140.0)
    assert agg_140.embedded_tax_at_revalue_usd == pytest.approx(900.0)
    assert agg_140.net_at_revalue_usd < agg_140.gross_at_revalue_usd
    assert agg_140.net_at_revalue_usd == pytest.approx(140.0 * 100 - 900.0)


# ---------------------------------------------------------------------------
# Blocker 2 — breaking lot with positive cost_basis: tax must not be understated
# ---------------------------------------------------------------------------

def test_blocker2_breaking_lot_positive_cost_basis():
    """Blocker 2: when a breaking lot has positive cost_basis, using tax/gross as the
    implied rate rather than tax/ordinary_income understates the tax at higher prices
    because cost_basis is absorbed into the denominator (gross) but not the numerator.

    Setup: breaking lot, 50 sh @ $200, cost_basis $50.
    ordinary_income = ($200 − $50) × 50 = $7,500.
    The simulation marks the entire gain as ordinary income at ~55% rate:
    net = $10,000 − 55% × $7,500 = $10,000 − $4,125 = $5,875.
    embedded_tax_sim = $4,125.

    OLD formula (tax/gross): implied_rate = $4,125 / $10,000 = 41.25%.
      At $220: new_tax = 41.25% × $11,000 = $4,537.50.
    CORRECT formula (tax/ordinary_income): r_eff = $4,125 / $7,500 = 55%.
      At $220: ordinary_rev = $7,500 + ($220−$200)×50 = $8,500.
              new_tax = 55% × $8,500 = $4,675.  ← LARGER (more conservative).
    """
    s = _db()
    rep = TaxSimReport(simulation_date="18/06/2026", lots=[
        TaxSimLot(
            plan_type="RSU", shares=50, holding_period="Breaking", eligible=False,
            grant_id="B1",
            sale_price_usd=200.0, cost_basis_usd=50.0,
            capital_income_usd=0.0,
            ordinary_income_usd=7_500.0,
            net_proceeds_usd=5_875.0,
        ),
    ])
    ingest_report(s, user_id="ariel", report=rep)

    agg = realization_tax_summary(s, "ariel", current_nvda_price_usd=220.0)
    # Correct formula: 55% × (7,500 + 20 × 50) = 55% × 8,500 = 4,675
    assert agg.embedded_tax_at_revalue_usd == pytest.approx(4_675.0)
    # Verify: old formula would have given 41.25% × 11,000 = 4,537.50 (too low)
    old_formula_result = (4_125.0 / 10_000.0) * (50 * 220.0)
    assert agg.embedded_tax_at_revalue_usd > old_formula_result, (
        "new formula must produce LARGER (more conservative) tax than the old gross-rate formula"
    )


# ---------------------------------------------------------------------------
# Round 3 Blocker 2 — lots missing net_proceeds_usd excluded from BOTH gross and tax
# ---------------------------------------------------------------------------

def test_round3_blocker2_incomplete_lot_excluded_from_both_gross_and_tax():
    """R3 B2: a lot with shares but no net_proceeds_usd must be excluded from BOTH
    gross_at_sim and embedded_tax_at_sim. Previously it was counted in gross but not
    in net, making the full gross appear as 'embedded tax' (overstates tax) or,
    depending on direction, silently understating the net figure.

    Setup: one complete lot (100 sh @ $200, net=$13,000) and one incomplete lot
    (50 sh @ $200, net_proceeds_usd=None). Only the complete lot should appear in
    gross/net/tax. The incomplete lot shares appear in total_shares and in
    incomplete_lot_shares."""
    s = _db()
    rep = TaxSimReport(simulation_date="18/06/2026", lots=[
        TaxSimLot(
            plan_type="RSU", shares=100, holding_period="OK", eligible=True,
            grant_id="A1",
            sale_price_usd=200.0, cost_basis_usd=50.0,
            capital_income_usd=15_000.0,
            ordinary_income_usd=5_000.0,
            net_proceeds_usd=13_000.0,
        ),
        TaxSimLot(
            plan_type="RSU", shares=50, holding_period="OK", eligible=True,
            grant_id="A2",
            sale_price_usd=200.0, cost_basis_usd=50.0,
            capital_income_usd=7_500.0,
            ordinary_income_usd=2_500.0,
            net_proceeds_usd=None,  # ← incomplete
        ),
    ])
    ingest_report(s, user_id="ariel", report=rep)

    agg = realization_tax_summary(s, "ariel")
    assert agg is not None

    # Total shares includes both lots.
    assert agg.total_shares == pytest.approx(150.0)

    # Incomplete lot excluded from gross and net: only 100 sh × $200 = $20,000 gross.
    assert agg.gross_at_sim_usd == pytest.approx(20_000.0), (
        "incomplete lot must be excluded from gross_at_sim"
    )
    assert agg.net_at_sim_usd == pytest.approx(13_000.0)
    assert agg.embedded_tax_at_sim_usd == pytest.approx(7_000.0)

    # incomplete_lot_shares reflects the 50 excluded shares.
    assert agg.incomplete_lot_shares == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Round 3 Blocker 3 — breaking lots with zero ordinary_income refused, not
# revalued via the old gross-rate fallback
# ---------------------------------------------------------------------------

def test_round3_blocker3_breaking_lot_no_ordinary_income_refused_for_revaluation():
    """R3 B3: a breaking lot with ordinary_income_usd=0 must be REFUSED from the
    revaluation loop (not fall back to the understating implied-rate-on-gross formula).
    Its shares are counted in total_shares and in incomplete_lot_shares; it is NOT
    counted in gross_at_revalue or net_at_revalue.

    Setup: one eligible lot (100 sh) and one breaking lot with ordinary_income=0 (50 sh).
    At revalue price $220, only the eligible lot contributes to the revalue figures.
    """
    s = _db()
    rep = TaxSimReport(simulation_date="18/06/2026", lots=[
        TaxSimLot(
            plan_type="RSU", shares=100, holding_period="OK", eligible=True,
            grant_id="A1",
            sale_price_usd=200.0, cost_basis_usd=50.0,
            capital_income_usd=15_000.0,
            ordinary_income_usd=5_000.0,
            net_proceeds_usd=13_000.0,
        ),
        TaxSimLot(
            plan_type="RSU", shares=50, holding_period="Breaking", eligible=False,
            grant_id="B1",
            sale_price_usd=200.0, cost_basis_usd=200.0,
            capital_income_usd=0.0,
            ordinary_income_usd=0.0,      # ← zero: cannot derive r_effective
            net_proceeds_usd=4_600.0,     # has net_proceeds (complete for sim)
        ),
    ])
    ingest_report(s, user_id="ariel", report=rep)

    # At sim price: breaking lot IS in the sim totals (it has net_proceeds_usd).
    agg_sim = realization_tax_summary(s, "ariel")
    assert agg_sim.gross_at_sim_usd == pytest.approx(100 * 200 + 50 * 200)  # both lots
    assert agg_sim.incomplete_lot_shares == pytest.approx(0.0)  # both complete for sim

    # At revalue price: breaking lot is REFUSED (no ordinary_income → incomplete_lot_shares).
    agg_rev = realization_tax_summary(s, "ariel", current_nvda_price_usd=220.0)
    assert agg_rev.uses_current_price

    # Only 100 sh (eligible lot) contribute to revalue figures.
    assert agg_rev.gross_at_revalue_usd == pytest.approx(100 * 220.0)
    # Eligible-lot tax at $220: $7,000 + 30% × $20 × 100 = $7,600.
    assert agg_rev.embedded_tax_at_revalue_usd == pytest.approx(7_600.0)
    assert agg_rev.net_at_revalue_usd == pytest.approx(100 * 220 - 7_600)

    # The 50 refused shares are in incomplete_lot_shares.
    assert agg_rev.incomplete_lot_shares == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# RED-9 (plan-109 review): dated Section-102 eligibility (24 months from grant,
# Amendment 147) — eligibility is time-varying, not a fixed snapshot.
# ---------------------------------------------------------------------------


def test_section_102_eligible_date_is_24_months_from_grant():
    """Amendment 147: 24 months FROM THE GRANT (allotment) date, not end of the
    tax year of grant. domain_knowledge/tax/israel/section_102.md."""
    assert section_102_eligible_date("10/03/2025") == date(2027, 3, 10)
    assert section_102_eligible_date("08/04/2024") == date(2026, 4, 8)
    # No parseable grant date (e.g. ESPP rows in the ingested report) -> unknown,
    # never a guessed maturity.
    assert section_102_eligible_date("") is None
    assert section_102_eligible_date(None) is None
    assert section_102_eligible_date("49.355") is None  # not a date at all


def _dated_report(sim_date="18/06/2026"):
    """Mirrors the shape of the live book: one already-eligible core, one
    Breaking RSU grant with a KNOWN maturity date, and one Breaking ESPP lot
    with no parseable grant date (maturity genuinely unknown)."""
    return TaxSimReport(simulation_date=sim_date, lots=[
        TaxSimLot(
            plan_type="RSU", shares=9_230, holding_period="OK", eligible=True,
            grant_id="213000", grant_date="08/06/2022",
            sale_price_usd=200.0, cost_basis_usd=20.0,
            capital_income_usd=9_230 * 180.0, ordinary_income_usd=9_230 * 20.0,
            net_proceeds_usd=9_230 * 200.0 * 0.85,
        ),
        TaxSimLot(
            plan_type="RSU", shares=358, holding_period="Breaking", eligible=False,
            grant_id="331375", grant_date="10/03/2025",
            sale_price_usd=200.0, cost_basis_usd=150.0,
            capital_income_usd=0.0, ordinary_income_usd=358 * 50.0,
            net_proceeds_usd=358 * 150.0,
        ),
        TaxSimLot(
            plan_type="ESPP", shares=1_295, holding_period="Breaking", eligible=False,
            grant_id="", grant_date="",  # no dated grant -> unknown maturity
            sale_price_usd=200.0, cost_basis_usd=190.0,
            capital_income_usd=0.0, ordinary_income_usd=1_295 * 10.0,
            net_proceeds_usd=1_295 * 190.0,
        ),
    ])


def test_eligibility_schedule_dates_the_breaking_rsu_lot_only():
    """A Breaking lot with a parseable grant date gets a dated tranche; the ESPP
    lot (no grant date in the report) is excluded — its maturity is unknown, not
    assumed. This is the dated multi-year eligibility schedule Sol asked for."""
    s = _db()
    ingest_report(s, user_id="ariel", report=_dated_report())
    sched = eligibility_schedule(s, "ariel")
    assert len(sched) == 1
    assert sched[0].grant_id == "331375"
    assert sched[0].shares == pytest.approx(358.0)
    assert sched[0].eligible_date == date(2027, 3, 10)


def test_dated_eligible_shares_grows_past_the_report_snapshot():
    """The eligible pool is NOT a fixed 9,230 forever: by 2027-03-10 the 331375
    grant matures, growing the dated-eligible pool to 9,588 — the ESPP tail
    (1,295 sh, no dated grant) never grows because its maturity is unknown."""
    s = _db()
    ingest_report(s, user_id="ariel", report=_dated_report())

    assert eligible_shares(s, "ariel") == pytest.approx(9_230.0)  # unchanged snapshot
    assert dated_eligible_shares(s, "ariel", date(2026, 12, 31)) == pytest.approx(9_230.0)
    assert dated_eligible_shares(s, "ariel", date(2027, 3, 9)) == pytest.approx(9_230.0)  # 1 day early
    assert dated_eligible_shares(s, "ariel", date(2027, 3, 10)) == pytest.approx(9_588.0)  # matures
    assert dated_eligible_shares(s, "ariel", date(2030, 1, 1)) == pytest.approx(9_588.0)  # ESPP never joins


def test_dated_eligible_shares_no_report_is_none():
    s = _db()
    assert dated_eligible_shares(s, "ariel", date(2027, 1, 1)) is None


def test_realization_tax_summary_as_of_date_relabels_matured_lot_to_capital_rate():
    """Selling 9,480 sh (the plan's planned glide sale) is short of the 9,230-sh
    snapshot-eligible pool by 250 sh — but by 2027-03-10 the 331375 grant matures,
    covering the shortfall entirely at CAPITAL rates. Without ``as_of_date`` the
    250-sh shortfall prices at the Breaking (ordinary) rate; with the dated
    projection it prices at the Section-102 Capital rate instead."""
    s = _db()
    ingest_report(s, user_id="ariel", report=_dated_report())

    sell_sh = 9_480.0
    eligible_now = 9_230.0
    breaking_now = sell_sh - eligible_now  # 250

    undated = realization_tax_summary(
        s, "ariel", max_eligible_shares=eligible_now, max_breaking_shares=breaking_now,
    )
    assert undated.total_shares == pytest.approx(sell_sh)

    dated = realization_tax_summary(
        s, "ariel", max_eligible_shares=sell_sh, max_breaking_shares=0.0,
        as_of_date=date(2027, 12, 31),
    )
    assert dated.total_shares == pytest.approx(sell_sh)
    # Every one of the 9,480 planned-sale shares now prices at the eligible
    # (capital-track) group's economics — no breaking-rate shares remain.
    # Sanity: the 250-sh shortfall came entirely from the matured 331375 grant
    # (cost_basis $150, ordinary_income $50/sh), which is a DIFFERENT per-share
    # economics than the undated run's arbitrary first-in-query-order breaking
    # lot (the ESPP grant), so the two totals need not be numerically equal —
    # only the SHARE COUNT priced at capital-vs-ordinary rate must differ.
    assert dated.embedded_tax_at_sim_usd != pytest.approx(undated.embedded_tax_at_sim_usd)


def test_realization_tax_summary_as_of_date_espp_never_relabeled():
    """as_of_date must NEVER relabel the ESPP tail (no parseable grant date) —
    even at a far-future as_of, only the dated RSU grant matures."""
    s = _db()
    ingest_report(s, user_id="ariel", report=_dated_report())

    # Cap at the full dated-eligible pool (9,588) + 0 breaking: must succeed
    # without needing to touch the ESPP tail.
    agg = realization_tax_summary(
        s, "ariel", max_eligible_shares=9_588.0, max_breaking_shares=0.0,
        as_of_date=date(2099, 1, 1),
    )
    assert agg.total_shares == pytest.approx(9_588.0)

    # Asking for MORE than the dated-eligible pool at max_breaking=0 must fall
    # back to whatever's left in the (still-unrelabeled) ESPP breaking group —
    # i.e. the ESPP shares are still excluded from the "eligible" cap group.
    agg_all = realization_tax_summary(
        s, "ariel", max_eligible_shares=9_588.0, max_breaking_shares=1_295.0,
        as_of_date=date(2099, 1, 1),
    )
    assert agg_all.total_shares == pytest.approx(9_588.0 + 1_295.0)
