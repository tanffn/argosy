"""Replay simulated receipts against real saved orders in a disposable DB copy.

No broker, network, approvals, or live database writes. This verifies the
production manual-fill/telemetry/audit path, NOT that a real trade occurred.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def replay(path: Path):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from argosy.execution.manual_fills import record_manual_fill
    from argosy.services.order_sheet import OrderSheet
    from argosy.services.order_sheet_audit import audit_order_sheet
    from argosy.services.order_sheet_materializer import (
        linked_order_sheet_proposals,
        materialized_order_sheet_errors,
    )
    from argosy.state.models import ActionProposal, Fill, Prediction

    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            parents = (await session.execute(select(ActionProposal).where(
                ActionProposal.user_id == "ariel", ActionProposal.status == "accepted",
                ActionProposal.dedup_key == "period_directive:ariel",
            ).order_by(ActionProposal.surfaced_at.desc(), ActionProposal.id.desc()))).scalars().all()
            for parent in parents:
                payload = json.loads(parent.suggested_payload)
                if payload.get("order_sheet", {}).get("lines"):
                    sheet = OrderSheet.model_validate(payload["order_sheet"])
                    break
            else:
                raise RuntimeError("No accepted nonempty sheet available for replay")
            fingerprint = payload["order_sheet_fingerprint"]
            rows = await session.run_sync(lambda db: linked_order_sheet_proposals(db, sheet))
            assert not await session.run_sync(lambda db: materialized_order_sheet_errors(db, sheet, rows))
            proposal = next(row for row in rows if row.status == "approved"
                            and row.account_id.startswith(("schwab", "leumi")))
            existing = (await session.execute(select(Fill).where(Fill.proposal_id == proposal.id))).scalars().all()
            if existing:
                raise RuntimeError("Chosen proposal already has receipts; do not blend replay with actual fills")
            forecasts = (await session.execute(select(Prediction).where(
                Prediction.user_id == sheet.user_id, Prediction.source == "signal_stream:order_sheet",
            ))).scalars().all()
            linked = [p for p in forecasts if json.loads(p.source_ref).get("order_sheet_fingerprint") == fingerprint
                      and p.ticker == proposal.ticker]
            assert len(linked) == 3, "Expected authored, six-month, and one-year clocks"
            def contract(p):
                return (p.entry_price, p.event_at, p.evaluation_due_at, p.timeframe_days, p.evaluation_method)
            before = {p.id: contract(p) for p in forecasts}
            quantity = Decimal(str(proposal.size_shares_or_currency))
            partial = (quantity / 2).quantize(Decimal("0.0001"))
            price = float(proposal.limit_price)
            arguments = dict(user_id=sheet.user_id, proposal_id=proposal.id,
                             broker_order_id="SIMULATED-COPY-ONLY-ORDER", price=price, commission=1)
            first = await record_manual_fill(session, **arguments,
                                             external_fill_id="SIMULATED-COPY-ONLY-A", quantity=float(partial))
            again = await record_manual_fill(session, **arguments,
                                             external_fill_id="SIMULATED-COPY-ONLY-A", quantity=float(partial))
            second = await record_manual_fill(session, **arguments,
                                              external_fill_id="SIMULATED-COPY-ONLY-B", quantity=float(quantity-partial))
            assert first.pending_status == "partial" and not again.created and second.pending_status == "filled"
            for p in forecasts:
                await session.refresh(p)
                assert contract(p) == before[p.id], "Recommendation scoring contract was rewritten"
            assert all(json.loads(p.source_ref)["fill_telemetry"]["quantity"] == float(quantity) for p in linked)
            audit = await session.run_sync(lambda db: audit_order_sheet(db, user_id=sheet.user_id, fingerprint=fingerprint))
            line = next(line for line in audit.lines if line.proposal_id == proposal.id)
            assert line.filled_shares == float(quantity) and not line.receipt_errors
            assert line.prediction_id in {p.id for p in linked}
            return {"directive_id": parent.id, "proposal_id": proposal.id,
                    "simulated_receipts": 2, "idempotent_duplicate": True,
                    "linked_forecast_horizons": len(linked), "recommendation_contracts_unchanged": len(forecasts),
                    "audit_filled_quantity": line.filled_shares, "receipt_errors": line.receipt_errors,
                    "live_db_modified": False, "execution_proven": False}
    finally:
        await engine.dispose()


def main():
    from argosy.config import get_settings

    source = get_settings().db_file.resolve()
    with tempfile.TemporaryDirectory(prefix="argosy-fill-replay-") as directory:
        copy = Path(directory) / "audit.db"
        with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as live:
            with closing(sqlite3.connect(copy)) as target:
                live.backup(target)
        result = asyncio.run(replay(copy))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
