# argosy/services/tax_simulation_ingest.py
"""Persist a parsed RSU/ESPP simulated tax report and expose eligibility to the planner.

Idempotent per (user_id, simulation_date): re-ingesting a report replaces that report's
lots. The latest ingested report (by ingested_at) is the one the derivation reads, so the
NVDA deconcentration schedule reflects how many shares are capital-track-eligible NOW.
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from argosy.services.tax_simulation_parser import TaxSimReport, parse_workbook
from argosy.state.models import TaxSimulationLot


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
