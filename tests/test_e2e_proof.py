from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from argosy.api.routes.action_proposals import AcceptRequest, accept_action
from argosy.services.e2e_proof import build_e2e_proof
from argosy.services.order_sheet import ReviewResolution
from argosy.services.order_sheet_materializer import order_sheet_fingerprint
from argosy.state.models import (
    ActionProposal,
    AgentReport,
    Base,
    Lot,
    Proposal,
    ProposalHistory,
    User,
)
from tests.test_order_sheet_materializer import NOW, _sell_funded_sheet


def _directive(session: Session) -> ActionProposal:
    sheet = _sell_funded_sheet().model_copy(update={
        "freshness_days": 7,
        "review_resolution": ReviewResolution(
            rounds=1,
            reviewers_ran=5,
            reviewers_expected=5,
            one_voice=True,
            summary="All five independent reviewers cleared the final order list.",
        )
    })
    reports = []
    for role in ("deployment_author", "deployment_reviewer"):
        report = AgentReport(
            user_id="ariel",
            agent_role=role,
            decision_id="deploy-test",
            model="test-model",
            response_text="{}",
            cost_usd=0.1,
            created_at=NOW,
        )
        session.add(report)
        reports.append(report)
    session.flush()
    now = datetime.now(UTC)
    row = ActionProposal(
        user_id="ariel",
        summary="One validated switch",
        rationale_md="The complete list.",
        suggested_payload=json.dumps(
            {
                "artifact_type": "validated_order_sheet",
                "validation_status": "validated",
                "order_sheet_fingerprint": order_sheet_fingerprint(sheet),
                "order_sheet": sheet.model_dump(mode="json"),
                "team_agent_report_ids": [report.id for report in reports],
                "team_decision_ids": ["deploy-test"],
                "team_cost_usd": 0.2,
            }
        ),
        severity="info",
        surfaced_at=now,
        expires_at=now + timedelta(days=3),
        status="open",
        kind="allocate",
        dedup_key="period_directive:ariel",
        execution_state="proposed",
    )
    session.add(row)
    session.commit()
    return row


def test_e2e_projection_proves_exact_materialization_and_calibration() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel"))
        session.add(
            Lot(
                user_id="ariel",
                account_id="schwab_rsu",
                ticker="NVDA",
                quantity=500,
                cost_basis_usd=20_000,
            )
        )
        parent = _directive(session)
        legacy = Proposal(
            user_id="ariel",
            ticker="SOFI",
            action="buy",
            size_shares_or_currency=100,
            size_units="shares",
            instrument="stock",
            order_type="market",
            tier="T2",
            account_class="main",
            status="approved",
            source="manual",
            rationale_summary="Older approval.",
        )
        session.add(legacy)
        session.commit()

        # Production starts clocks on the ActionProposal before trade Proposal
        # ids exist. Acceptance must reuse those immutable observations.
        from argosy.services.jobs.period_directive_daily import _record_surfaced_recommendations
        from argosy.services.order_sheet import OrderSheet
        payload = json.loads(parent.suggested_payload)
        _record_surfaced_recommendations(
            session, proposal_id=parent.id,
            sheet=OrderSheet.model_validate(payload["order_sheet"]),
            fingerprint=payload["order_sheet_fingerprint"],
        )

        authored = build_e2e_proof(session, "ariel")
        assert authored["stage"] == "ready_to_accept"
        assert authored["artifact"]["validation"]["valid"] is True
        assert len(authored["lines"]) == 2
        assert all(line["proposal"] is None for line in authored["lines"])
        assert next(check for check in authored["checks"] if check["key"] == "telemetry")["passed"]
        assert not next(check for check in authored["checks"] if check["key"] == "materialized")["passed"]
        assert "schwab_rsu" in authored["known_accounts"]

        response = accept_action(parent.id, AcceptRequest(user_id="ariel"), db=session)
        assert response.materialization_status == "approved_for_execution"

        staged = build_e2e_proof(session, "ariel")
        assert staged["stage"] == "execution"
        checks = {row["key"]: row["passed"] for row in staged["checks"]}
        assert checks["validated"] is True
        assert checks["fingerprint"] is True
        assert checks["materialized"] is True
        assert checks["telemetry"] is True
        assert checks["fills"] is False
        assert staged["self_audit"] == []
        assert all(line["proposal"] is not None for line in staged["lines"])
        assert all(line["calibration"]["status"] == "scheduled" for line in staged["lines"])
        assert all(
            line["calibration"]["due_at"][:10] == line["authored"]["expectation"]["due_date"]
            for line in staged["lines"]
        )
        session.refresh(legacy)
        assert legacy.status == "cancelled"
        history = session.query(ProposalHistory).filter_by(proposal_id=legacy.id).all()
        assert any("Superseded by validated order sheet" in row.note for row in history)
        from argosy.state.models import Prediction
        forecasts = session.query(Prediction).filter_by(source="signal_stream:order_sheet").all()
        assert len(forecasts) == 6  # authored, half-year and annual for each line
        assert all(json.loads(row.source_ref)["proposal_id"] == parent.id for row in forecasts)
        authored_clock = next(row for row in forecasts if row.evaluation_method == "order_sheet_due_date_v1")
        session.delete(authored_clock)
        session.commit()
        missing = build_e2e_proof(session, "ariel")
        assert not next(row["passed"] for row in missing["checks"] if row["key"] == "telemetry")
    engine.dispose()


@pytest.mark.parametrize("research", [False, True])
@pytest.mark.parametrize("expired", [False, True])
def test_no_action_is_not_approval_or_completed_execution(research, expired):
    from argosy.services.order_sheet import FundingSummary, OrderSheet, PendingResearch

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel"))
        parent = _directive(session)
        payload = json.loads(parent.suggested_payload)
        original = OrderSheet.model_validate(payload["order_sheet"])
        sheet = original.model_copy(update={
            "lines": [], "portfolio_symbols": [],
            "review_resolution": original.review_resolution.model_copy(update={"separation_reviewed": True}),
            "funding": FundingSummary(new_cash_usd=0, gross_sell_proceeds_usd=0,
                                      sell_tax_usd=0, sell_costs_usd=0, reserve_usd=0,
                                      available_to_buy_usd=0),
            "pending_research": [PendingResearch(
                tickers=["EXUS"], disagreement="Evidence incomplete",
                missing_evidence="Verified costs", research_question="Verify fund costs",
                next_review_date=(datetime.now(UTC) + timedelta(days=2)).date(),
                reserved_usd=0, independence_reason="No executable actions depend on this",
            )] if research else [],
        })
        payload["order_sheet"] = sheet.model_dump(mode="json")
        payload["order_sheet_fingerprint"] = order_sheet_fingerprint(sheet)
        parent.suggested_payload = json.dumps(payload)
        if expired:
            parent.expires_at = datetime.now(UTC) - timedelta(days=1)
        session.commit()

        proof = build_e2e_proof(session, "ariel")
        assert proof["stage"] == ("blocked" if expired else "no_action")
        if not expired:
            assert "research remains open" in proof["headline"] if research else "in this review" in proof["headline"]
        assert len(proof["pending_research"]) == int(research)
        assert proof["lines"] == []
        execution_checks = [c for c in proof["checks"] if c["key"] in {"materialized", "telemetry", "fills"}]
        assert all(c["applicable"] is False and c["passed"] is False for c in execution_checks)
        assert session.query(Proposal).count() == 0
        assert proof["self_audit"] == []
    engine.dispose()


def test_unparseable_outcome_is_not_a_score_and_missing_clocks_are_audited():
    from argosy.services.jobs.period_directive_daily import _record_surfaced_recommendations
    from argosy.services.order_sheet import OrderSheet
    from argosy.state.models import EvaluationMethod, Prediction, PredictionOutcome

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel"))
        parent = _directive(session)
        missing = build_e2e_proof(session, "ariel")
        assert any("Surfaced recommendations" in finding for finding in missing["self_audit"])
        payload = json.loads(parent.suggested_payload)
        _record_surfaced_recommendations(
            session, proposal_id=parent.id, sheet=OrderSheet.model_validate(payload["order_sheet"]),
            fingerprint=payload["order_sheet_fingerprint"],
        )
        clock = session.query(Prediction).filter_by(evaluation_method="order_sheet_due_date_v1").first()
        session.add(EvaluationMethod(method_name=clock.evaluation_method,
                                     family="fixed_lookahead", method_version=1))
        session.add(PredictionOutcome(prediction_id=clock.id, evaluation_method=clock.evaluation_method,
                                      outcome_kind="unparseable", evaluated_at=NOW))
        session.commit()
        proof = build_e2e_proof(session, "ariel")
        calibration = next(line["calibration"] for line in proof["lines"]
                           if line["calibration"]["prediction_id"] == clock.id)
        assert calibration["status"] == "unscorable"
        assert calibration["outcomes"][0]["kind"] == "unparseable"
        assert proof["self_audit"] == []
    engine.dispose()


def test_e2e_projection_does_not_mislabel_legacy_directive_as_proof() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel"))
        session.add(
            ActionProposal(
                user_id="ariel",
                summary="Legacy prose allocation",
                rationale_md="No schema.",
                suggested_payload=json.dumps({"cash_usd": 10_000}),
                severity="info",
                surfaced_at=NOW,
                expires_at=NOW + timedelta(days=3),
                status="open",
                kind="allocate",
                dedup_key="period_directive:ariel",
                execution_state="proposed",
            )
        )
        session.commit()

        proof = build_e2e_proof(session, "ariel")
        assert proof["stage"] == "not_run"
        assert proof["artifact"] is None
        assert "legacy" in proof["headline"].lower()
    engine.dispose()


@pytest.mark.parametrize("quantity", [201, 100000000000000])
def test_same_count_quantity_drift_is_broken_not_proof_of_exact_orders(quantity):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="ariel"))
        session.add(Lot(user_id="ariel", account_id="schwab_rsu", ticker="NVDA",
                        quantity=500, cost_basis_usd=20000))
        parent = _directive(session)
        accept_action(parent.id, AcceptRequest(user_id="ariel"), db=session)
        proposal = session.query(Proposal).filter_by(source="order_sheet").first()
        proposal.size_shares_or_currency = quantity
        session.commit()
        proof = build_e2e_proof(session, "ariel")
        assert proof["stage"] == "broken"
        assert not next(c["passed"] for c in proof["checks"] if c["key"] == "materialized")
        assert any("size_shares_or_currency" in finding for finding in proof["self_audit"])
        if quantity == 100000000000000:
            assert any("exceeds positive ledger" in finding for finding in proof["self_audit"])
    engine.dispose()


@pytest.mark.parametrize("fault", [None, "paper", "ticker", "account_id", "action", "broker",
                                   "user_id", "broker_order_id", "external_fill_id", "overfill",
                                   "shortfill", "missing_clock"])
def test_completion_requires_matching_live_receipts_and_telemetry(fault):
    from argosy.services.order_sheet_audit import audit_order_sheet
    from argosy.state.models import Fill, Prediction

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([User(id="ariel"), User(id="other")])
        session.add(Lot(user_id="ariel", account_id="schwab_rsu", ticker="NVDA",
                        quantity=500, cost_basis_usd=20000))
        parent = _directive(session)
        accept_action(parent.id, AcceptRequest(user_id="ariel"), db=session)
        for proposal in session.query(Proposal).filter_by(source="order_sheet").all():
            row = Fill(user_id="ariel", proposal_id=proposal.id, broker="schwab_csv",
                       broker_order_id=f"order-{proposal.id}", external_fill_id=f"exec-{proposal.id}",
                       account_id=proposal.account_id, ticker=proposal.ticker, action=proposal.action,
                       quantity=proposal.size_shares_or_currency, price=proposal.limit_price,
                       commission=1, paper=False)
            if fault == "paper":
                row.paper = True
            elif fault in {"ticker", "account_id", "action", "broker", "user_id"}:
                setattr(row, fault, "other")
            elif fault in {"broker_order_id", "external_fill_id"}:
                setattr(row, fault, "")
            elif fault == "overfill":
                row.quantity += 1
            elif fault == "shortfill":
                row.quantity -= Decimal("0.0001")
            session.add(row)
        if fault == "missing_clock":
            clock = session.query(Prediction).filter_by(evaluation_method="order_sheet_due_date_v1").first()
            session.delete(clock)
        session.commit()
        proof = build_e2e_proof(session, "ariel")
        audit = audit_order_sheet(session, user_id="ariel",
                                 fingerprint=json.loads(parent.suggested_payload)["order_sheet_fingerprint"])
        assert (proof["stage"] == "filled") is (fault is None)
        assert proof["stage"] != "complete"  # Receipt totals do not prove the portfolio was updated.
        assert next(c["passed"] for c in proof["checks"] if c["key"] == "fills") is (fault in {None, "missing_clock"})
        if fault == "paper":
            assert all(line["execution"]["paper_fill_count"] == 1 for line in proof["lines"])
            assert all(line.filled_shares == 0 and line.paper_fill_count == 1 for line in audit.lines)
        if fault not in {None, "paper", "shortfill", "missing_clock"}:
            assert proof["stage"] == "broken" and proof["self_audit"]
            assert all(line.receipt_errors for line in audit.lines)
    engine.dispose()
