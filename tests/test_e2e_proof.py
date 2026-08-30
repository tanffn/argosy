from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

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

        authored = build_e2e_proof(session, "ariel")
        assert authored["stage"] == "ready_to_accept"
        assert authored["artifact"]["validation"]["valid"] is True
        assert len(authored["lines"]) == 2
        assert all(line["proposal"] is None for line in authored["lines"])
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
        session.refresh(legacy)
        assert legacy.status == "cancelled"
        history = session.query(ProposalHistory).filter_by(proposal_id=legacy.id).all()
        assert any("Superseded by validated order sheet" in row.note for row in history)
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
