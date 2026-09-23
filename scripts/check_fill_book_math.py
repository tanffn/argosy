"""Exercise signed fill accounting on a disposable copy of the real book.

Trades/fees/withholding are explicitly synthetic. No network, broker, approvals,
or live DB writes. This is NOT proof of automatic or exactly-once application.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def replay(path: Path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row, row_to_snapshot
    from argosy.services.snapshot_refresh import (
        Fill,
        _fill_mark_consistent,
        apply_fills_to_snapshot,
    )

    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with Session(engine) as session:
            original_row = get_latest_snapshot_row(session, "ariel")
            original = row_to_snapshot(original_row)
            cash_rows = [p for p in original.positions if p.asset_type == "Cash" and p.currency == "USD"]
            candidates = [p for p in original.positions if p.currency == "USD" and (p.shares or 0) >= 2
                          and (p.current_price or 0) > 0 and p.avg_price is not None
                          and sum(c.location == p.location for c in cash_rows) == 1]
            rejected_units = []
            for candidate in candidates:
                if _fill_mark_consistent(candidate):
                    continue
                try:
                    apply_fills_to_snapshot(session, user_id="ariel", fills=[Fill(
                        symbol=candidate.symbol, shares=1, price=round(candidate.current_price, 4),
                        location=candidate.location, currency="USD")], cash_location=candidate.location,
                        source_tag="fills-applied:SIMULATED-COPY-BAD-UNITS", today=original.snapshot_date)
                except ValueError as exc:
                    assert "price/value units" in str(exc)
                    rejected_units.append(candidate.symbol)
                else:
                    raise AssertionError("Inconsistent mark units were silently accepted")
                assert get_latest_snapshot_row(session, "ariel").id == original_row.id
            held = next(p for p in candidates if p.location == "Leumi" and _fill_mark_consistent(p))
            price = round(float(held.current_price), 4)
            before_shares = held.shares
            cash = next(c for c in cash_rows if c.location == held.location)
            common = dict(symbol=held.symbol, shares=1, price=price, location=held.location, currency="USD")
            buy = apply_fills_to_snapshot(session, user_id="ariel", fills=[Fill(**common, commission=1)],
                                          cash_location=held.location, source_tag="fills-applied:SIMULATED-COPY-BUY",
                                          today=original.snapshot_date)
            assert next(p for p in buy.snapshot.positions if p.symbol == held.symbol
                        and p.location == held.location).shares == before_shares + 1
            apply_fills_to_snapshot(session, user_id="ariel", fills=[Fill(**common, action="sell",
                                                                                commission=2, tax_withheld=5)],
                                           cash_location=held.location, source_tag="fills-applied:SIMULATED-COPY-SELL",
                                           today=original.snapshot_date)
            actual = row_to_snapshot(get_latest_snapshot_row(session, "ariel"))
            after = next(p for p in actual.positions if p.symbol == held.symbol and p.location == held.location)
            after_cash = next(p for p in actual.positions if p.asset_type == "Cash" and p.location == held.location
                              and p.currency == "USD")
            assert after.shares == before_shares
            assert abs(after_cash.current_value_local - (cash.current_value_local - 8)) < 1e-8
            assert abs(actual.total_usd_value_k - (original.total_usd_value_k - .008)) < 1e-8
            # Other accounts/currencies retain their quantities and local cash.
            def untouched(snapshot):
                return [(p.symbol, p.location, p.currency, p.shares, p.current_value_local)
                        for p in snapshot.positions if p.location != held.location or p.currency != "USD"]
            assert untouched(actual) == untouched(original)
            return {"base_snapshot_id": original_row.id, "synthetic_buy_and_sell_symbol": held.symbol,
                    "shares_restored": True, "net_cash_change_usd": -8,
                    "synthetic_commissions_usd": 3, "synthetic_withholding_usd": 5,
                    "inconsistent_units_rejected_without_writes": rejected_units,
                    "other_accounts_and_currencies_unchanged": True, "live_db_modified": False,
                    "automatic_application_proven": False, "exactly_once_proven": False,
                    "execution_proven": False}
    finally:
        engine.dispose()


def main():
    from argosy.config import get_settings

    source = get_settings().db_file.resolve()
    with tempfile.TemporaryDirectory(prefix="argosy-fill-book-") as directory:
        copy = Path(directory) / "audit.db"
        with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as live:
            with closing(sqlite3.connect(copy)) as target:
                live.backup(target)
        result = replay(copy)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
