"""Exercise the UI's real HTTP handlers on a disposable live-book copy.

All receipts/proposal below are explicitly SIMULATED. No broker, live writes,
scheduler or actual approval. ASGI transport calls production route handlers.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def replay(copy: Path):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import func, select

    from argosy.api.routes.execution import router
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row, row_to_snapshot
    from argosy.state import db
    from argosy.state.models import Fill, PortfolioSnapshotRow, Proposal

    db.init_engine(f"sqlite+aiosqlite:///{copy.as_posix()}")
    try:
        async with db.get_session() as session:
            before_row = await session.run_sync(lambda sync: get_latest_snapshot_row(sync, "ariel"))
            before = row_to_snapshot(before_row)
            holding = next(p for p in before.positions if p.location == "Leumi" and p.symbol == "GOOG")
            execution_price = round(holding.current_price, 4)  # Simulated receipt's explicit ledger precision.
            cash = next(p for p in before.positions if p.location == "Leumi" and p.currency == "USD" and p.asset_type == "Cash")
            proposal = Proposal(user_id="ariel", ticker="GOOG", action="buy", size_shares_or_currency=1,
                size_units="shares", instrument="stock", order_type="limit", limit_price=holding.current_price,
                tier="T2", account_class="main", account_id="Leumi", status="approved", source="SIMULATED-COPY-ONLY",
                rationale_summary="Synthetic UI receipt protocol probe", confidence="LOW")
            session.add(proposal)
            await session.commit()
            proposal_id = proposal.id
        app = FastAPI()  # No app lifespan/scheduler: only the actual execution API.
        app.include_router(router, prefix="/api")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://copy") as client:
            path = f"/api/proposals/{proposal_id}/manual-fill"
            body = dict(user_id="ariel", broker_order_id="SIMULATED-UI-ORDER", external_fill_id="SIMULATED-UI-EXEC",
                        quantity=1, price=execution_price, commission=None, filled_at=None, settlement=None)
            first = await client.post(path, json=body)
            assert first.status_code == 200, first.text
            assert first.json()["book_status"] == "needs_settlement"
            listing = await client.get("/api/fills", params={"user_id": "ariel", "proposal_id": proposal_id})
            assert listing.status_code == 200, listing.text
            saved = listing.json()["rows"][0]
            assert saved["commission"] is None and not saved["execution_time_confirmed"] and saved["settlement"] is None
            body.update(commission=1, filled_at=datetime.now(UTC).isoformat(), settlement=dict(
                currency="USD", tax_withheld="0", net_cash_delta=str(-execution_price-1), reference="SIMULATED-UI-STATEMENT"))
            completed = await client.post(path, json=body)
            assert completed.status_code == 200, completed.text
            result = completed.json()
            assert result["book_status"] == "applied" and not result["created"]
            listing = await client.get("/api/fills", params={"user_id": "ariel", "proposal_id": proposal_id})
            saved = listing.json()["rows"][0]
            assert saved["settlement"]["reference"] == "SIMULATED-UI-STATEMENT"
            assert saved["filled_at"].endswith("+00:00") and saved["execution_time_confirmed"]
            assert saved["book_status"] == "applied" and saved["applied_snapshot_id"] == result["applied_snapshot_id"]
            retry = await client.post(path, json=body)
            assert retry.status_code == 200 and retry.json()["applied_snapshot_id"] == result["applied_snapshot_id"]
        async with db.get_session() as session:
            after = row_to_snapshot(await session.run_sync(lambda sync: get_latest_snapshot_row(sync, "ariel")))
            assert next(p for p in after.positions if p.location == "Leumi" and p.symbol == "GOOG").shares == holding.shares+1
            after_cash = next(p for p in after.positions if p.location == "Leumi" and p.currency == "USD" and p.asset_type == "Cash")
            assert abs(after_cash.current_value_local-(cash.current_value_local-execution_price-1)) < .00001
            assert await session.scalar(select(func.count()).select_from(Fill).where(Fill.proposal_id == proposal_id)) == 1
            assert await session.scalar(select(func.count()).select_from(PortfolioSnapshotRow).where(PortfolioSnapshotRow.id > before_row.id)) == 1
        return {"base_snapshot": before_row.id, "production_http_handlers": True, "receipt_saved_unknown": True,
                "same_execution_enriched": True, "settlement_roundtrip": True, "one_book_application": True,
                "cash_debited_usd": execution_price+1, "live_modified": False,
                "actual_execution_proven": False, "full_e2e_proven": False}
    finally:
        await db.dispose_engine()


def main():
    from argosy.config import get_settings

    source = get_settings().db_file.resolve()
    with tempfile.TemporaryDirectory(prefix="argosy-receipt-ui-") as directory:
        copy = Path(directory)/"audit.db"
        with closing(sqlite3.connect(source.as_uri()+"?mode=ro", uri=True)) as live:
            with closing(sqlite3.connect(copy)) as target:
                live.backup(target)
        print(json.dumps(asyncio.run(replay(copy))))


if __name__ == "__main__":
    main()
