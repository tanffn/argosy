"""Audit real saved orders; optionally fault-replay on a disposable SQLite copy.

No approvals, fills, broker calls or writes to the live financial database.
This proves materialization consistency, not execution or investment quality.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def inspect(engine, user_id, *, replay=False):
    from sqlalchemy import delete, select
    from sqlalchemy.orm import Session

    from argosy.services.order_sheet import OrderAction, OrderSheet
    from argosy.services.order_sheet_materializer import (
        linked_order_sheet_proposals,
        materialize_order_sheet,
        materialized_order_sheet_errors,
    )
    from argosy.state.models import ActionProposal, Proposal

    with Session(engine) as session:
        candidates = session.scalars(select(ActionProposal).where(
            ActionProposal.user_id == user_id,
            ActionProposal.dedup_key == f"period_directive:{user_id}",
            ActionProposal.status == "accepted",
        ).order_by(ActionProposal.surfaced_at.desc(), ActionProposal.id.desc())).all()
        for parent in candidates:
            payload = json.loads(parent.suggested_payload)
            if payload.get("order_sheet", {}).get("lines"):
                sheet = OrderSheet.model_validate(payload["order_sheet"])
                break
        else:
            raise RuntimeError("No accepted nonempty saved sheet; real materialization is unproven")
        rows = linked_order_sheet_proposals(session, sheet)
        errors = materialized_order_sheet_errors(session, sheet, rows)
        result = {"directive_id": parent.id, "order_rows": len(rows), "consistency_errors": errors}
        if errors or not replay:
            return result
        ids = [row.id for row in rows]
        accounts = {row.ticker: row.account_id for row in rows}
        buy_accounts = {accounts[line.symbol] for line in sheet.lines if line.action in (OrderAction.BUY, OrderAction.ADD)}
        if len(buy_accounts) > 1:
            raise RuntimeError("Multiple buy accounts require explicit replay routing")
        kwargs = {"funding_account_id": next(iter(buy_accounts), None),
                  "sell_accounts_by_symbol": accounts}
        repeated = materialize_order_sheet(session, sheet, **kwargs)
        assert [row.id for row in repeated] == ids
        rejected = []
        changes = {
            "size_shares_or_currency": float(rows[0].size_shares_or_currency) + 1,
            "limit_price": float(rows[0].limit_price) + 0.01,
            "action": "sell" if rows[0].action == "buy" else "buy",
            "account_id": "leumi_replay_different_account",
            "source": "manual",
            "expected_impact_json": "{}",
            "user_id": "materialization_replay_invalid_owner",
        }
        for field, value in changes.items():
            savepoint = session.begin_nested()
            try:
                setattr(rows[0], field, value)
                session.flush()
                try:
                    materialize_order_sheet(session, sheet, **kwargs)
                except ValueError as exc:
                    if "materialization mismatch" not in str(exc):
                        raise
                    rejected.append(field)
                else:
                    raise AssertionError(f"Same-count drift went undetected: {field}")
            finally:
                savepoint.rollback()
        savepoint = session.begin_nested()
        try:
            session.execute(delete(Proposal).where(Proposal.id.in_(ids)))
            try:
                materialize_order_sheet(session, sheet, **kwargs)
            except ValueError as exc:
                if "order rows are missing" not in str(exc):
                    raise
                rejected.append("all_order_rows_deleted")
            else:
                raise AssertionError("Deleted orders were silently rematerialized")
        finally:
            savepoint.rollback()
        assert not materialized_order_sheet_errors(session, sheet, linked_order_sheet_proposals(session, sheet))
        result.update(idempotent_proposal_ids=ids, injected_drift_rejected=rejected)
        return result


def main():
    from sqlalchemy import create_engine

    from argosy.config import get_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="ariel")
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    source = get_settings().db_file.resolve()
    if args.replay:
        with tempfile.TemporaryDirectory(prefix="argosy-materialization-") as directory:
            copy = Path(directory) / "audit.db"
            with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as live:
                with closing(sqlite3.connect(copy)) as target:
                    live.backup(target)
            engine = create_engine(f"sqlite:///{copy.as_posix()}")
            try:
                result = inspect(engine, args.user_id, replay=True)
            finally:
                engine.dispose()
    else:
        engine = create_engine(f"sqlite:///file:{source.as_posix()}?mode=ro&uri=true")
        try:
            result = inspect(engine, args.user_id)
        finally:
            engine.dispose()
    print(json.dumps({**result, "live_db_modified": False, "execution_proven": False}))
    return int(bool(result["consistency_errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
