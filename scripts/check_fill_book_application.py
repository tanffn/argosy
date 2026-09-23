"""Production receipt-to-book replay on a migrated disposable live-DB copy.

No broker, network or live financial writes. Executions/settlements and the
Leumi test proposal are EXPLICITLY SYNTHETIC, not actual approved trades.
Also checks the unmodified historical approved order's actual custody gap.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def migrate(path: Path, action: str, revision: str):
    environment = dict(os.environ, ARGOSY_ALEMBIC_URL=f"sqlite+aiosqlite:///{path.as_posix()}", PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-m", "alembic", action, revision], cwd=ROOT, env=environment,
                            capture_output=True, text=True, encoding="utf-8",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=90)
    if result.returncode:
        raise RuntimeError(result.stderr[-2500:])


async def replay(path: Path):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from argosy.execution.manual_fills import record_manual_fill
    from argosy.execution.settlement import FillSettlement
    from argosy.services.portfolio_snapshot_store import (
        get_latest_snapshot_row,
        persist_snapshot,
        row_to_snapshot,
    )
    from argosy.state.models import FillBookApplication, PortfolioSnapshotRow, Proposal

    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            original_row = await session.run_sync(lambda db: get_latest_snapshot_row(db, "ariel"))
            original = row_to_snapshot(original_row)
            historical = await session.get(Proposal, 40)
            assert historical.ticker == "CSPX" and historical.status == "approved" and historical.account_id == "schwab"
            when = datetime.now(UTC)
            receipt = await record_manual_fill(session, user_id="ariel", proposal_id=historical.id,
                broker_order_id="SIMULATED-COPY-HISTORICAL", external_fill_id="SIMULATED-COPY-HISTORICAL",
                quantity=1, price=float(historical.limit_price), commission=0, filled_at=when,
                settlement=FillSettlement(currency="USD", tax_withheld=0,
                    net_cash_delta=-historical.limit_price, reference="SIMULATED-COPY-ONLY", listing_symbol="CSPX.L"))
            assert receipt.book_status == "needs_reconciliation" and receipt.applied_snapshot_id is None
            assert (await session.run_sync(lambda db: get_latest_snapshot_row(db, "ariel"))).id == original_row.id

            held = next(p for p in original.positions if p.symbol == "GOOG" and p.location == "Leumi")
            cash = next(p for p in original.positions if p.location == "Leumi" and p.currency == "USD" and p.asset_type == "Cash")
            price = round(held.current_price, 4)
            # Explicit simulation fixture, NOT a new user approval or a claim
            # that this is a fleet-authored recommendation.
            proposal = Proposal(user_id="ariel", ticker=held.symbol, action="buy", size_shares_or_currency=2,
                size_units="shares", instrument="stock", order_type="limit", limit_price=price, tier="T2",
                account_class="main", account_id="Leumi", status="approved", source="SIMULATED-COPY-ONLY",
                rationale_summary="Synthetic receipt/book integration test", confidence="LOW")
            session.add(proposal)
            await session.commit()
            args = dict(user_id="ariel", proposal_id=proposal.id, broker_order_id="SIMULATED-COPY-ORDER",
                        quantity=1, price=price, commission=1, filled_at=when,
                        settlement=FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=-price-1,
                                                  reference="SIMULATED-COPY-SETTLEMENT"))
            first = await record_manual_fill(session, **args, external_fill_id="SIMULATED-COPY-A")
            again = await record_manual_fill(session, **args, external_fill_id="SIMULATED-COPY-A")
            second = await record_manual_fill(session, **args, external_fill_id="SIMULATED-COPY-B")
            assert first.book_status == second.book_status == "applied" and not again.created
            assert first.applied_snapshot_id == again.applied_snapshot_id
            latest = await session.run_sync(lambda db: get_latest_snapshot_row(db, "ariel"))
            actual = row_to_snapshot(latest)
            assert next(p for p in actual.positions if p.symbol == "GOOG" and p.location == "Leumi").shares == held.shares + 2
            after_cash = next(p for p in actual.positions if p.location == "Leumi" and p.currency == "USD" and p.asset_type == "Cash")
            assert abs(after_cash.current_value_local - (cash.current_value_local - 2 * (price + 1))) < 1e-8
            assert abs(actual.total_usd_value_k - (original.total_usd_value_k - .002)) < 1e-8
            def untouched(snap):
                return [(p.symbol, p.location, p.currency, p.shares, p.current_value_local)
                        for p in snap.positions if p.location != "Leumi" or p.currency != "USD"]
            assert untouched(original) == untouched(actual)
            old_statement = original.model_copy(deep=True)
            old_statement.source_path = "broker:SIMULATED-OLD-STATEMENT"
            old_statement.snapshot_date = when.date() - timedelta(days=1)
            try:
                await session.run_sync(lambda db: persist_snapshot(db, user_id="ariel", snapshot=old_statement))
            except ValueError as exc:
                assert "statement may predate" in str(exc)
                await session.rollback()
            else:
                raise AssertionError("Older statement erased the applied receipts")
            new_statement = actual.model_copy(deep=True)
            new_statement.source_path = "broker:SIMULATED-LATER-STATEMENT"
            new_statement.snapshot_date = when.date() + timedelta(days=2)
            await session.run_sync(lambda db: persist_snapshot(db, user_id="ariel", snapshot=new_statement))
            before_retry = await session.scalar(select(func.count()).select_from(PortfolioSnapshotRow))
            await record_manual_fill(session, **args, external_fill_id="SIMULATED-COPY-B")
            assert await session.scalar(select(func.count()).select_from(PortfolioSnapshotRow)) == before_retry
            assert await session.scalar(select(func.count()).select_from(FillBookApplication).where(
                FillBookApplication.applied_snapshot_id.is_not(None))) == 2
            return {"base_snapshot_id": original_row.id, "migration_roundtrip": True,
                    "unmodified_historical_order_book_status": receipt.book_status,
                    "unmodified_historical_order_reason": receipt.book_reason,
                    "synthetic_receipts_applied": 2, "duplicate_did_not_reapply": True,
                    "shares_added": 2, "cash_debited": round(2*(price+1), 4), "fees_usd": 2,
                    "other_accounts_unchanged": True, "older_statement_rejected": True,
                    "later_statement_retry_did_not_reapply": True, "live_db_modified": False,
                    "actual_execution_proven": False, "full_e2e_proven": False}
    finally:
        await engine.dispose()


def main():
    from argosy.config import get_settings

    source = get_settings().db_file.resolve()
    with tempfile.TemporaryDirectory(prefix="argosy-fill-application-") as directory:
        copy = Path(directory) / "audit.db"
        with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as live:
            with closing(sqlite3.connect(copy)) as target:
                live.backup(target)
        migrate(copy, "upgrade", "head")
        migrate(copy, "downgrade", "0121_prediction_data_recovery")
        migrate(copy, "upgrade", "head")
        result = asyncio.run(replay(copy))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
