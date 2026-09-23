import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from argosy.services.action_order_sheet_acceptance import materialize_action_order_sheet
from argosy.services.order_sheet import (
    DatedCatalyst,
    FundingSummary,
    MarketEvidence,
    NoActionLine,
    OrderLine,
    OrderSheet,
    OutcomeExpectation,
    TaxImpact,
    VoiceVerdict,
)
from argosy.services.order_sheet_audit import audit_order_sheet
from argosy.services.order_sheet_materializer import (
    materialize_order_sheet,
    order_sheet_fingerprint,
)
from argosy.state.models import (
    ActionProposal,
    Base,
    DecisionRun,
    EvaluationMethod,
    Fill,
    Lot,
    PendingOrder,
    Prediction,
    PredictionOutcome,
    Proposal,
    ProposalHistory,
    User,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _sheet() -> OrderSheet:
    voice = VoiceVerdict(
        source="deployment_author",
        verdict="BUY",
        as_of=NOW,
        rationale="authored allocation",
    )
    return OrderSheet(
        user_id="ariel",
        generated_at=NOW,
        horizon_years_min=1,
        horizon_years_max=5,
        portfolio_symbols=[],
        funding=FundingSummary(
            new_cash_usd=10_000,
            gross_sell_proceeds_usd=0,
            sell_tax_usd=0,
            sell_costs_usd=0,
            reserve_usd=0,
            available_to_buy_usd=10_000,
        ),
        lines=[
            OrderLine(
                symbol="EXUS",
                action="BUY",
                shares=200,
                notional_usd=10_000,
                venue="LSE",
                instrument_type="etf",
                thesis="Diversify outside the US.",
                thesis_type="diversifier",
                falsifier="The fund becomes US-heavy.",
                catalyst=DatedCatalyst(
                    description="quarterly rebalance", due_date=date(2026, 11, 30)
                ),
                expectation=OutcomeExpectation(
                    statement="The ex-US gap closes.",
                    due_date=date(2026, 12, 31),
                    success_measure="allocation moves toward target",
                ),
                evidence=MarketEvidence(
                    price_usd=50,
                    price_as_of=NOW,
                    price_source="live",
                ),
                stance_source="rebalance",
                voices=[voice],
                post_trade_weight_pct=2,
                expected_upside_multiple=2,
                estate_situs="non_US",
            )
        ],
        no_action=[
            NoActionLine(
                symbol="CASH",
                reason="The stated cash is fully allocated.",
                voices=[
                    VoiceVerdict(
                        source="cash_review",
                        verdict="HOLD",
                        as_of=NOW,
                        rationale="no residual cash",
                    )
                ],
            )
        ],
        rationale="One validated list.",
    )


def _sell_funded_sheet() -> OrderSheet:
    base = _sheet()
    sell = OrderLine(
        symbol="NVDA",
        action="TRIM",
        shares=100,
        notional_usd=12_000,
        venue="NASDAQ",
        thesis="Reduce concentration to fund the preferred diversifier.",
        thesis_type="diversifier",
        falsifier="Concentration is already within the accepted plan cap.",
        catalyst=DatedCatalyst(
            description="quarterly rebalance", due_date=date(2026, 11, 30)
        ),
        expectation=OutcomeExpectation(
            statement="The funded switch reduces concentration.",
            due_date=date(2026, 12, 31),
            success_measure="NVDA weight declines while the diversifier is funded",
        ),
        evidence=MarketEvidence(price_usd=120, price_as_of=NOW, price_source="live"),
        stance_source="funding_switch",
        voices=[VoiceVerdict(
            source="deployment_author",
            verdict="TRIM",
            as_of=NOW,
            rationale="Unified funding judgment.",
        )],
        post_trade_weight_pct=10,
        expected_upside_multiple=2,
        estate_situs="US",
        tax=TaxImpact(
            gross_proceeds_usd=12_000,
            cost_basis_usd=4_000,
            taxable_gain_usd=8_000,
            effective_tax_rate=0.25,
            estimated_tax_usd=2_000,
            net_proceeds_usd=10_000,
            method="Israeli CGT from authoritative lot basis",
            authoritative=True,
            as_of=NOW,
        ),
    )
    return base.model_copy(update={
        "portfolio_symbols": ["NVDA"],
        "funding": FundingSummary(
            new_cash_usd=0,
            gross_sell_proceeds_usd=12_000,
            sell_tax_usd=2_000,
            sell_costs_usd=0,
            reserve_usd=0,
            available_to_buy_usd=10_000,
        ),
        "lines": [*base.lines, sell],
    })


@pytest.mark.real_seam
def test_validated_sheet_materializes_once_into_execution_spine() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel"))
        session.flush()
        first = materialize_order_sheet(
            session, _sheet(), funding_account_id="leumi_main"
        )
        second = materialize_order_sheet(
            session, _sheet(), funding_account_id="leumi_main"
        )
        assert [row.id for row in first] == [row.id for row in second]
        row = first[0]
        assert row.status == "awaiting_human"
        assert row.account_id == "leumi_main"
        assert row.size_units == "shares" and float(row.size_shares_or_currency) == 200
        assert row.instrument == "etf"
        assert row.source == "order_sheet"
        assert session.scalar(select(DecisionRun).where(DecisionRun.id == row.decision_run_id))
        assert session.scalar(
            select(ProposalHistory).where(ProposalHistory.proposal_id == row.id)
        )
        assert len(session.scalars(select(Proposal)).all()) == 1
        predictions = session.scalars(select(Prediction).order_by(Prediction.id)).all()
        assert len(predictions) == 3
        authored, thesis, annual = predictions
        assert authored.source == "signal_stream:order_sheet"
        assert authored.evaluation_method == "order_sheet_due_date_v1"
        assert authored.evaluation_due_at.date() == date(2026, 12, 31)
        assert thesis.evaluation_method == "fixed_lookahead_180d"
        assert thesis.timeframe_days == 180
        assert annual.evaluation_method == "fixed_lookahead_365d"
        assert annual.timeframe_days == 365
        session.add(
            Fill(
                user_id="ariel",
                proposal_id=row.id,
                broker="leumi_tsv",
                broker_order_id="manual-1",
                external_fill_id="manual-execution-1",
                account_id="leumi_main",
                ticker="EXUS",
                action="buy",
                quantity=200,
                price=50.25,
                commission=25,
                paper=False,
            )
        )
        session.flush()
        session.add(EvaluationMethod(method_name="order_sheet_due_date_v1",
                                     family="fixed_lookahead", method_version=1))
        session.add(PredictionOutcome(
            prediction_id=authored.id, evaluation_method="order_sheet_due_date_v1",
            outcome_kind="expired_positive", pnl_pct=0.15, evaluated_at=NOW,
        ))
        session.flush()
        audit = audit_order_sheet(
            session,
            user_id="ariel",
            fingerprint=order_sheet_fingerprint(_sheet()),
        )
        assert audit.proposals == 1 and audit.proposals_filled == 1
        assert audit.predictions_due == 1 and audit.predictions_scored == 1
        assert audit.lines[0].prediction_id == authored.id
        assert audit.lines[0].average_fill_price_usd == 50.25
        assert audit.lines[0].adverse_slippage_bps == 50
        assert audit.lines[0].commission_usd == 25


@pytest.mark.parametrize("field,value", [
    ("ticker", "OTHER"), ("action", "sell"), ("size_shares_or_currency", 201),
    ("size_units", "currency"), ("limit_price", 50.0001), ("account_id", "schwab_main"),
    ("instrument", "stock"), ("order_type", "market"), ("time_in_force", "GTC"),
    ("shadow", 1), ("account_class", "limited"), ("source", "manual"),
    ("expires_at", NOW + timedelta(days=30)), ("expected_impact_json", "{}"),
])
def test_same_row_count_is_not_exact_materialization(field, value):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel"))
        row = materialize_order_sheet(session, _sheet(), funding_account_id="leumi_main")[0]
        session.commit()
        setattr(row, field, value)
        session.flush()
        with pytest.raises(ValueError, match="materialization mismatch"):
            materialize_order_sheet(session, _sheet(), funding_account_id="leumi_main")
        assert session.query(Proposal).count() == 1
    engine.dispose()


def test_retry_cannot_change_account_but_unified_approval_promotes_exact_staged_rows():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel"))
        row = materialize_order_sheet(session, _sheet(), funding_account_id="leumi_main")[0]
        with pytest.raises(ValueError, match="different custody"):
            materialize_order_sheet(session, _sheet(), funding_account_id="schwab_main")
        assert row.status == "awaiting_human"
        again = materialize_order_sheet(session, _sheet(), funding_account_id="leumi_main",
                                       approved_by_unified_acceptance=True)
        assert again[0].id == row.id and row.status == "approved"
        row.status = "cancelled"
        with pytest.raises(ValueError, match="Cannot re-approve"):
            materialize_order_sheet(session, _sheet(), funding_account_id="leumi_main",
                                    approved_by_unified_acceptance=True)
        assert row.status == "cancelled"
    engine.dispose()


def test_changed_owner_stays_linked_to_original_run_and_cannot_duplicate():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([User(id="ariel"), User(id="other")])
        row = materialize_order_sheet(session, _sheet(), funding_account_id="leumi_main")[0]
        session.commit()
        row.user_id = "other"
        session.commit()
        with pytest.raises(ValueError, match="user_id differs"):
            materialize_order_sheet(session, _sheet(), funding_account_id="leumi_main")
        assert session.query(Proposal).count() == 1
        assert session.query(DecisionRun).count() == 1
    engine.dispose()


@pytest.mark.parametrize("via_acceptance", [False, True])
def test_missing_all_orders_cannot_recreate_a_materialized_sheet(via_acceptance):
    from sqlalchemy import delete

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel"))
        materialize_order_sheet(session, _sheet(), funding_account_id="leumi_main")
        session.commit()
        # Simulate corruption out of band: the durable run must prevent replay.
        session.execute(delete(Proposal))
        session.commit()
        with pytest.raises(ValueError, match="order rows are missing"):
            if via_acceptance:
                parent = ActionProposal(kind="allocate", suggested_payload=json.dumps({
                    "artifact_type": "validated_order_sheet", "validation_status": "validated",
                    "order_sheet": _sheet().model_dump(mode="json"),
                    "order_sheet_fingerprint": order_sheet_fingerprint(_sheet()),
                }))
                materialize_action_order_sheet(session, parent, funding_account_id="leumi_main")
            else:
                materialize_order_sheet(session, _sheet(), funding_account_id="leumi_main")
        assert session.query(Proposal).count() == 0
        assert session.query(DecisionRun).count() == 1
    engine.dispose()


def test_shared_validator_checks_matching_but_out_of_range_amounts():
    from argosy.services.order_sheet_materializer import materialized_order_sheet_errors

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel"))
        sheet = _sheet()
        row = materialize_order_sheet(session, sheet, funding_account_id="leumi_main")[0]
        row.size_shares_or_currency = 100000000000000
        session.commit()
        # Matching malformed authored+stored amounts must not be mistaken for
        # arithmetic integrity by consumers that do not call materialize().
        sheet = sheet.model_copy(update={"lines": [sheet.lines[0].model_copy(
            update={"shares": 100000000000000})]})
        errors = materialized_order_sheet_errors(session, sheet, [row])
        assert any("size_shares_or_currency exceeds positive ledger" in error for error in errors)
        assert not any("size_shares_or_currency differs" in error for error in errors)
    engine.dispose()


@pytest.mark.parametrize("inventory_change", ["exhaust", "delete", "reassign"])
@pytest.mark.parametrize("via_acceptance", [False, True])
def test_replay_preserves_original_custody_after_inventory_changes(inventory_change, via_acceptance):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sheet = _sell_funded_sheet()
    with Session(engine) as session:
        session.add(User(id="ariel"))
        lot = Lot(user_id="ariel", account_id="schwab_rsu", ticker="NVDA",
                  quantity=500, cost_basis_usd=20_000)
        session.add(lot)
        rows = materialize_order_sheet(session, sheet)
        original_ids = [row.id for row in rows]
        session.commit()
        if inventory_change == "delete":
            session.delete(lot)
        elif inventory_change == "exhaust":
            lot.quantity = 0
        else:
            lot.account_id = "leumi_main"
        session.commit()
        if via_acceptance:
            parent = ActionProposal(kind="allocate", suggested_payload=json.dumps({
                "artifact_type": "validated_order_sheet", "validation_status": "validated",
                "order_sheet": sheet.model_dump(mode="json"),
                "order_sheet_fingerprint": order_sheet_fingerprint(sheet),
            }))
            repeated, funding = materialize_action_order_sheet(session, parent)
            assert funding == "schwab_rsu"
            assert {row.status for row in repeated} == {"approved"}
        else:
            repeated = materialize_order_sheet(session, sheet)
        assert [row.id for row in repeated] == original_ids
        assert {row.account_id for row in repeated} == {"schwab_rsu"}
        assert session.query(Proposal).count() == 2
        assert session.query(DecisionRun).count() == 1
        with pytest.raises(ValueError, match="different custody"):
            materialize_order_sheet(session, sheet, sell_accounts_by_symbol={"NVDA": "leumi_main"})
    engine.dispose()


@pytest.mark.parametrize("value", [
    "100000000000000", "99999999999999.99999", "0.000000001",
    "0.00005", "0", "-1", "NaN", "Infinity", "50.000001",
])
def test_ledger_numeric_rejects_overflow_zero_and_rounding_loss(value):
    from argosy.services.order_sheet_materializer import _ledger_number

    with pytest.raises(ValueError, match="ledger precision or range"):
        _ledger_number(Decimal(value))


@pytest.mark.parametrize("value,expected", [
    ("99999999999999.9999", "99999999999999.9999"),
    ("0.0001", "0.0001"), ("50.00000000000001", "50.0000"),
])
def test_ledger_numeric_accepts_boundaries_and_binary_noise(value, expected):
    from argosy.services.order_sheet_materializer import _ledger_number

    assert _ledger_number(Decimal(value)) == Decimal(expected)


def test_incidental_fingerprint_text_does_not_join_an_unrelated_proposal():
    from argosy.services.order_sheet_materializer import linked_order_sheet_proposals

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel"))
        unrelated = Proposal(user_id="ariel", ticker="OTHER", action="buy", source="order_sheet", tier="T2",
                             expected_impact_json=json.dumps({"comment": order_sheet_fingerprint(_sheet())}))
        session.add(unrelated)
        session.flush()
        rows = materialize_order_sheet(session, _sheet(), funding_account_id="leumi_main")
        assert linked_order_sheet_proposals(session, _sheet()) == rows
        assert unrelated.id != rows[0].id and session.query(Proposal).count() == 2
    engine.dispose()


def test_acknowledging_no_action_does_not_create_run_or_cancel_other_approvals():
    sheet = _sheet().model_copy(update={"lines": [], "funding": FundingSummary(
        new_cash_usd=0, gross_sell_proceeds_usd=0, sell_tax_usd=0,
        sell_costs_usd=0, reserve_usd=0, available_to_buy_usd=0,
    )})
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel"))
        legacy = Proposal(user_id="ariel", ticker="OTHER", action="buy", tier="T2", status="approved")
        session.add(legacy)
        session.flush()
        parent = ActionProposal(kind="allocate", suggested_payload=json.dumps({
            "artifact_type": "validated_order_sheet", "validation_status": "validated",
            "order_sheet": sheet.model_dump(mode="json"),
            "order_sheet_fingerprint": order_sheet_fingerprint(sheet),
        }))
        assert materialize_action_order_sheet(session, parent) == ([], None)
        assert materialize_order_sheet(session, sheet) == []
        assert legacy.status == "approved"
        assert session.query(DecisionRun).count() == 0
        assert session.query(Proposal).count() == 1
    engine.dispose()


def test_precision_loss_is_rejected_before_any_order_is_created():
    sheet = _sheet()
    # Funding stays exact while the ledger's price precision cannot represent it.
    line = sheet.lines[0].model_copy(update={
        "shares": 200, "notional_usd": 10000.0002,
        "evidence": sheet.lines[0].evidence.model_copy(update={"price_usd": 50.000001}),
    })
    sheet = sheet.model_copy(update={"lines": [line], "funding": sheet.funding.model_copy(
        update={"new_cash_usd": 10000.0002, "available_to_buy_usd": 10000.0002})})
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel"))
        with pytest.raises(ValueError, match="ledger precision"):
            materialize_order_sheet(session, sheet, funding_account_id="leumi_main")
        assert session.query(Proposal).count() == session.query(DecisionRun).count() == 0
    engine.dispose()


@pytest.mark.real_seam
def test_zero_cash_switch_derives_buy_account_from_exact_sell_custody() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel"))
        session.add(Lot(
            user_id="ariel",
            account_id="schwab_rsu",
            ticker="NVDA",
            quantity=500,
            cost_basis_usd=20_000,
        ))
        session.flush()
        rows = materialize_order_sheet(session, _sell_funded_sheet())

    assert {(row.ticker, row.account_id) for row in rows} == {
        ("EXUS", "schwab_rsu"),
        ("NVDA", "schwab_rsu"),
    }
    engine.dispose()


def test_external_cash_still_requires_explicit_funding_account() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session, pytest.raises(
        ValueError, match="exact buy funding account is required"
    ):
        session.add(User(id="ariel"))
        session.flush()
        materialize_order_sheet(session, _sheet())
    engine.dispose()


@pytest.mark.real_seam
def test_accepting_unified_action_atomically_materializes_trade_proposals() -> None:
    from argosy.api.routes.action_proposals import AcceptRequest, accept_action

    assert callable(materialize_action_order_sheet)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sheet = _sell_funded_sheet()
    fingerprint = order_sheet_fingerprint(sheet)
    payload = {
        "artifact_type": "validated_order_sheet",
        "validation_status": "validated",
        "order_sheet_fingerprint": fingerprint,
        "order_sheet": sheet.model_dump(mode="json"),
    }
    with Session(engine) as session:
        session.add(User(id="ariel"))
        session.add(Lot(
            user_id="ariel",
            account_id="schwab_rsu",
            ticker="NVDA",
            quantity=500,
            cost_basis_usd=20_000,
        ))
        parent = ActionProposal(
            user_id="ariel",
            summary="One unified switch",
            rationale_md="Approved as one list.",
            suggested_payload=json.dumps(payload),
            severity="info",
            surfaced_at=NOW,
            expires_at=NOW + timedelta(days=3),
            status="open",
            kind="allocate",
            dedup_key="period_directive:ariel",
            execution_state="proposed",
        )
        session.add(parent)
        session.commit()

        response = accept_action(parent.id, AcceptRequest(user_id="ariel"), db=session)
        executable = session.scalars(
            select(Proposal).where(Proposal.source == "order_sheet")
        ).all()

        assert response.materialization_status == "approved_for_execution"
        assert response.funding_account_id == "schwab_rsu"
        assert response.materialized_proposal_ids == [row.id for row in executable]
        assert session.get(ActionProposal, parent.id).status == "accepted"
        assert {(row.ticker, row.account_id, row.status) for row in executable} == {
            ("EXUS", "schwab_rsu", "approved"),
            ("NVDA", "schwab_rsu", "approved"),
        }
        assert session.scalar(select(PendingOrder).limit(1)) is None
    engine.dispose()


def test_accept_without_resolvable_funding_leaves_parent_open() -> None:
    from fastapi import HTTPException

    from argosy.api.routes.action_proposals import AcceptRequest, accept_action

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sheet = _sheet()
    payload = {
        "artifact_type": "validated_order_sheet",
        "validation_status": "validated",
        "order_sheet_fingerprint": order_sheet_fingerprint(sheet),
        "order_sheet": sheet.model_dump(mode="json"),
    }
    with Session(engine) as session:
        session.add(User(id="ariel"))
        parent = ActionProposal(
            user_id="ariel",
            summary="Allocate external cash",
            rationale_md="Requires exact custody.",
            suggested_payload=json.dumps(payload),
            severity="info",
            surfaced_at=NOW,
            expires_at=NOW + timedelta(days=3),
            status="open",
            kind="allocate",
            dedup_key="period_directive:ariel",
            execution_state="proposed",
        )
        session.add(parent)
        session.commit()

        with pytest.raises(HTTPException, match="exact buy funding account"):
            accept_action(parent.id, AcceptRequest(user_id="ariel"), db=session)
        session.expire_all()
        assert session.get(ActionProposal, parent.id).status == "open"
        assert session.scalar(select(Proposal).limit(1)) is None
    engine.dispose()
