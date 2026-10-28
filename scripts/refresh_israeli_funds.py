"""Repair missing broker identities and refresh Israeli fund marks.

Dry-run by default; --apply appends a guarded snapshot, never edits history.
The supplied export verifies identity/quantity, NOT today's price. Other
holdings, cash, dates and quantities stay unchanged. Uses the shared live path.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from argosy.adapters.brokers.leumi_portfolio_xls import parse_portfolio_xls
from argosy.config import get_settings
from argosy.ingest.tsv import PortfolioPosition
from argosy.services.israeli_fund_marks import enrich_from_broker, is_israeli_fund, reprice_fund_position
from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row, persist_snapshot, row_to_snapshot
from argosy.services.snapshot_refresh import _recompute_allocations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--user-id", default="ariel")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    broker = parse_portfolio_xls(args.export)
    if not broker.reconciles():
        raise ValueError("Broker export does not reconcile")
    engine = create_engine(get_settings().database_url.replace("+aiosqlite", ""))
    with Session(engine, expire_on_commit=False) as session:
        row = get_latest_snapshot_row(session, args.user_id)
        if row is None:
            raise ValueError("No current snapshot")
        snap = row_to_snapshot(row)
        prior_id = row.id
        positions = enrich_from_broker(snap.positions, broker.holdings, source=str(args.export.resolve()))
        changed = []
        for i, p in enumerate(positions):
            if not is_israeli_fund(p.model_dump()):
                continue
            priced = reprice_fund_position(p.model_dump(), fx_usd_nis=snap.fx_usd_nis, today=date.today())
            if priced is None:
                raise ValueError(f"No eligible live NAV for {p.security_id}; nothing written")
            positions[i] = PortfolioPosition(**priced)
            changed.append(p.security_id)
            print(f'{p.security_id} | {p.symbol} | NAV {priced["current_price"]:.4f} ILS '
                  f'| dated {priced["valued_as_of"]} | quantity {p.shares:g} '
                  f'| {"AGING" if priced["mark_stale"] else "FRESH"} | {priced["price_source"]}')
        if not changed:
            raise ValueError("No matching Israeli fund holdings; nothing written")
        assert [p.shares for p in positions] == [p.shares for p in snap.positions]
        snap.positions = positions
        snap.allocations = _recompute_allocations(positions, snap.allocations)
        snap.source_path = f"israeli-fund-nav:identity-from-snapshot-{prior_id}"
        # Do not promote the observation date of unrelated broker holdings.
        if args.apply:
            stored = persist_snapshot(session, user_id=args.user_id, snapshot=snap,
                                      expected_prior_id=prior_id, commit=True)
            print(f"APPLIED snapshot={stored.id}; funds={len(changed)}; quantities unchanged")
        else:
            print(f"DRY RUN snapshot={prior_id}; funds={len(changed)}; no database changes")
    engine.dispose()


if __name__ == "__main__":
    main()
