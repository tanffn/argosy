"""Real DB/API seam for read-only-broker fill receipts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from argosy.execution.manual_fills import record_manual_fill
from argosy.state import db as db_mod
from argosy.state.models import (
    AuditLog,
    Fill,
    PendingOrder,
    Prediction,
    Proposal,
    ProposalHistory,
    User,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("fee", [0, 1])
async def test_omitted_manual_fee_stays_unknown_until_explicit_confirmation(engine, fee):
    from argosy.api.routes.execution import ManualFillRequest
    from argosy.execution.settlement import FillSettlement
    from argosy.state.models import PortfolioSnapshotRow

    proposal_id, _ = await _seed_proposal()
    await _seed_receipt_book()
    request = ManualFillRequest(broker_order_id="unknown-fee", external_fill_id="unknown-fee",
        quantity=10, price=190, filled_at=datetime.now(UTC),
        settlement=FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=1900-fee, reference="fee-report"))
    assert request.commission is None
    args = dict(proposal_id=proposal_id, **request.model_dump())
    args["settlement"] = request.settlement
    async with db_mod.get_session() as session:
        first = await record_manual_fill(session, **args)
        repeated = await record_manual_fill(session, **args)
        assert not repeated.created and repeated.book_status == "needs_reconciliation"
        assert "commission is unknown" in repeated.book_reason
        assert not (await session.get(Fill, first.fill_id)).commission_confirmed
        assert len(list(await session.scalars(select(PortfolioSnapshotRow)))) == 1
        confirmed = await record_manual_fill(session, **(args | {"commission": fee}))
        assert confirmed.book_status == "applied" and not confirmed.created
        omitted_again = await record_manual_fill(session, **args)
        assert omitted_again.applied_snapshot_id == confirmed.applied_snapshot_id
        assert len(list(await session.scalars(select(PortfolioSnapshotRow)))) == 2
        assert len(list(await session.scalars(select(AuditLog).where(
            AuditLog.event_type == "fill.broker_metadata_confirmed")))) == 1


async def _seed_receipt_book(account="schwab_rsu"):
    from argosy.ingest.tsv import PortfolioPosition, PortfolioSnapshot
    from argosy.services.portfolio_snapshot_store import persist_snapshot

    async with db_mod.get_session() as session:
        return await session.run_sync(lambda db: persist_snapshot(db, user_id="ariel", snapshot=PortfolioSnapshot(
            source_path="broker:TEST-statement", snapshot_date=(datetime.now(UTC) - timedelta(days=2)).date(),
            positions=[
                PortfolioPosition(location=account, currency="USD", symbol="NVDA", asset_type="NVIDIA",
                                  shares=100, current_price=190, current_value_local=19000, usd_value_k=19),
                PortfolioPosition(location=account, currency="USD", symbol="", asset_type="Cash",
                                  current_value_local=10000, usd_value_k=10),
            ],
        )))


@pytest.mark.asyncio
async def test_settled_manual_receipt_updates_book_once_and_can_enrich_retry(engine, client):
    from argosy.execution.settlement import FillSettlement
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row, row_to_snapshot
    from argosy.state.models import FillBookApplication, PortfolioSnapshotRow

    proposal_id, _ = await _seed_proposal()
    await _seed_receipt_book()
    arguments = dict(user_id="ariel", proposal_id=proposal_id, broker_order_id="order-book",
                     external_fill_id="exec-book", quantity=10, price=190, commission=1,
                     filled_at=datetime.now(UTC))
    facts = FillSettlement(currency="USD", tax_withheld=25, net_cash_delta=1874, reference="broker-statement:execution")
    async with db_mod.get_session() as session:
        first = await record_manual_fill(session, **arguments)
        assert first.book_status == "needs_settlement" and first.applied_snapshot_id is None
        second = await record_manual_fill(session, **arguments, settlement=facts)
        assert not second.created and second.book_status == "applied"
        third = await record_manual_fill(session, **arguments, settlement=facts)
        assert third.applied_snapshot_id == second.applied_snapshot_id
        row = await session.run_sync(lambda db: get_latest_snapshot_row(db, "ariel"))
        book = row_to_snapshot(row)
        assert next(p for p in book.positions if p.symbol == "NVDA").shares == 90
        assert next(p for p in book.positions if p.asset_type == "Cash").current_value_local == 11874
        assert book.total_usd_value_k == pytest.approx(28.974)
        assert len(list(await session.scalars(select(PortfolioSnapshotRow)))) == 2
        assert len(list(await session.scalars(select(FillBookApplication)))) == 1
        displayed = await client.get("/api/fills", params={"user_id": "ariel", "proposal_id": proposal_id})
        assert displayed.status_code == 200
        saved = displayed.json()["rows"][0]
        assert saved["settlement"]["reference"] == facts.reference
        assert saved["settlement"]["currency"] == "USD"
        assert saved["book_status"] == "applied" and saved["applied_snapshot_id"] == second.applied_snapshot_id
        assert saved["execution_time_confirmed"] and saved["filled_at"].endswith("+00:00")
        with pytest.raises(ValueError, match="settlement facts conflict"):
            await record_manual_fill(session, **arguments, settlement=facts.model_copy(update={"reference": "changed"}))
        await session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["old", "same_day", "wrong_account", "bad_net"])
async def test_unresolved_settlement_keeps_receipt_without_changing_book(engine, case):
    from argosy.execution.settlement import FillSettlement
    from argosy.state.models import PortfolioSnapshotRow

    proposal_id, _ = await _seed_proposal()
    baseline = await _seed_receipt_book("schwab 876" if case == "wrong_account" else "schwab_rsu")
    when = datetime.now(UTC) - timedelta(days=3 if case == "old" else 2 if case == "same_day" else 0)
    async with db_mod.get_session() as session:
        result = await record_manual_fill(session, user_id="ariel", proposal_id=proposal_id,
            broker_order_id="order-review", external_fill_id="exec-review", quantity=10, price=190, commission=0,
            filled_at=when, settlement=FillSettlement(currency="USD", tax_withheld=0,
                net_cash_delta=123 if case == "bad_net" else 1900, reference="broker:settlement"))
        assert result.created and result.book_status == "needs_reconciliation"
        assert result.applied_snapshot_id is None
        assert [r.id for r in await session.scalars(select(PortfolioSnapshotRow))] == [baseline.id]
        assert len(list(await session.scalars(select(Fill)))) == 1


@pytest.mark.asyncio
async def test_receipt_and_book_application_rollback_together(engine):
    from argosy.adapters.brokers.types import Fill as BrokerFill
    from argosy.execution.reconcile import persist_broker_fill
    from argosy.execution.settlement import FillSettlement
    from argosy.state.models import FillBookApplication, PortfolioSnapshotRow

    proposal_id, _ = await _seed_proposal()
    baseline = await _seed_receipt_book()
    async with db_mod.get_session() as session:
        assert await persist_broker_fill(session, user_id="ariel", proposal_id=proposal_id,
            account_id="schwab_rsu", fill=BrokerFill(proposal_id=proposal_id, broker="schwab_csv",
                broker_order_id="rollback-order", external_fill_id="rollback-exec", account_id="schwab_rsu",
                ticker="NVDA", action="sell", quantity=10, price=190, commission=0, filled_at=datetime.now(UTC),
                price_currency="USD", commission_currency="USD",
                settlement=FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=1900, reference="broker:receipt")))
        assert (await session.scalar(select(FillBookApplication))).status == "applied"
        await session.rollback()
    async with db_mod.get_session() as session:
        assert not list(await session.scalars(select(Fill)))
        assert not list(await session.scalars(select(FillBookApplication)))
        assert [r.id for r in await session.scalars(select(PortfolioSnapshotRow))] == [baseline.id]


@pytest.mark.asyncio
async def test_unknown_execution_time_can_be_confirmed_without_reinventing_receipt(engine):
    from argosy.execution.settlement import FillSettlement

    proposal_id, _ = await _seed_proposal()
    await _seed_receipt_book()
    args = dict(user_id="ariel", proposal_id=proposal_id, broker_order_id="time-order",
                external_fill_id="time-exec", quantity=10, price=190, commission=0)
    async with db_mod.get_session() as session:
        first = await record_manual_fill(session, **args)
        row = await session.get(Fill, first.fill_id)
        assert not row.execution_time_confirmed
        actual = datetime.now(UTC) - timedelta(hours=1)
        second = await record_manual_fill(session, **args, filled_at=actual,
            settlement=FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=1900, reference="broker:time"))
        assert not second.created and second.book_status == "applied"
        assert row.execution_time_confirmed and row.filled_at == actual
        assert len(list(await session.scalars(select(AuditLog).where(AuditLog.event_type == "fill.execution_time_confirmed")))) == 1


@pytest.mark.asyncio
async def test_utc_midnight_cannot_make_same_us_statement_day_eligible(engine):
    from argosy.execution.settlement import FillSettlement

    proposal_id, _ = await _seed_proposal()
    baseline = await _seed_receipt_book()
    # 01:00 UTC on following date is still the statement day in US accounts.
    when = datetime.combine(baseline.snapshot_date + timedelta(days=1), datetime.min.time(), UTC) + timedelta(hours=1)
    async with db_mod.get_session() as session:
        result = await record_manual_fill(session, user_id="ariel", proposal_id=proposal_id,
            broker_order_id="tz-order", external_fill_id="tz-exec", quantity=10, price=190, commission=0, filled_at=when,
            settlement=FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=1900, reference="broker:tz"))
        assert result.book_status == "needs_reconciliation" and "already be included" in result.book_reason


@pytest.mark.asyncio
async def test_older_statement_cannot_erase_already_applied_receipt(engine):
    from argosy.execution.settlement import FillSettlement
    from argosy.services.portfolio_snapshot_store import persist_snapshot, row_to_snapshot

    proposal_id, _ = await _seed_proposal()
    baseline = await _seed_receipt_book()
    when = datetime.now(UTC)
    args = dict(user_id="ariel", proposal_id=proposal_id, broker_order_id="statement-order",
                external_fill_id="statement-exec", quantity=10, price=190, commission=0, filled_at=when,
                settlement=FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=1900, reference="broker:statement"))
    async with db_mod.get_session() as session:
        result = await record_manual_fill(session, **args)
        assert result.book_status == "applied"
        earlier = row_to_snapshot(baseline)
        earlier.snapshot_date = when.date()
        with pytest.raises(ValueError, match="statement may predate"):
            await session.run_sync(lambda db: persist_snapshot(db, user_id="ariel", snapshot=earlier))
        await session.rollback()
        retry = await record_manual_fill(session, **args)
        assert retry.applied_snapshot_id == result.applied_snapshot_id


@pytest.mark.asyncio
async def test_equal_time_pending_partials_recover_in_stable_receipt_order(engine):
    from argosy.execution.settlement import FillSettlement

    proposal_id, _ = await _seed_proposal()
    await _seed_receipt_book()
    common = dict(user_id="ariel", proposal_id=proposal_id, broker_order_id="same-time", price=190, commission=0,
                  filled_at=datetime.now(UTC))
    async with db_mod.get_session() as session:
        for identity, quantity in (("first", 4), ("second", 6)):
            await record_manual_fill(session, **common, external_fill_id=identity, quantity=quantity)
        for identity, quantity in (("first", 4), ("second", 6)):
            result = await record_manual_fill(session, **common, external_fill_id=identity, quantity=quantity,
                settlement=FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=quantity*190, reference=identity))
            assert result.book_status == "applied"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["schwab_rsu", " schwab_rsu "])
async def test_unknown_time_blocks_later_activity_until_order_is_confirmed(engine, account):
    from argosy.execution.fill_book import recover_fill_applications
    from argosy.execution.settlement import FillSettlement

    proposal_id, _ = await _seed_proposal()
    await _seed_receipt_book()
    common = dict(user_id="ariel", proposal_id=proposal_id, broker_order_id="unknown-order", price=190, commission=0)
    now = datetime.now(UTC)
    async with db_mod.get_session() as session:
        (await session.get(Proposal, proposal_id)).account_id = account
        await session.commit()
        await record_manual_fill(session, **common, external_fill_id="unknown-first", quantity=4)
        later = await record_manual_fill(session, **common, external_fill_id="known-later", quantity=6,
            filled_at=now-timedelta(hours=1), settlement=FillSettlement(currency="USD", tax_withheld=0,
                net_cash_delta=1140, reference="later"))
        assert later.book_status == "needs_reconciliation" and "earlier receipt" in later.book_reason
        first = await record_manual_fill(session, **common, external_fill_id="unknown-first", quantity=4,
            filled_at=now-timedelta(hours=2), settlement=FillSettlement(currency="USD", tax_withheld=0,
                net_cash_delta=760, reference="first"))
        assert first.book_status == "applied"
        recovery = await session.run_sync(lambda db: recover_fill_applications(db, user_id="ariel"))
        assert recovery["applied"] == 1


@pytest.mark.asyncio
async def test_offset_timestamp_survives_new_session_and_idempotent_replay(engine):
    from datetime import timezone

    from argosy.execution.settlement import FillSettlement

    proposal_id, _ = await _seed_proposal()
    await _seed_receipt_book()
    when = datetime.now(UTC).astimezone(timezone(timedelta(hours=3)))
    args = dict(user_id="ariel", proposal_id=proposal_id, broker_order_id="offset-order",
                external_fill_id="offset-exec", quantity=10, price=190, commission=0, filled_at=when,
                settlement=FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=1900, reference="offset"))
    async with db_mod.get_session() as session:
        first = await record_manual_fill(session, **args)
        assert first.book_status == "applied"
    async with db_mod.get_session() as session:
        row = await session.get(Fill, first.fill_id)
        assert row.filled_at.replace(tzinfo=UTC) == when.astimezone(UTC)
        repeated = await record_manual_fill(session, **args)
        assert repeated.applied_snapshot_id == first.applied_snapshot_id and not repeated.created


@pytest.mark.asyncio
async def test_new_listing_can_be_enriched_without_changing_cash_facts(engine):
    from argosy.execution.settlement import FillSettlement
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row, row_to_snapshot

    proposal_id, _ = await _seed_proposal()
    await _seed_receipt_book()
    async with db_mod.get_session() as session:
        proposal = await session.get(Proposal, proposal_id)
        proposal.ticker, proposal.action, proposal.instrument = "CSPX", "buy", "etf"
        await session.commit()
        args = dict(user_id="ariel", proposal_id=proposal_id, broker_order_id="listing-order",
                    external_fill_id="listing-exec", quantity=10, price=190, commission=0, filled_at=datetime.now(UTC))
        facts = FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=-1900, reference="listing")
        first = await record_manual_fill(session, **args, settlement=facts)
        assert first.book_status == "needs_reconciliation" and "verified listing" in first.book_reason
        enriched = await record_manual_fill(session, **args, settlement=facts.model_copy(update={"listing_symbol": "CSPX.L"}))
        assert enriched.book_status == "applied" and not enriched.created
        book = row_to_snapshot(await session.run_sync(lambda db: get_latest_snapshot_row(db, "ariel")))
        added = next(p for p in book.positions if p.symbol == "CSPX")
        assert added.details.endswith("CSPX LN") and added.valued_as_of == args["filled_at"].date()


@pytest.mark.asyncio
async def test_recovery_advances_past_200_unresolved_receipts(engine):
    from argosy.execution.fill_book import _receipt_hash, recover_fill_applications
    from argosy.execution.settlement import FillSettlement
    from argosy.state.models import FillBookApplication

    blocked_proposal, _ = await _seed_proposal(quantity=1000)
    await _seed_receipt_book("Leumi")
    now = datetime.now(UTC)
    facts = FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=190, reference="recovery-fixture")
    async with db_mod.get_session() as session:
        eligible = Proposal(user_id="ariel", ticker="NVDA", action="sell", size_shares_or_currency=1,
            size_units="shares", instrument="stock", order_type="limit", limit_price=190, tier="T2",
            account_class="main", account_id="Leumi", status="approved", source="test-recovery",
            rationale_summary="test", confidence="LOW")
        session.add(eligible)
        await session.flush()
        receipts = [Fill(user_id="ariel", proposal_id=eligible.id if index == 201 else blocked_proposal,
            broker="leumi_tsv" if index == 201 else "schwab_csv", broker_order_id=f"order-{index}",
            external_fill_id=f"exec-{index}", account_id="Leumi" if index == 201 else "schwab_rsu",
            ticker="NVDA", action="sell", quantity=1, price=190, commission=0,
            price_currency="USD", commission_currency="USD",
            filled_at=now, execution_time_confirmed=True, commission_confirmed=True, paper=False) for index in range(202)]
        session.add_all(receipts)
        await session.flush()
        for index, receipt in enumerate(receipts):
            session.add(FillBookApplication(fill_id=receipt.id, user_id="ariel", receipt_hash="corrupt" if index == 0 else _receipt_hash(receipt),
                status="needs_reconciliation", reason="recovery fixture", settlement_json=facts.model_dump_json(),
                updated_at=now-timedelta(days=1 if index == 201 else 2)))
        await session.commit()
        first = await session.run_sync(lambda db: recover_fill_applications(db, user_id="ariel"))
        assert first["examined"] == 200 and first["applied"] == 0
        assert first["errors"] == 1
        second = await session.run_sync(lambda db: recover_fill_applications(db, user_id="ariel"))
        assert second["applied"] == 1
        assert second["errors"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,currency,details,ticker,listing", [
    (" NVDA ", " usd ", "NVDA LN", "NVDA", "NVDA"),
    ("NVDA.L", "USD", "NVDA DE", "NVDA.L", "NVDA.L"),
])
async def test_receipt_uses_same_listing_identity_as_quote_and_book_paths(engine, symbol, currency, details, ticker, listing):
    from argosy.execution.settlement import FillSettlement
    from argosy.state.models import PortfolioSnapshotRow

    proposal_id, _ = await _seed_proposal()
    baseline = await _seed_receipt_book()
    async with db_mod.get_session() as session:
        row = await session.get(PortfolioSnapshotRow, baseline.id)
        positions = json.loads(row.positions_json)
        positions[0].update(symbol=symbol, currency=currency, details=details)
        row.positions_json = json.dumps(positions)
        (await session.get(Proposal, proposal_id)).ticker = ticker
        await session.commit()
        result = await record_manual_fill(session, user_id="ariel", proposal_id=proposal_id,
            broker_order_id="identity-order", external_fill_id="identity-exec", quantity=10, price=190, commission=0,
            filled_at=datetime.now(UTC), settlement=FillSettlement(currency="USD", tax_withheld=0,
                net_cash_delta=1900, reference="identity", listing_symbol=listing))
        assert result.book_status == "needs_reconciliation" and "listing" in result.book_reason
        assert len(list(await session.scalars(select(PortfolioSnapshotRow)))) == 1


@pytest.mark.asyncio
async def test_book_sync_failure_keeps_receipt_but_rolls_back_all_book_changes(engine, monkeypatch):
    from argosy.execution.settlement import FillSettlement
    from argosy.state.models import PortfolioSnapshotRow

    proposal_id, _ = await _seed_proposal()
    await _seed_receipt_book()
    monkeypatch.setattr("argosy.services.holding_books.sync_unmanaged_from_positions", lambda *args, **kw: {"errors": ["injected sync failure"]})
    async with db_mod.get_session() as session:
        result = await record_manual_fill(session, user_id="ariel", proposal_id=proposal_id,
            broker_order_id="sync-order", external_fill_id="sync-exec", quantity=10, price=190, commission=0,
            filled_at=datetime.now(UTC), settlement=FillSettlement(currency="USD", tax_withheld=0,
                net_cash_delta=1900, reference="sync"))
        assert result.book_status == "needs_reconciliation" and "synchronization failed" in result.book_reason
        assert len(list(await session.scalars(select(PortfolioSnapshotRow)))) == 1
        assert len(list(await session.scalars(select(Fill)))) == 1


@pytest.mark.asyncio
async def test_raised_book_failure_after_side_effects_preserves_manual_receipt(engine, monkeypatch):
    from argosy.execution.settlement import FillSettlement
    from argosy.services import holding_books
    from argosy.state.models import PortfolioSnapshotRow

    proposal_id, _ = await _seed_proposal()
    await _seed_receipt_book()
    original = holding_books.sync_unmanaged_from_positions

    def crash_after_sync(*args, **kw):
        original(*args, **kw)
        raise RuntimeError("injected after durable book writes")

    monkeypatch.setattr(holding_books, "sync_unmanaged_from_positions", crash_after_sync)
    async with db_mod.get_session() as session:
        result = await record_manual_fill(session, user_id="ariel", proposal_id=proposal_id,
            broker_order_id="raised-sync", external_fill_id="raised-sync", quantity=10, price=190, commission=0,
            filled_at=datetime.now(UTC), settlement=FillSettlement(currency="USD", tax_withheld=0,
                net_cash_delta=1900, reference="raised-sync"))
        assert result.book_status == "application_error" and "RuntimeError" in result.book_reason
        assert len(list(await session.scalars(select(PortfolioSnapshotRow)))) == 1
        assert len(list(await session.scalars(select(Fill)))) == 1


@pytest.mark.asyncio
async def test_settlement_replay_compares_values_not_json_number_spelling(engine):
    from argosy.execution.settlement import FillSettlement
    from argosy.state.models import FillBookApplication

    proposal_id, _ = await _seed_proposal()
    await _seed_receipt_book()
    args = dict(user_id="ariel", proposal_id=proposal_id, broker_order_id="decimal-order",
                external_fill_id="decimal-exec", quantity=10, price=190, commission=0, filled_at=datetime.now(UTC),
                settlement=FillSettlement(currency="USD", tax_withheld="0.0", net_cash_delta="1900.0000", reference="decimal"))
    async with db_mod.get_session() as session:
        first = await record_manual_fill(session, **args)
        application = await session.get(FillBookApplication, first.fill_id)
        payload = json.loads(application.settlement_json)
        payload.update(tax_withheld=0, net_cash_delta=1900)
        application.settlement_json = json.dumps(payload)
        await session.commit()
        again = await record_manual_fill(session, **args)
        assert again.book_status == "applied" and again.applied_snapshot_id == first.applied_snapshot_id


@pytest.mark.asyncio
async def test_manual_brokers_are_not_polled_and_verified_enrichment_can_resolve_adapter_only_error(engine):
    from argosy.execution.reconcile import ReconcileLoop
    from argosy.execution.settlement import FillSettlement

    proposal_id, _ = await _seed_proposal()
    await _seed_receipt_book()
    args = dict(user_id="ariel", proposal_id=proposal_id, broker_order_id="manual-only",
                external_fill_id="manual-only", quantity=10, price=190, commission=0, filled_at=datetime.now(UTC))
    async with db_mod.get_session() as session:
        first = await record_manual_fill(session, **args)
        pending = await session.get(PendingOrder, first.pending_order_id)
        pending.last_polled_at = datetime.now(UTC)-timedelta(hours=7)
        await session.commit()
    result = await ReconcileLoop(adapter_factory=lambda broker: pytest.fail("Manual broker must not be polled")).tick()
    assert result["pending"] == 0 and result["errors"] == 0
    async with db_mod.get_session() as session:
        pending = await session.get(PendingOrder, first.pending_order_id)
        assert pending.receipt_sync_error is None
        # An old, capability-only barrier can be explicitly retired after the
        # manual path verifies every stored receipt; genuine data errors cannot.
        pending.receipt_sync_error = "broker adapter unavailable"
        await session.commit()
        enriched = await record_manual_fill(session, **args,
            settlement=FillSettlement(currency="USD", tax_withheld=0, net_cash_delta=1900, reference="manual-only"))
        assert enriched.book_status == "applied" and pending.receipt_sync_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_manual_capture_cannot_clear_a_different_pending_custody_account(engine, existing):
    from argosy.execution.settlement import FillSettlement
    from argosy.state.models import PortfolioSnapshotRow

    proposal_id, _ = await _seed_proposal()
    await _seed_receipt_book()
    args = dict(user_id="ariel", proposal_id=proposal_id, broker_order_id="custody-order",
                external_fill_id="custody-exec", quantity=10, price=190, commission=0, filled_at=datetime.now(UTC))
    async with db_mod.get_session() as session:
        if existing:
            first = await record_manual_fill(session, **args)
            pending = await session.get(PendingOrder, first.pending_order_id)
        else:
            pending = PendingOrder(user_id="ariel", proposal_id=proposal_id, broker="schwab_csv",
                broker_order_id="custody-order", status="submitted", account_id="schwab_rsu")
            session.add(pending)
        pending.account_id, pending.receipt_sync_error = "schwab_other", "broker adapter unavailable"
        await session.commit()
        with pytest.raises(ValueError, match="custody|different broker"):
            await record_manual_fill(session, **args, settlement=FillSettlement(currency="USD", tax_withheld=0,
                net_cash_delta=1900, reference="custody"))
        await session.rollback()
        assert (await session.scalar(select(PendingOrder))).receipt_sync_error == "broker adapter unavailable"
        assert len(list(await session.scalars(select(Fill)))) == int(existing)
        assert len(list(await session.scalars(select(PortfolioSnapshotRow)))) == 1


async def _seed_proposal(*, quantity: float = 10.0) -> tuple[int, int]:
    now = datetime.now(UTC) - timedelta(days=1)
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        proposal = Proposal(
            user_id="ariel",
            ticker="NVDA",
            action="sell",
            size_shares_or_currency=quantity,
            size_units="shares",
            instrument="stock",
            order_type="limit",
            limit_price=190,
            tier="T2",
            account_class="main",
            account_id="schwab_rsu",
            status="approved",
            rationale_summary="fund a higher-conviction order-sheet line",
            expected_impact_json=json.dumps({"order_sheet_fingerprint": "f" * 64,
                                             "order_line": {"action": "TRIM"}}),
            confidence="MEDIUM",
            source="order_sheet",
        )
        session.add(proposal)
        await session.flush()
        prediction = Prediction(
            user_id="ariel",
            source="signal_stream:order_sheet",
            source_ref=json.dumps(
                {"proposal_id": proposal.id, "action": "TRIM", "order_sheet_fingerprint": "f" * 64}, sort_keys=True
            ),
            ticker="NVDA",
            direction="short",
            entry_price=Decimal("190"),
            timeframe_days=30,
            message_id=f"manual-fill-test-{proposal.id}",
            event_at=now,
            evaluation_due_at=now + timedelta(days=30),
            evaluation_method="order_sheet_due_date_v1",
        )
        session.add(prediction)
        await session.commit()
        return proposal.id, prediction.id


@pytest.mark.asyncio
@pytest.mark.real_seam
async def test_manual_fill_closes_execution_and_calibration_loop(
    engine: None, client: AsyncClient
) -> None:
    proposal_id, prediction_id = await _seed_proposal()
    filled_at = datetime.now(UTC).replace(microsecond=0)

    response = await client.post(
        f"/api/proposals/{proposal_id}/manual-fill",
        json={
            "user_id": "ariel",
            "broker_order_id": "SCHWAB-ORDER-71",
            "external_fill_id": "SCHWAB-EXEC-71",
            "quantity": 10,
            "price": 187.25,
            "commission": 1.5,
            "filled_at": filled_at.isoformat(),
            "settlement": {"currency": "USD", "tax_withheld": 0, "net_cash_delta": 1871,
                           "reference": "test-broker-report"},
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True
    assert body["broker"] == "schwab_csv"
    assert body["account_id"] == "schwab_rsu"
    assert body["proposal_status"] == "executed_live"
    assert body["pending_status"] == "filled"

    async with db_mod.get_session() as session:
        proposal = await session.get(Proposal, proposal_id)
        prediction = await session.get(Prediction, prediction_id)
        fills = (
            await session.execute(select(Fill).where(Fill.proposal_id == proposal_id))
        ).scalars().all()
        pending = (
            await session.execute(
                select(PendingOrder).where(PendingOrder.proposal_id == proposal_id)
            )
        ).scalar_one()
        histories = (
            await session.execute(
                select(ProposalHistory).where(
                    ProposalHistory.proposal_id == proposal_id
                )
            )
        ).scalars().all()
        audit_types = set(
            (
                await session.execute(
                    select(AuditLog.event_type).where(
                        AuditLog.entity_id == str(proposal_id)
                    )
                )
            ).scalars().all()
        )

        assert proposal is not None and proposal.status == "executed_live"
        assert len(fills) == 1
        assert fills[0].ticker == "NVDA"
        assert fills[0].action == "sell"
        assert fills[0].account_id == "schwab_rsu"
        assert fills[0].paper is False
        assert pending.status == "filled"
        assert histories[-1].transitioned_by == "manual_fill_capture"
        assert {"fill.received", "proposal.transition"} <= audit_types
        assert prediction is not None
        assert float(prediction.entry_price) == 190  # Recommendation stays immutable.
        telemetry = json.loads(prediction.source_ref)["fill_telemetry"]
        assert telemetry["quantity"] == 10
        assert telemetry["vwap"] == 187.25
        assert telemetry["commission"] == 1.5


@pytest.mark.asyncio
async def test_manual_partial_fills_are_idempotent_and_update_vwap(
    engine: None, client: AsyncClient
) -> None:
    proposal_id, prediction_id = await _seed_proposal(quantity=10)
    first_payload = {
        "user_id": "ariel",
        "broker_order_id": "SCHWAB-ORDER-72",
        "external_fill_id": "SCHWAB-EXEC-72A",
        "quantity": 4,
        "price": 180,
        "commission": 0,
        "filled_at": datetime.now(UTC).isoformat(),
        "settlement": {"currency": "USD", "tax_withheld": 0, "net_cash_delta": 720,
                       "reference": "partial-a"},
    }
    first = await client.post(
        f"/api/proposals/{proposal_id}/manual-fill", json=first_payload
    )
    replay = await client.post(
        f"/api/proposals/{proposal_id}/manual-fill", json=first_payload
    )
    second = await client.post(
        f"/api/proposals/{proposal_id}/manual-fill",
        json={
            **first_payload,
            "external_fill_id": "SCHWAB-EXEC-72B",
            "quantity": 6,
            "price": 200,
            "settlement": {"currency": "USD", "tax_withheld": 0, "net_cash_delta": 1200,
                           "reference": "partial-b"},
        },
    )

    assert first.status_code == 200 and first.json()["pending_status"] == "partial"
    assert replay.status_code == 200 and replay.json()["created"] is False
    assert second.status_code == 200 and second.json()["pending_status"] == "filled"
    assert second.json()["filled_quantity"] == 10

    async with db_mod.get_session() as session:
        fills = (
            await session.execute(select(Fill).where(Fill.proposal_id == proposal_id))
        ).scalars().all()
        prediction = await session.get(Prediction, prediction_id)
        assert len(fills) == 2
        assert prediction is not None
        assert float(prediction.entry_price) == 190
        assert json.loads(prediction.source_ref)["fill_telemetry"]["vwap"] == 192.0


@pytest.mark.asyncio
async def test_surfaced_forecasts_keep_clocks_and_link_fills_across_all_horizons(engine):
    from argosy.services.action_order_sheet_acceptance import materialize_action_order_sheet
    from argosy.services.jobs.period_directive_daily import _record_surfaced_recommendations
    from argosy.services.order_sheet import OrderSheet
    from argosy.services.order_sheet_audit import audit_order_sheet
    from argosy.state.models import Lot
    from tests.test_e2e_proof import _directive

    def seed(sync):
        sync.add(User(id="ariel"))
        sync.add(Lot(user_id="ariel", account_id="schwab_rsu", ticker="NVDA",
                     quantity=500, cost_basis_usd=20000))
        parent = _directive(sync)
        payload = json.loads(parent.suggested_payload)
        sheet = OrderSheet.model_validate(payload["order_sheet"])
        _record_surfaced_recommendations(sync, proposal_id=parent.id, sheet=sheet,
                                        fingerprint=payload["order_sheet_fingerprint"])
        rows, _ = materialize_action_order_sheet(sync, parent)
        sync.commit()
        return next(row.id for row in rows if row.ticker == "NVDA"), payload["order_sheet_fingerprint"]

    async with db_mod.get_session() as session:
        pid, fingerprint = await session.run_sync(seed)
        before = {row.id: (row.entry_price, row.event_at, row.evaluation_due_at, row.timeframe_days,
                           row.evaluation_method) for row in (await session.execute(select(Prediction))).scalars()}
        result = await record_manual_fill(session, user_id="ariel", proposal_id=pid,
                                         broker_order_id="real-path-order", external_fill_id="real-path-partial",
                                         quantity=40, price=121, commission=1)
        assert result.pending_status == "partial"
        rows = (await session.execute(select(Prediction))).scalars().all()
        assert len(rows) == 6
        for row in rows:
            assert (row.entry_price, row.event_at, row.evaluation_due_at, row.timeframe_days,
                    row.evaluation_method) == before[row.id]
            telemetry = json.loads(row.source_ref).get("fill_telemetry")
            if row.ticker == "NVDA":
                assert telemetry["execution_proposal_id"] == pid
                assert telemetry["quantity"] == 40 and telemetry["vwap"] is None
                assert telemetry["status"] == "execution_details_incomplete"
            else:
                assert telemetry is None
        audit = await session.run_sync(lambda sync: audit_order_sheet(sync, user_id="ariel", fingerprint=fingerprint))
        assert all(line.prediction_id is not None for line in audit.lines)
        assert next(line for line in audit.lines if line.symbol == "NVDA").filled_shares == 40

        from argosy.execution.reconcile import _sync_order_sheet_fill_telemetry
        session.add(User(id="other"))
        wrong_owner = Fill(user_id="other", proposal_id=pid, broker="schwab_csv", broker_order_id="real-path-order",
                           external_fill_id="wrong-owner", account_id="schwab_rsu", ticker="NVDA",
                           action="sell", quantity=1, price=121, paper=False)
        session.add(wrong_owner)
        await session.flush()
        await _sync_order_sheet_fill_telemetry(session, user_id="ariel", proposal_id=pid)
        await session.commit()
        for row in (await session.execute(select(Prediction))).scalars():
            if row.ticker == "NVDA":
                telemetry = json.loads(row.source_ref)["fill_telemetry"]
                assert telemetry["status"] == "reconciliation_required"
                assert any("user_id mismatch" in error for error in telemetry["receipt_errors"])
        # This is a test-only injected receipt; remove it before the independent
        # overfill case below so each defect is independently demonstrated.
        await session.delete(wrong_owner)
        await session.commit()

        # A later real overfill remains a fact but invalidates prior telemetry;
        # it cannot leave the earlier "verified" aggregate looking current.
        from argosy.adapters.brokers.types import Fill as BrokerFill
        from argosy.execution.reconcile import persist_broker_fill
        await persist_broker_fill(session, user_id="ariel", proposal_id=pid, account_id="schwab_rsu",
                                  fill=BrokerFill(broker="schwab_csv", broker_order_id="real-path-order",
                                                  external_fill_id="overfill", account_id="schwab_rsu",
                                                  ticker="NVDA", action="sell", quantity=61, price=121))
        await session.commit()
        for row in (await session.execute(select(Prediction))).scalars():
            assert (row.entry_price, row.event_at, row.evaluation_due_at, row.timeframe_days,
                    row.evaluation_method) == before[row.id]
            if row.ticker == "NVDA":
                telemetry = json.loads(row.source_ref)["fill_telemetry"]
                assert telemetry["status"] == "reconciliation_required"
                assert telemetry["quantity"] == 101 and not telemetry["complete"]
                assert telemetry["receipt_errors"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("paper", True), ("ticker", "OTHER"), ("action", "buy"),
                                        ("account_id", "leumi_main"), ("commission", 2)])
async def test_manual_duplicate_receipt_checks_identity_not_only_id(engine, field, value):
    pid, _ = await _seed_proposal()
    args = dict(user_id="ariel", proposal_id=pid, broker_order_id="order", external_fill_id="execution",
                quantity=10, price=190, commission=1)
    async with db_mod.get_session() as session:
        await record_manual_fill(session, **args)
        receipt = (await session.execute(select(Fill))).scalars().one()
        setattr(receipt, field, value)
        await session.commit()
        with pytest.raises(ValueError, match="different execution facts"):
            await record_manual_fill(session, **args)


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [9.99995, 10.00005])
async def test_manual_fill_rejects_unrepresentable_quantities_before_writing(engine, quantity):
    pid, _ = await _seed_proposal()
    async with db_mod.get_session() as session:
        with pytest.raises(ValueError, match="ledger precision"):
            await record_manual_fill(session, user_id="ariel", proposal_id=pid, broker_order_id="order",
                                     external_fill_id="execution", quantity=quantity, price=190)
        assert not (await session.execute(select(Fill))).scalars().all()


@pytest.mark.asyncio
async def test_manual_partial_does_not_round_up_to_completion(engine):
    pid, _ = await _seed_proposal()
    async with db_mod.get_session() as session:
        args = dict(user_id="ariel", proposal_id=pid, broker_order_id="order", price=190)
        first = await record_manual_fill(session, **args, external_fill_id="first", quantity=9.9999)
        assert first.pending_status == "partial"
        final = await record_manual_fill(session, **args, external_fill_id="last", quantity=0.0001)
        assert final.pending_status == "filled" and final.filled_quantity == 10


@pytest.mark.asyncio
async def test_manual_derived_identity_cannot_create_terminal_order(engine):
    pid, _ = await _seed_proposal()
    async with db_mod.get_session() as session:
        with pytest.raises(ValueError, match="broker-issued"):
            await record_manual_fill(session, user_id="ariel", proposal_id=pid, broker_order_id="order",
                                     external_fill_id="derived:not-a-broker-id", quantity=10, price=190)
        assert not (await session.execute(select(Fill))).scalars().all()
        assert not (await session.execute(select(PendingOrder))).scalars().all()
        assert (await session.get(Proposal, pid)).status == "approved"


@pytest.mark.asyncio
async def test_manual_fill_rejects_unapproved_or_overfilled_proposal(
    engine: None,
) -> None:
    proposal_id, _ = await _seed_proposal(quantity=5)
    async with db_mod.get_session() as session:
        proposal = await session.get(Proposal, proposal_id)
        assert proposal is not None
        proposal.status = "awaiting_human"
        await session.commit()

    async with db_mod.get_session() as session:
        with pytest.raises(ValueError, match="must be approved"):
            await record_manual_fill(
                session,
                user_id="ariel",
                proposal_id=proposal_id,
                broker_order_id="SCHWAB-ORDER-73",
                external_fill_id="SCHWAB-EXEC-73",
                quantity=6,
                price=100,
            )
        await session.rollback()
        proposal = await session.get(Proposal, proposal_id)
        assert proposal is not None
        proposal.status = "approved"
        await session.commit()

    async with db_mod.get_session() as session:
        with pytest.raises(ValueError, match="exceeds approved quantity"):
            await record_manual_fill(
                session,
                user_id="ariel",
                proposal_id=proposal_id,
                broker_order_id="SCHWAB-ORDER-73",
                external_fill_id="SCHWAB-EXEC-73",
                quantity=6,
                price=100,
            )
        await session.rollback()

    async with db_mod.get_session() as session:
        assert (
            await session.execute(select(Fill).where(Fill.proposal_id == proposal_id))
        ).scalars().all() == []
