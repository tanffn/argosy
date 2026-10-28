"""Production ledger/publication/valuation path on real SQLite, no orders."""
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import argosy.services.shadow_portfolio as ledger
from argosy.services.order_sheet import OrderSheet, validate_order_sheet
from argosy.services.order_sheet_materializer import order_sheet_fingerprint
from argosy.services.shadow_daily import run_user
from argosy.state.models import ActionProposal, Base, PortfolioSnapshotRow, Proposal, User
from argosy.state.shadow_models import ShadowEvent

ACTIVATED = datetime(2026, 1, 1, 22, tzinfo=UTC)
PUBLISHED = datetime(2026, 1, 2, 22, tzinfo=UTC)


def authored(shares=10):
    stamp = PUBLISHED.isoformat()
    def voice(action):
        return {"source": "fleet", "verdict": action, "as_of": stamp, "rationale": "Independent fresh judgment"}
    raw = {"user_id": "a", "generated_at": stamp, "horizon_years_min": 1, "horizon_years_max": 5,
        "portfolio_symbols": ["AAA", "NVDA"], "funding": {"new_cash_usd": shares * 10,
            "gross_sell_proceeds_usd": 0, "sell_tax_usd": 0, "sell_costs_usd": 0,
            "reserve_usd": 0, "available_to_buy_usd": shares * 10},
        "lines": [{"symbol": "AAA", "action": "ADD", "shares": shares, "notional_usd": shares * 10,
            "venue": "NYSE", "thesis": "Higher evidenced earning power", "thesis_type": "compounder",
            "falsifier": "Margin recovery fails", "catalyst": {"description": "Earnings", "due_date": "2026-06-01"},
            "expectation": {"statement": "Cash flows improve", "success_measure": "Positive total return", "due_date": "2027-01-01"},
            "evidence": {"price_usd": 10, "price_as_of": stamp, "price_source": "fixture:quote"},
            "stance_source": "portfolio_review", "voices": [voice("ADD")], "post_trade_weight_pct": 50,
            "estate_situs": "non_US"}],
        "no_action": [{"symbol": "NVDA", "reason": "Retain this holding", "voices": [voice("HOLD")]}],
        "rationale": "One conserved list"}
    sheet = OrderSheet.model_validate(raw)
    validation = validate_order_sheet(sheet)
    assert validation.valid, validation
    return {"artifact_type": "validated_order_sheet", "validation_status": "validated",
        "order_sheet": sheet.model_dump(mode="json"), "order_sheet_fingerprint": order_sheet_fingerprint(sheet),
        "execution_accounts": {"AAA": "bank"}, "paper_intent_id": "deployment-one",
        "paper_economics": {"AAA": {"fee_usd": 0}}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'shadow.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        db.add_all([User(id="a"), User(id="b")])
        db.add(PortfolioSnapshotRow(user_id="a", imported_at=ACTIVATED, snapshot_date=ACTIVATED.date(),
            positions_json=json.dumps([
                {"symbol": "AAA", "location": "bank", "currency": "USD", "shares": 100, "usd_value_k": 1},
                {"symbol": "NVDA", "location": "bank", "currency": "USD", "shares": 1, "usd_value_k": .02, "managed": False},
                {"symbol": "", "location": "bank", "currency": "USD", "current_value_local": 1000, "asset_type": "Cash", "usd_value_k": 1},
                {"symbol": "-", "asset_type": "Real estate", "details": "Physical property", "usd_value_k": 10},
            ])))
        db.commit()
        ledger.activate(db, user_id="a", now=ACTIVATED)
        db.commit()
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return PUBLISHED
    monkeypatch.setattr(ledger, "datetime", Clock)
    yield factory
    engine.dispose()


def publish(factory, envelope=None):
    with factory() as db:
        row = ActionProposal(user_id="a", summary="Fixture recommendation", rationale_md="Thesis",
            suggested_payload=json.dumps(envelope or authored()), severity="info", surfaced_at=PUBLISHED,
            expires_at=PUBLISHED + timedelta(days=7), kind="allocate", status="open")
        db.add(row)
        db.commit()
        return row.id


def collected(seed, receipts, **kwargs):
    packets = []
    for day, price, spy in [("2026-01-02", 10, 100), ("2026-01-05", 10, 100), ("2026-01-06", 12, 110)]:
        packets.append({"date": day, "fx": {"USD": "1"}, "actions": [], "marks": {
            symbol: {"price": str(value), "as_of": day, "currency": "USD", "eligible": True, "source": "fixture:raw"}
            for symbol, value in (("AAA", price), ("NVDA", 20), ("SPY", spy))}})
    return packets, {"status": "fixture_evidence"}


@pytest.mark.real_seam
def test_atomic_publication_to_daily_whole_book_projection_and_replay(store):
    proposal_id = publish(store)
    with store() as db:
        receipts = list(db.scalars(select(ShadowEvent).where(ShadowEvent.kind == "sheet")))
        assert len(receipts) == 1
        assert json.loads(receipts[0].payload_json)["validation"]["valid"]
        assert json.loads(receipts[0].payload_json)["publication_id"] == proposal_id
    first = run_user(store, "a", now=datetime(2026, 1, 6, 23, tzinfo=UTC), collector=collected)
    latest = first["report"]["latest"]["books"]["gross"]
    assert float(latest["unchanged"]["nav_usd"]) == 2220
    assert float(latest["argosy"]["nav_usd"]) == 2240
    assert float(latest["spy"]["nav_usd"]) == 2222
    with store() as db:
        previous_count = len(list(db.scalars(select(ShadowEvent))))
    second = run_user(store, "a", now=datetime(2026, 1, 6, 23, tzinfo=UTC), collector=collected)
    assert second["report"] == first["report"]
    with store() as db:
        assert len(list(db.scalars(select(ShadowEvent)))) == previous_count
        assert list(db.scalars(select(Proposal))) == []
        assert db.get(ActionProposal, proposal_id).status == "open"


def test_daily_evaluator_wires_real_shadow_step_and_exposes_failures(store, monkeypatch):
    import asyncio

    import argosy.services.shadow_daily as daily
    import argosy.services.shadow_market as market
    from argosy.orchestrator.loops.predictions_evaluator import PredictionsEvaluatorLoop
    from argosy.services.jobs.summary_status import derive_run_status
    loop = PredictionsEvaluatorLoop(session_factory=store,
        now_fn=lambda: datetime(2026, 1, 6, 23, tzinfo=UTC))
    monkeypatch.setattr(loop, "_run_tick_sync", lambda now: {"stages": {}})
    monkeypatch.setattr(market, "collect", collected)
    summary = asyncio.run(loop.tick())
    assert summary["stages"]["whole_portfolio"]["users"][0]["status"] == "collecting"
    assert derive_run_status(summary)[0] == "ok"
    def broken(*args, **kwargs):
        raise ValueError("Deliberate ledger fault")
    monkeypatch.setattr(daily, "run_batch", broken)
    summary = asyncio.run(loop.tick())
    assert derive_run_status(summary)[0] == "error"
    assert loop.last_output_summary == summary
    shadow_calls = []
    def record_shadow(*args, **kwargs):
        shadow_calls.append(True)
        return {"users": [], "failures": 0}
    monkeypatch.setattr(daily, "run_batch", record_shadow)
    monkeypatch.setattr(loop, "_run_tick_sync", broken)
    with pytest.raises(ValueError, match="Deliberate ledger fault"):
        asyncio.run(loop.tick())
    assert shadow_calls == [True]
    assert loop.last_output_summary["stages"]["evaluator"]["status"] == "error"
    assert loop.last_output_summary["stages"]["whole_portfolio"]["failures"] == 0


def test_baseline_is_frozen_includes_nvda_and_never_silently_rebases(store):
    with store() as db:
        first = ledger.activate(db, user_id="a", now=ACTIVATED)
        db.add(PortfolioSnapshotRow(user_id="a", imported_at=PUBLISHED, positions_json='[]'))
        db.commit()
        second = ledger.activate(db, user_id="a", now=PUBLISHED)
        assert first == second
        assert "bank|NVDA" in first["positions"]
        assert float(first["recorded_liquid_usd"]) == 2020
        assert first["cash"]["bank|USD"]["quantity"] == "1000"
        assert first["excluded"][0]["reason"]
        assert ledger.report(db, "b")["status"] == "not_started"


def test_ledger_events_are_database_immutable(store):
    with store() as db:
        with pytest.raises(IntegrityError, match="append-only"):
            db.execute(text("UPDATE shadow_events SET payload_json='{}'"))
        db.rollback()
        with pytest.raises(IntegrityError, match="append-only"):
            db.execute(text("DELETE FROM shadow_events"))


def test_publication_refresh_preserves_both_artifacts_and_cancels_pending_revision(store):
    proposal_id = publish(store)
    with store() as db:
        row = db.get(ActionProposal, proposal_id)
        row.suggested_payload = json.dumps(authored(shares=20))
        db.commit()
        receipts = [json.loads(r.payload_json) for r in db.scalars(select(ShadowEvent).where(ShadowEvent.kind == "sheet").order_by(ShadowEvent.id))]
        assert len(receipts) == 2
        assert receipts[0]["sheet"]["lines"][0]["shares"] == 10
        assert receipts[1]["sheet"]["lines"][0]["shares"] == 20


def test_read_only_report_does_not_initialize_or_append(store):
    with store() as db:
        before = len(list(db.scalars(select(ShadowEvent))))
        ledger.report(db, "a")
        ledger.report(db, "b")
        assert len(list(db.scalars(select(ShadowEvent)))) == before


def test_economics_only_revision_and_publication_rollback(store):
    proposal_id = publish(store)
    with store() as db:
        row = db.get(ActionProposal, proposal_id)
        changed = authored()
        changed["paper_economics"]["AAA"]["fee_usd"] = 5
        row.suggested_payload = json.dumps(changed)
        db.commit()
        assert len(list(db.scalars(select(ShadowEvent).where(ShadowEvent.kind == "sheet")))) == 2
        changed["paper_economics"]["AAA"]["fee_usd"] = 6
        row.suggested_payload = json.dumps(changed)
        db.flush()
        db.rollback()
        assert len(list(db.scalars(select(ShadowEvent).where(ShadowEvent.kind == "sheet")))) == 2


def test_publication_reversion_is_a_third_revision_and_executes_latest_shares(store):
    proposal_id = publish(store)
    with store() as db:
        row = db.get(ActionProposal, proposal_id)
        row.suggested_payload = json.dumps(authored(shares=20))
        db.commit()
        row.suggested_payload = json.dumps(authored(shares=10))
        db.commit()
        assert len(list(db.scalars(select(ShadowEvent).where(ShadowEvent.kind == "sheet")))) == 3
    result = run_user(store, "a", now=datetime(2026, 1, 6, 23, tzinfo=UTC), collector=collected)
    assert float(result["report"]["latest"]["books"]["gross"]["argosy"]["nav_usd"]) == 2240


def test_market_reversions_are_new_revisions_and_false_actions_can_be_removed(store):
    now = datetime(2026, 1, 6, 23, tzinfo=UTC)
    def provider(price, false_split=False):
        def collect(*args, **kwargs):
            packets, info = collected(*args, **kwargs)
            packets[1]["marks"]["AAA"]["price"] = str(price)
            packets[1]["action_coverage"] = ["AAA"]
            if false_split:
                packets[1]["actions"] = [{"id": "false", "instrument": "AAA", "kind": "split", "ratio": "2"}]
            return packets, info
        return collect
    run_user(store, "a", now=now, collector=provider(10))
    run_user(store, "a", now=now + timedelta(seconds=1), collector=provider(11, True))
    result = run_user(store, "a", now=now + timedelta(seconds=2), collector=provider(10))
    assert float(result["report"]["latest"]["books"]["gross"]["unchanged"]["nav_usd"]) == 2220
    with store() as db:
        rows = [json.loads(r.payload_json) for r in db.scalars(select(ShadowEvent).where(ShadowEvent.kind == "market").order_by(ShadowEvent.id))]
        corrected = [row for row in rows if row["date"] == "2026-01-05"]
        assert len(corrected) == 3
        assert corrected[-1]["actions"] == []
        assert corrected[-1]["revision"] == 3


def test_explicit_external_flow_is_idempotent_and_uses_next_completed_cutoff(store):
    with store() as db:
        args = dict(user_id="a", receipt_id="bank-deposit", account="bank", ccy="USD", amount=100,
                    effective_at=datetime(2026, 1, 3, 12, tzinfo=UTC), evidence_reference="bank:confirmed-receipt")
        first = ledger.record_external_flow(db, **args)
        second = ledger.record_external_flow(db, **args)
        assert first == second
        db.commit()
        flow = db.scalar(select(ShadowEvent).where(ShadowEvent.kind == "flow"))
        assert json.loads(flow.payload_json)["date"] == "2026-01-05"
    result = run_user(store, "a", now=datetime(2026, 1, 6, 23, tzinfo=UTC), collector=collected)
    assert float(result["report"]["latest"]["books"]["gross"]["unchanged"]["nav_usd"]) == 2320


def test_duplicate_native_cash_aggregates_and_foreign_listings_stay_distinct(store):
    with store() as db:
        rows = [{"symbol": "", "asset_type": "Cash", "currency": "NIS", "location": "bank",
                 "current_value_local": value, "usd_value_k": value / 1000} for value in (30, 70)]
        rows += [{"symbol": "IWDP", "details": "IWDP " + venue, "currency": "USD", "location": "bank",
                  "shares": 2, "usd_value_k": .2} for venue in ("LN", "SW")]
        db.add(PortfolioSnapshotRow(user_id="b", imported_at=ACTIVATED, positions_json=json.dumps(rows)))
        db.flush()
        seed = ledger.activate(db, user_id="b", now=ACTIVATED)
        assert seed["cash"]["bank|ILS"]["quantity"] == "100"
        assert set(seed["positions"]) == {"bank|IWDP.L@USD", "bank|IWDP.SW@USD"}


def test_unknown_native_cash_currency_never_becomes_usd(store):
    with store() as db:
        db.add(PortfolioSnapshotRow(user_id="b", imported_at=ACTIVATED, positions_json=json.dumps([
            {"symbol": "", "asset_type": "Cash", "location": "bank", "current_value_local": 1000}])))
        db.flush()
        seed = ledger.activate(db, user_id="b", now=ACTIVATED)
        assert "bank|USD" not in seed["cash"]
        from argosy.services.shadow_accounting import mark, new_book
        assert mark(new_book(seed), {"fx": {"USD": "1"}}, "2026-01-02")["nav_usd"] is None


def test_new_listing_identities_do_not_collapse_by_ticker(store):
    with store() as db:
        db.add(PortfolioSnapshotRow(user_id="b", imported_at=ACTIVATED, positions_json='[]'))
        db.flush()
        ledger.activate(db, user_id="b", now=ACTIVATED)
        db.commit()
        for listing in ("AAA.L", "AAA.SW"):
            raw = authored()
            raw["order_sheet"]["user_id"] = "b"
            raw["order_sheet"]["lines"][0]["evidence"]["price_source"] = "https://finance.yahoo.com/quote/" + listing
            sheet = OrderSheet.model_validate(raw["order_sheet"])
            raw["order_sheet_fingerprint"] = order_sheet_fingerprint(sheet)
            db.add(ActionProposal(user_id="b", summary="New listing", rationale_md="Thesis",
                suggested_payload=json.dumps(raw), severity="info", surfaced_at=PUBLISHED,
                expires_at=PUBLISHED + timedelta(days=7), kind="allocate", status="open"))
            db.commit()
        receipts = [json.loads(row.payload_json) for row in db.scalars(select(ShadowEvent)
            .where(ShadowEvent.user_id == "b", ShadowEvent.kind == "sheet"))]
        assert {row["instruments"]["AAA"] for row in receipts} == {"AAA.L", "AAA.SW"}


def test_failed_attempts_are_durable_and_do_not_starve_later_users(store, monkeypatch):
    import argosy.services.shadow_daily as daily
    with store() as db:
        for index in range(11):
            user = f"c{index:02}"
            db.add(User(id=user))
            db.add(PortfolioSnapshotRow(user_id=user, imported_at=ACTIVATED, positions_json='[]'))
        db.commit()
    called = set()
    def fail(factory, user, **kwargs):
        called.add(user)
        raise ValueError("Deliberate provider outage")
    monkeypatch.setattr(daily, "run_user", fail)
    daily.run_batch(store, now=PUBLISHED)
    daily.run_batch(store, now=PUBLISHED + timedelta(seconds=1))
    assert len(called) == 12
    with store() as db:
        assert set(db.scalars(select(ShadowEvent.user_id).where(ShadowEvent.kind == "attempt"))) == called


def test_future_session_cannot_enter_market_ledger(store):
    with pytest.raises(ValueError, match="future/incomplete"):
        run_user(store, "a", now=datetime(2026, 1, 5, 12, tzinfo=UTC), collector=collected)


def test_activation_records_preexisting_publications_without_backdating_entries(store):
    with store() as db:
        raw = authored()
        raw["order_sheet"]["user_id"] = "b"
        sheet = OrderSheet.model_validate(raw["order_sheet"])
        raw["order_sheet_fingerprint"] = order_sheet_fingerprint(sheet)
        row = ActionProposal(user_id="b", summary="Old advice", rationale_md="Old thesis",
            suggested_payload=json.dumps(raw), severity="info", surfaced_at=ACTIVATED - timedelta(days=1),
            expires_at=PUBLISHED + timedelta(days=7), kind="allocate", status="open")
        db.add(row)
        db.add(PortfolioSnapshotRow(user_id="b", imported_at=ACTIVATED, positions_json='[]'))
        db.flush()
        ledger.activate(db, user_id="b", now=ACTIVATED)
        db.commit()
        event = db.scalar(select(ShadowEvent).where(ShadowEvent.user_id == "b", ShadowEvent.kind == "sheet"))
        receipt = json.loads(event.payload_json)
        assert receipt["publication_time_basis"] == "known_preexisting_at_activation"
        assert datetime.fromisoformat(receipt["published_at"]) < ACTIVATED
        row.suggested_payload = json.dumps({**raw, "paper_economics": {"AAA": {"fee_usd": 7}}})
        db.commit()
        events = list(db.scalars(select(ShadowEvent).where(ShadowEvent.user_id == "b",
            ShadowEvent.kind == "sheet").order_by(ShadowEvent.id)))
        assert len(events) == 2
        revision = json.loads(events[-1].payload_json)
        assert datetime.fromisoformat(revision["published_at"]) == PUBLISHED
        assert revision["source_surfaced_at"] == receipt["source_surfaced_at"]


def test_migration_upgrade_downgrade_upgrade_on_empty_ledger(alembic_engine_at_head, monkeypatch):
    from alembic.config import Config

    from alembic import command
    engine = alembic_engine_at_head
    monkeypatch.setenv("ARGOSY_ALEMBIC_URL", str(engine.url).replace("sqlite:", "sqlite+aiosqlite:"))
    config = Config("alembic.ini")
    command.downgrade(config, "0122_fill_book_application")
    command.upgrade(config, "head")
    with engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM shadow_events")).scalar_one() == 0
