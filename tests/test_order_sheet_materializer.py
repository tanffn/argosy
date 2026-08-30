import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

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
from argosy.services.action_order_sheet_acceptance import materialize_action_order_sheet
from argosy.services.order_sheet_materializer import (
    materialize_order_sheet,
    order_sheet_fingerprint,
)
from argosy.state.models import (
    ActionProposal,
    Base,
    DecisionRun,
    Fill,
    Lot,
    PendingOrder,
    Prediction,
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
        assert len(predictions) == 2
        authored, thesis = predictions
        assert authored.source == "signal_stream:order_sheet"
        assert authored.evaluation_method == "order_sheet_due_date_v1"
        assert authored.evaluation_due_at.date() == date(2026, 12, 31)
        assert thesis.evaluation_method == "fixed_lookahead_180d"
        assert thesis.timeframe_days == 180
        session.add(
            Fill(
                user_id="ariel",
                proposal_id=row.id,
                broker="leumi_tsv",
                broker_order_id="manual-1",
                ticker="EXUS",
                action="buy",
                quantity=200,
                price=50.25,
                commission=25,
                paper=False,
            )
        )
        session.flush()
        audit = audit_order_sheet(
            session,
            user_id="ariel",
            fingerprint=order_sheet_fingerprint(_sheet()),
        )
        assert audit.proposals == 1 and audit.proposals_filled == 1
        assert audit.lines[0].average_fill_price_usd == 50.25
        assert audit.lines[0].adverse_slippage_bps == 50
        assert audit.lines[0].commission_usd == 25


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
