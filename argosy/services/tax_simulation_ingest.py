# argosy/services/tax_simulation_ingest.py
"""Persist a parsed RSU/ESPP simulated tax report and expose eligibility to the planner.

Idempotent per (user_id, simulation_date): re-ingesting a report replaces that report's
lots. The latest ingested report (by ingested_at) is the one the derivation reads, so the
NVDA deconcentration schedule reflects how many shares are capital-track-eligible NOW.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import sqlalchemy as sa

from argosy.services.tax_simulation_parser import TaxSimReport, parse_workbook
from argosy.state.models import TaxSimulationLot

# Israeli Section-102 capital-track high-income marginal effective rate on the capital
# gain slice: 25% base CGT + 3% general surtax + 2% capital-source surtax.
# domain_knowledge/tax/israel/section_102.md § "Marginal effective in surtax zone = 30%"
# Statutory policy parameter — re-verify annually.
_SECTION_102_HIGH_INCOME_RATE: float = 0.30

# At-vest ordinary-income high-income effective rate: 47% top marginal + 3% general surtax.
# domain_knowledge/tax/israel/surtax.md § "50% marginal income tax".
# Used as the TAX FLOOR for eligible lots: even if the capital gain goes to zero because
# the price falls below the cost basis, the ordinary income tax is still owed.
_ORDINARY_HIGH_INCOME_RATE: float = 0.50


@dataclass
class LotTaxAggregate:
    """Aggregated tax figures from the latest simulation report for one user.

    Two price bases are provided:
    - *at-simulation*: the values exactly as the simulation computed them at its
      ``sim_sale_price_usd`` (18/06/2026 @ $204.65 for the current report).
    - *revalued*: adjusted to ``revalue_price_usd`` (the current NVDA mark) when that
      price is available; otherwise ``uses_current_price=False`` and the revalued fields
      mirror the at-simulation values.

    Revaluation formula (see inline comments):
    - Eligible lots (§102 Capital track, 30% effective): ordinary income is fixed at grant
      date and unchanged by sale price; only the capital slice shifts.
      ``Δembedded_tax = 0.30 × (revalue_price - sim_price) × shares``
    - Breaking lots (entire gain is ordinary income, ~54-62% effective depending on NI
      ceiling): use the per-lot implied effective rate from the simulation rather than
      re-deriving NI brackets.  ``new_tax = implied_rate × revalue_price × shares``
    """

    simulation_date: str
    sim_sale_price_usd: float
    total_shares: float

    # At-simulation price (exactly what the trustee report computed)
    gross_at_sim_usd: float
    net_at_sim_usd: float
    embedded_tax_at_sim_usd: float

    # Revalued at current NVDA mark (or mirrors at-simulation if price unavailable)
    revalue_price_usd: float
    gross_at_revalue_usd: float
    net_at_revalue_usd: float
    embedded_tax_at_revalue_usd: float
    uses_current_price: bool  # True iff revalue_price differs from sim_sale_price

    # Shares that could NOT be fully included in the tax computation (missing
    # net_proceeds_usd, or breaking lots with no ordinary_income_usd for revaluation).
    # Non-zero → tax figures cover FEWER shares than total_shares; confidence degrades.
    incomplete_lot_shares: float = 0.0


def is_tax_simulation_workbook(path: str) -> bool:
    """Recognizer for the upload pipeline: an xlsx with RSU/ESPP tabs that parse to lots."""
    try:
        rep = parse_workbook(path)
        return len(rep.lots) > 0
    except Exception:  # noqa: BLE001
        return False


def ingest_report(session, *, user_id: str, report: TaxSimReport,
                  source_file_id: int | None = None) -> dict:
    session.execute(
        sa.delete(TaxSimulationLot).where(
            TaxSimulationLot.user_id == user_id,
            TaxSimulationLot.simulation_date == report.simulation_date,
        )
    )
    now = datetime.now(timezone.utc)
    for l in report.lots:
        session.add(TaxSimulationLot(
            user_id=user_id, simulation_date=report.simulation_date, plan_type=l.plan_type,
            shares=l.shares, holding_period=l.holding_period, eligible=l.eligible,
            grant_id=l.grant_id, grant_date=l.grant_date, purchase_date=l.purchase_date,
            sale_price_usd=l.sale_price_usd, cost_basis_usd=l.cost_basis_usd,
            capital_income_usd=l.capital_income_usd, ordinary_income_usd=l.ordinary_income_usd,
            net_proceeds_usd=l.net_proceeds_usd, source_file_id=source_file_id, ingested_at=now,
        ))
    session.commit()
    return {
        "simulation_date": report.simulation_date, "lots": len(report.lots),
        "eligible_shares": report.eligible_shares(),
        "breaking_shares": report.breaking_shares(),
    }


def ingest_path(session, *, user_id: str, path: str,
                source_file_id: int | None = None) -> dict:
    """Parse + persist a report file. Used by the upload pipeline and CLI."""
    return ingest_report(
        session, user_id=user_id, report=parse_workbook(path),
        source_file_id=source_file_id,
    )


def ingest_uploaded_file(user_id: str, storage_path: str,
                         source_file_id: int | None = None) -> dict:
    """Open a fresh sync session and ingest a catalog-stored report. Used by the upload
    route (async) via a worker thread so future uploads flow straight into the plan."""
    from sqlalchemy.orm import sessionmaker

    from argosy.config import get_settings

    eng = sa.create_engine(
        f"sqlite:///{get_settings().db_file}", connect_args={"check_same_thread": False},
    )
    s = sessionmaker(bind=eng)()
    try:
        return ingest_path(s, user_id=user_id, path=storage_path,
                           source_file_id=source_file_id)
    finally:
        s.close()
        eng.dispose()


def _latest_simulation_date(session, user_id: str) -> str | None:
    return session.execute(
        sa.select(TaxSimulationLot.simulation_date)
        .where(TaxSimulationLot.user_id == user_id)
        .order_by(TaxSimulationLot.ingested_at.desc()).limit(1)
    ).scalar()


def eligible_shares(session, user_id: str, *, plan_type: str | None = None,
                    eligible: bool = True) -> float | None:
    """Capital-track-eligible (or, with eligible=False, 'Breaking') share count from the
    LATEST ingested report. None if no report ingested."""
    sim = _latest_simulation_date(session, user_id)
    if sim is None:
        return None
    q = sa.select(sa.func.coalesce(sa.func.sum(TaxSimulationLot.shares), 0.0)).where(
        TaxSimulationLot.user_id == user_id,
        TaxSimulationLot.simulation_date == sim,
        TaxSimulationLot.eligible.is_(eligible),
    )
    if plan_type:
        q = q.where(TaxSimulationLot.plan_type == plan_type)
    return float(session.execute(q).scalar() or 0.0)


def realization_tax_summary(
    session,
    user_id: str,
    *,
    current_nvda_price_usd: float | None = None,
) -> "LotTaxAggregate | None":
    """Aggregate tax figures for the ENTIRE NVDA position from the latest simulation.

    If ``current_nvda_price_usd`` is provided and differs from the simulation price, the
    result is revalued to the current mark; otherwise ``uses_current_price=False`` and the
    revalued fields mirror the at-simulation values.

    Returns ``None`` if no simulation report is ingested for this user.

    Revaluation:
    - Eligible lots: ordinary income is fixed at grant-date FMV (unchanged by sale price).
      Only the capital slice changes.  ΔEmbedded_tax = §102-rate × (new_price − sim_price) × shares.
      Floor: even if the capital gain goes to zero (price < cost_basis), the ordinary-income
      tax component is still owed, so tax is clamped to ``_ORDINARY_HIGH_INCOME_RATE × ordinary_income``.
    - Breaking lots: the simulation's per-lot effective rate is derived as tax/ordinary_income (NOT
      tax/gross) so cost_basis is held fixed rather than absorbed into the rate denominator.
      This avoids understating tax at higher prices when cost_basis > 0.
    """
    sim_date = _latest_simulation_date(session, user_id)
    if sim_date is None:
        return None

    lots = session.execute(
        sa.select(TaxSimulationLot).where(
            TaxSimulationLot.user_id == user_id,
            TaxSimulationLot.simulation_date == sim_date,
        )
    ).scalars().all()
    if not lots:
        return None

    # All lots share the same simulation sale price; pick from any non-null row.
    sim_price = next(
        (l.sale_price_usd for l in lots if l.sale_price_usd is not None), None
    )
    if sim_price is None or sim_price <= 0:
        return None

    # Blocker 2: only lots with BOTH shares AND net_proceeds_usd are "complete".
    # Incomplete lots (missing net_proceeds_usd) are counted in total_shares for
    # held-share comparison but EXCLUDED from gross/net/tax aggregation — counting
    # them in gross but not in net would make the full gross appear as "embedded tax",
    # which silently understates net proceeds.
    total_shares = sum(l.shares for l in lots if l.shares)
    complete_lots = [l for l in lots if l.shares and l.net_proceeds_usd is not None]
    incomplete_lot_shares = sum(
        (l.shares or 0.0) for l in lots
        if not (l.shares and l.net_proceeds_usd is not None)
    )

    gross_at_sim = sum(l.shares * sim_price for l in complete_lots)
    net_at_sim = sum(l.net_proceeds_usd for l in complete_lots)
    embedded_tax_at_sim = gross_at_sim - net_at_sim

    # Revaluation
    rev_price = current_nvda_price_usd if (
        current_nvda_price_usd is not None and current_nvda_price_usd > 0
        and abs(current_nvda_price_usd - sim_price) > 0.001
    ) else sim_price
    uses_current = rev_price != sim_price

    if not uses_current:
        return LotTaxAggregate(
            simulation_date=sim_date,
            sim_sale_price_usd=sim_price,
            total_shares=total_shares,
            gross_at_sim_usd=gross_at_sim,
            net_at_sim_usd=net_at_sim,
            embedded_tax_at_sim_usd=embedded_tax_at_sim,
            revalue_price_usd=sim_price,
            gross_at_revalue_usd=gross_at_sim,
            net_at_revalue_usd=net_at_sim,
            embedded_tax_at_revalue_usd=embedded_tax_at_sim,
            uses_current_price=False,
            incomplete_lot_shares=incomplete_lot_shares,
        )

    # Revalue each COMPLETE lot at the current price.
    # Track revalue_shares separately: breaking lots with no ordinary_income_usd
    # cannot be revalued (blocker 3 — refuses the old gross-rate fallback that
    # understates tax). Such lots are added to incomplete_lot_shares so the caller
    # knows the tax figure covers fewer shares than total_shares.
    delta_price = rev_price - sim_price
    revalue_embedded_tax = 0.0
    revalue_shares = 0.0  # shares where we could compute the revalued tax
    for lot in complete_lots:
        lot_gross_sim = lot.shares * sim_price
        lot_tax_sim = lot_gross_sim - lot.net_proceeds_usd
        if lot.eligible:
            # Eligible (§102 Capital track): ordinary income is fixed at grant-date FMV
            # and does NOT change with the sale price.  Only the capital income shifts.
            # ΔEmbedded_tax = §102-rate × Δprice × shares.
            #
            # CLAMP (blocker 3): when the price falls, tax can only fall as far as the
            # ordinary-income tax floor — a lower price reduces the capital gain but the
            # ordinary-income component (already taxed at the employer level and modelled
            # here as still owed) cannot go below zero.  Clamping prevents tax going
            # negative, which would make net proceeds exceed gross (impossible).
            lot_ordinary_tax = _ORDINARY_HIGH_INCOME_RATE * (lot.ordinary_income_usd or 0.0)
            lot_tax_rev = max(
                lot_ordinary_tax,
                lot_tax_sim + _SECTION_102_HIGH_INCOME_RATE * delta_price * lot.shares,
            )
            revalue_embedded_tax += lot_tax_rev
            revalue_shares += lot.shares
        else:
            # Breaking lot: the simulation's NI + income-tax stack was computed on the
            # ordinary income = (sale_price − cost_basis) × shares.  This DOES scale
            # with sale price (via the variable price), but the cost_basis is FIXED.
            # Using the implied rate on gross (lot_tax_sim / lot_gross_sim) understates
            # tax at higher prices when cost_basis > 0, because it attributes the full
            # gross to the tax base rather than (gross − cost_basis × shares).
            #
            # Correct formula: derive r_effective from (tax / ordinary_income_sim),
            # then apply to (ordinary_income_sim + Δprice × shares).  This holds
            # cost_basis fixed across revaluation.
            ordinary_sim = lot.ordinary_income_usd or 0.0
            if ordinary_sim > 0:
                r_effective = lot_tax_sim / ordinary_sim
                # ordinary_income at new price = ordinary_sim + Δprice × shares
                # (because ordinary_income = (price − cost_basis) × shares, cost_basis fixed)
                ordinary_rev = max(0.0, ordinary_sim + delta_price * lot.shares)
                lot_tax_rev = r_effective * ordinary_rev
                revalue_embedded_tax += lot_tax_rev
                revalue_shares += lot.shares
            else:
                # Blocker 3: no ordinary income recorded — REFUSE this lot rather than
                # falling back to the old implied-rate-on-gross formula, which understates
                # tax (absorbs cost_basis into the denominator). Refusing is the conservative
                # choice: we do not fabricate a tax figure from an inconsistent basis.
                # The lot is already in gross_at_sim/net_at_sim (it had net_proceeds_usd),
                # but excluded from the revalued tax and gross figures.
                incomplete_lot_shares += lot.shares

    # Revalue gross/net only over the lots we could actually revalue (internal consistency).
    gross_at_rev = revalue_shares * rev_price
    net_at_rev = gross_at_rev - revalue_embedded_tax

    return LotTaxAggregate(
        simulation_date=sim_date,
        sim_sale_price_usd=sim_price,
        total_shares=total_shares,
        gross_at_sim_usd=gross_at_sim,
        net_at_sim_usd=net_at_sim,
        embedded_tax_at_sim_usd=embedded_tax_at_sim,
        revalue_price_usd=rev_price,
        gross_at_revalue_usd=gross_at_rev,
        net_at_revalue_usd=net_at_rev,
        embedded_tax_at_revalue_usd=revalue_embedded_tax,
        uses_current_price=True,
        incomplete_lot_shares=incomplete_lot_shares,
    )
