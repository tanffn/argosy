"""/deploy-cash wiring for the fleet-authors pivot: behind deployment_author_enabled
the route attaches `authored` (accepted → primary; unavailable/rejected → degraded,
tiers are the labelled fallback). The author itself is stubbed — no LLM here."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from argosy.api.main import create_app
from argosy.services.allocation_author.flow import AuthorOutcome
from argosy.services.allocation_author.proposal import (
    AllocationProposal,
    AuthoredOrderIntent,
    Buy,
    Sell,
)
from argosy.services.allocation_author.verifier import GateReport, GateStatus


def _doc():
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc,
        AllocationInstrument,
        TargetAllocationDoc,
    )

    return TargetAllocationDoc(
        anchor_sigma=0.18,
        blended_sigma=0.16,
        nvda_cap_pct=30.0,
        fi_pct=10.0,
        provenance="test",
        classes=[
            AllocationClassDoc(
                label="Ex-US developed",
                snapshot_category="ex_us",
                sigma_class="ex_us",
                target_pct=100.0,
                instruments=[
                    AllocationInstrument(
                        symbol="EXUS",
                        role="primary",
                        weight_within_class_pct=100.0,
                        rationale="",
                        domicile="IE",
                    )
                ],
                agreement="",
                rationale="",
                dissent="",
            )
        ],
        glide=[],
    )


def _patch_doc(monkeypatch):
    import argosy.api.routes.portfolio as portfolio

    monkeypatch.setattr(
        portfolio,
        "_load_current_doc_and_holdings",
        lambda user_id, db=None: (_doc(), {"NVDA": 600_000.0, "SCHD": 264_000.0}, 0.0),
    )


def _enable(monkeypatch):
    monkeypatch.setenv("ARGOSY_DEPLOYMENT_AUTHOR_ENABLED", "1")
    from argosy.config import get_settings

    get_settings.cache_clear()
    from argosy.services.deploy_decision_team import TeamDecision

    monkeypatch.setattr(
        "argosy.services.deploy_decision_team.run_deploy_decision_team",
        lambda *a, **kw: TeamDecision(
            reviewers_ran=5,
            reviewers_expected=5,
            approved=list(getattr(a[1], "buys", []) or []),
            flagged=[],
        ),
    )

    def unexpected_inbox_write(*args, **kwargs):
        raise AssertionError("GET /deploy-cash must not mutate the inbox")

    monkeypatch.setattr(
        "argosy.services.deploy_decision_team.write_team_flag_proposals",
        unexpected_inbox_write,
    )
    monkeypatch.setattr(
        "argosy.services.deploy_decision_team.supersede_cleared_flags",
        unexpected_inbox_write,
    )


def _intent() -> AuthoredOrderIntent:
    return AuthoredOrderIntent(
        thesis="Close the ex-US sleeve gap.",
        thesis_type="diversifier",
        falsifier="The fund becomes materially US-heavy.",
        catalyst_description="Next quarterly rebalance",
        catalyst_date="2026-11-30",
        expectation="Ex-US allocation approaches target.",
        expectation_due_date="2026-12-31",
        success_measure="sleeve gap closes without a concentration breach",
        expected_upside_multiple=2,
    )


def test_authored_absent_when_flag_off(monkeypatch):
    _patch_doc(monkeypatch)
    from argosy.config import get_settings

    get_settings.cache_clear()  # ensure default (off)
    client = TestClient(create_app())
    body = client.get("/api/portfolio/deploy-cash", params={"cash_usd": 180000}).json()
    assert body.get("authored") is None
    get_settings.cache_clear()


def test_authored_accepted_is_primary(monkeypatch):
    _patch_doc(monkeypatch)
    _enable(monkeypatch)

    def fake_author(packet, **kw):
        return AuthorOutcome(
            status="accepted",
            proposal=AllocationProposal(
                cash_to_deploy=180_000.0,
                buys=[
                    Buy(
                        symbol="EXUS",
                        amount_usd=180_000.0,
                        sleeve="ex-US",
                        claimed_us_weight=0.0,
                        justification="true ex-US diversifier",
                        order_intent=_intent(),
                    )
                ],
                rationale="Directed to genuine ex-US on a concentrated book.",
            ),
            report=GateReport(status=GateStatus.ACCEPT, failures=[]),
            attempts=1,
        )

    monkeypatch.setattr(
        "argosy.services.allocation_author.reliable.authored_allocation", fake_author
    )
    client = TestClient(create_app())
    resp = client.get(
        "/api/portfolio/deploy-cash",
        params={"cash_usd": 180000},
    )
    assert resp.status_code == 200, resp.text
    a = resp.json()["authored"]
    assert a["status"] == "accepted" and a["degraded"] is False
    assert a["buys"][0]["symbol"] == "EXUS"
    from argosy.config import get_settings

    get_settings.cache_clear()


def test_requested_order_sheet_runs_through_real_route_and_validates(monkeypatch):
    _patch_doc(monkeypatch)
    _enable(monkeypatch)
    now = datetime.now(UTC)

    def fake_author(packet, **kw):
        return AuthorOutcome(
            status="accepted",
            proposal=AllocationProposal(
                cash_to_deploy=180_000,
                buys=[
                    Buy(
                        symbol="EXUS",
                        amount_usd=180_000,
                        sleeve="ex-US",
                        claimed_us_weight=0,
                        justification="closes ex-US gap",
                        order_intent=_intent(),
                    )
                ],
                rationale="One fully-funded list.",
            ),
            report=GateReport(status=GateStatus.ACCEPT, failures=[]),
            attempts=1,
        )

    monkeypatch.setattr(
        "argosy.services.allocation_author.reliable.authored_allocation",
        fake_author,
    )
    from argosy.services.order_sheet import MarketEvidence, VoiceVerdict
    from argosy.services.order_sheet_builder import ExecutionFacts

    monkeypatch.setattr(
        "argosy.services.order_sheet_facts.collect_execution_facts",
        lambda symbols, **kw: (
            {
                "EXUS": ExecutionFacts(
                    evidence=MarketEvidence(
                        price_usd=24,
                        price_as_of=now,
                        price_source="live test seam",
                        incorporation_country="IE",
                        incorporation_as_of=now,
                        incorporation_source="plan domicile",
                    ),
                    venue="LSE",
                    estate_situs="non_US",
                )
            },
            {},
        ),
    )
    hold_voice = VoiceVerdict(
        source="review",
        verdict="HOLD",
        as_of=now,
        rationale="fresh hold",
    )
    monkeypatch.setattr(
        "argosy.services.order_sheet_state.load_portfolio_voices",
        lambda *a, **kw: {"NVDA": [hold_voice], "SCHD": [hold_voice]},
    )

    client = TestClient(create_app())
    resp = client.get(
        "/api/portfolio/deploy-cash",
        params={"cash_usd": 180000, "include_order_sheet": True},
    )
    assert resp.status_code == 200, resp.text
    artifact = resp.json()["order_sheet"]
    assert artifact["status"] == "validated", artifact
    assert artifact["sheet"]["lines"][0]["shares"] == 7_500
    assert artifact["validation"]["buy_total_usd"] == 180_000
    assert {r["symbol"] for r in artifact["sheet"]["no_action"]} == {
        "NVDA",
        "SCHD",
    }
    from argosy.config import get_settings

    get_settings.cache_clear()


def test_authored_unavailable_is_degraded(monkeypatch):
    _patch_doc(monkeypatch)
    _enable(monkeypatch)

    def fake_author(packet, **kw):
        return AuthorOutcome(status="unavailable", proposal=None, report=None, attempts=1)

    monkeypatch.setattr(
        "argosy.services.allocation_author.reliable.authored_allocation", fake_author
    )
    client = TestClient(create_app())
    resp = client.get(
        "/api/portfolio/deploy-cash",
        params={"cash_usd": 180000, "include_order_sheet": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    a = body["authored"]
    assert a["status"] == "unavailable" and a["degraded"] is True
    assert any("degraded" in n.lower() for n in a["notes"])
    # The deterministic tiers remain as the labelled fallback.
    assert body["tiers"]
    assert body["order_sheet"]["status"] == "unavailable"
    assert body["order_sheet"]["failures"] == [
        "deployment author status is unavailable"
    ]
    from argosy.config import get_settings

    get_settings.cache_clear()


def test_packet_carries_no_tax_reserve_field(monkeypatch):
    _patch_doc(monkeypatch)
    _enable(monkeypatch)

    captured = {}

    def fake_author(packet, **kw):
        captured["packet"] = packet
        return AuthorOutcome(
            status="accepted",
            proposal=AllocationProposal(
                cash_to_deploy=180_000.0, buys=[Buy(symbol="EXUS", amount_usd=180_000.0)]
            ),
            report=GateReport(status=GateStatus.ACCEPT, failures=[]),
            attempts=1,
        )

    monkeypatch.setattr(
        "argosy.services.allocation_author.reliable.authored_allocation", fake_author
    )
    client = TestClient(create_app())
    a = client.get("/api/portfolio/deploy-cash", params={"cash_usd": 180000}).json()["authored"]
    # No tax-reserve concept anywhere: not in the packet, not in the DTO.
    assert "cgt_liability_usd" not in captured["packet"]
    assert "cash_reserved_for_tax" not in a
    from argosy.config import get_settings

    get_settings.cache_clear()


def test_route_reauthors_material_warn_and_persists_resolution(monkeypatch):
    """A sizing dissent must change the proposal before a sheet can validate."""
    from argosy.services.deploy_decision_team import (
        run_deploy_decision_team as actual_run_deploy_decision_team,
    )

    _patch_doc(monkeypatch)
    _enable(monkeypatch)

    from argosy.services.allocation_author import packet_assembly

    original_assemble = packet_assembly.assemble_author_packet

    def assemble_without_environment_candidates(*args, **kwargs):
        packet = original_assemble(*args, **kwargs)
        packet["discovery_candidates"] = []
        return packet

    monkeypatch.setattr(
        packet_assembly,
        "assemble_author_packet",
        assemble_without_environment_candidates,
    )

    from argosy.agents.deployment_reviewer import (
        DeploymentReviewOutput,
        ReviewObjection,
    )
    review_round = 0

    def review_team(packet, proposal, **kwargs):
        nonlocal review_round
        review_round += 1

        def review(lens, packet, buys, **kwargs):
            objections = []
            if review_round == 1 and lens == "sizing":
                objections = [ReviewObjection(
                    ticker="EXUS",
                    concern="Use a smaller first tranche.",
                    severity="warn",
                    impact="changes_amount",
                    recommended_amount_usd=170_000,
                )]
            return DeploymentReviewOutput(lens=lens, objections=objections)

        return actual_run_deploy_decision_team(packet, proposal, review_fn=review)

    monkeypatch.setattr(
        "argosy.services.deploy_decision_team.run_deploy_decision_team",
        review_team,
    )

    first = AllocationProposal(
        cash_to_deploy=180_000,
        buys=[Buy(
            symbol="EXUS",
            amount_usd=180_000,
            sleeve="ex-US",
            claimed_us_weight=0,
            justification="close the ex-US gap",
            order_intent=_intent(),
        )],
        rationale="Initial proposal.",
    )
    revised = first.model_copy(update={
        "cash_to_deploy": 170_000,
        "cash_to_reserve": 10_000,
        "buys": [first.buys[0].model_copy(update={"amount_usd": 170_000})],
        "rationale": "Smaller first tranche after independent sizing review.",
    })

    def fake_author(packet, **kwargs):
        first_report = kwargs["verify"](first, packet)
        assert first_report.status == GateStatus.REVISION_REQUIRED
        assert "recommended amount $170,000" in first_report.failures[0].detail
        final_report = kwargs["verify"](revised, packet)
        assert final_report.status == GateStatus.ACCEPT
        return AuthorOutcome(
            status="accepted",
            proposal=revised,
            report=final_report,
            attempts=2,
        )

    monkeypatch.setattr(
        "argosy.services.allocation_author.reliable.authored_allocation",
        fake_author,
    )

    from argosy.services.order_sheet import MarketEvidence, VoiceVerdict
    from argosy.services.order_sheet_builder import ExecutionFacts

    now = datetime.now(UTC)
    monkeypatch.setattr(
        "argosy.services.order_sheet_facts.collect_execution_facts",
        lambda symbols, **kw: ({
            "EXUS": ExecutionFacts(
                evidence=MarketEvidence(
                    price_usd=25,
                    price_as_of=now,
                    price_source="fresh route test",
                    incorporation_country="IE",
                    incorporation_as_of=now,
                    incorporation_source="issuer",
                ),
                venue="LSE",
                estate_situs="non_US",
            )
        }, {}),
    )
    hold_voice = VoiceVerdict(
        source="review", verdict="HOLD", as_of=now, rationale="fresh hold"
    )
    monkeypatch.setattr(
        "argosy.services.order_sheet_state.load_portfolio_voices",
        lambda *a, **kw: {"NVDA": [hold_voice], "SCHD": [hold_voice]},
    )

    response = TestClient(create_app()).get(
        "/api/portfolio/deploy-cash",
        params={"cash_usd": 180_000, "include_order_sheet": True},
    )

    assert response.status_code == 200, response.text
    artifact = response.json()["order_sheet"]
    assert artifact["status"] == "validated", artifact
    assert artifact["sheet"]["lines"][0]["notional_usd"] == 170_000
    resolution = artifact["sheet"]["review_resolution"]
    assert resolution["one_voice"] is True
    assert resolution["rounds"] == 2
    assert resolution["objections"][0]["status"] == "resolved_by_re_review"

    from argosy.config import get_settings

    get_settings.cache_clear()


def test_revised_sale_amount_uses_one_run_scoped_quote(monkeypatch):
    from types import SimpleNamespace
    _patch_doc(monkeypatch)
    _enable(monkeypatch)
    monkeypatch.setattr("argosy.services.allocation_author.packet_assembly.assemble_author_packet", lambda *a, **k: {})
    quotes = []
    resolutions = []
    def quote(symbols, **kwargs):
        quotes.append(symbols)
        return {"NVDA": SimpleNamespace(evidence=SimpleNamespace(price_usd=219.6499 + len(quotes)))}, {}
    monkeypatch.setattr("argosy.services.order_sheet_facts.collect_execution_facts", quote)
    def tax(*args, **kwargs):
        resolutions.append((kwargs["gross_proceeds_usd"], kwargs["current_price_usd"]))
        return SimpleNamespace()
    monkeypatch.setattr("argosy.services.sale_tax_facts.resolve_authoritative_sale", tax)
    def arithmetic(proposal, packet, sale_resolver):
        sale_resolver("NVDA", 10000)
        sale_resolver("NVDA", 9000)
        sale_resolver("NVDA", 10000)
        return GateReport(status=GateStatus.BLOCK)
    monkeypatch.setattr("argosy.services.allocation_author.verifier.verify_allocation_proposal", arithmetic)
    def author(packet, **kwargs):
        kwargs["verify"](AllocationProposal(cash_to_deploy=0), packet)
        return AuthorOutcome(status="unavailable")
    monkeypatch.setattr("argosy.services.allocation_author.reliable.authored_allocation", author)
    response = TestClient(create_app()).get("/api/portfolio/deploy-cash", params={"cash_usd": 180000})
    assert response.status_code == 200, response.text
    assert quotes == [["NVDA"]]
    assert resolutions == [(10000, 220.6499), (9000, 220.6499)]
    from argosy.config import get_settings
    get_settings.cache_clear()


def test_recovery_phase_requires_pending_or_explicit_blocker(monkeypatch):
    from argosy.services.deploy_decision_team import TeamDecision
    from argosy.services.order_sheet import PendingResearch
    _patch_doc(monkeypatch)
    _enable(monkeypatch)
    monkeypatch.setattr(
        "argosy.services.allocation_author.verifier.verify_allocation_proposal",
        lambda *a, **kw: GateReport(status=GateStatus.ACCEPT),
    )
    rounds = []
    def review(*args, **kwargs):
        rounds.append(1)
        if len(rounds) == 3:
            assert args[0]["recovery_review_objections"][0]["symbol"] == "AAA"
        return TeamDecision(reviewers_ran=5, reviewers_expected=5, approved=[], flagged=[{
            "symbol": "AAA", "objections": [{"impact": "changes_selection", "severity": "warn",
                "recommended_ticker": "BBB", "concern": "The source evidence does not settle this comparison"}],
        }])
    monkeypatch.setattr("argosy.services.deploy_decision_team.run_deploy_decision_team", review)
    def author(packet, **kwargs):
        verify = kwargs["verify"]
        proposal = AllocationProposal(cash_to_deploy=0, cash_to_reserve=180000)
        assert verify(proposal, packet).status == GateStatus.REVISION_REQUIRED
        second = verify(proposal, packet)
        assert second.status == GateStatus.REVISION_REQUIRED
        assert any(f.code == "core_research_recovery" for f in second.failures)
        missing = verify(proposal, packet)
        assert missing.status == GateStatus.BLOCK
        assert missing.failures[0].code == "core_research_recovery_incomplete"
        blocker = verify(proposal.model_copy(update={"research_separation_blocker": "Shared sale funding is unsafe"}), packet)
        assert blocker.status == GateStatus.BLOCK
        assert "Shared sale funding" in blocker.failures[0].detail
        assert len(rounds) == 2
        pending = PendingResearch(tickers=["AAA", "BBB"], disagreement="Evidence unresolved",
            missing_evidence="Source endpoint", research_question="What supports the endpoint?",
            next_review_date="2026-12-31", reserved_usd=10000, independence_reason="Separate funded reserve")
        for tickers in (["UNRELATED"], ["AAA"]):
            incomplete = verify(proposal.model_copy(update={"pending_research": [pending.model_copy(update={"tickers": tickers})]}), packet)
            assert incomplete.status == GateStatus.BLOCK
            assert incomplete.failures[0].code == "core_research_coverage_missing"
            assert "BBB" in incomplete.failures[0].detail
        assert len(rounds) == 2
        still_disputed = verify(proposal.model_copy(update={"pending_research": [pending]}), packet)
        assert len(rounds) == 3  # Recovery still reaches the full team.
        assert still_disputed.status == GateStatus.BLOCK  # It cannot override fresh dissent.
        return AuthorOutcome(status="rejected", proposal=proposal, report=blocker, attempts=3)
    monkeypatch.setattr("argosy.services.allocation_author.reliable.authored_allocation", author)
    response = TestClient(create_app()).get("/api/portfolio/deploy-cash", params={"cash_usd": 180000})
    assert response.status_code == 200, response.text
    assert len(rounds) == 3
    from argosy.config import get_settings
    get_settings.cache_clear()


def test_zero_cash_current_recommendations_reach_author_as_sell_funded_switch(
    monkeypatch,
):
    """The autonomous chain must not stop merely because idle cash is zero."""
    _patch_doc(monkeypatch)
    _enable(monkeypatch)
    current = [
        {
            "proposal_id": 38,
            "ticker": "NVDA",
            "action": "sell",
            "size": 519.0,
            "size_units": "shares",
            "rationale": "Trim concentration and fund plan diversification.",
            "confidence": "MEDIUM",
            "source": "verdict_trigger_sweep",
            "decision_run_id": 481,
            "created_at": "2026-08-26T04:55:02+00:00",
            "expires_at": None,
        },
        {
            "proposal_id": 39,
            "ticker": "GLUE",
            "action": "buy",
            "size": 26_000.0,
            "size_units": "currency",
            "rationale": "Fresh moonshot candidate with funded optionality.",
            "confidence": "MEDIUM",
            "source": "decision_funnel",
            "decision_run_id": 491,
            "created_at": "2026-08-26T15:47:14+00:00",
            "expires_at": "2026-08-29T15:30:07+00:00",
        },
    ]
    monkeypatch.setattr(
        "argosy.services.current_recommendations.load_actionable_recommendations",
        lambda *a, **kw: current,
    )
    captured = {}

    def fake_author(packet, **kw):
        captured["packet"] = packet
        return AuthorOutcome(
            status="accepted",
            proposal=AllocationProposal(
                cash_to_deploy=26_000,
                buys=[Buy(symbol="GLUE", amount_usd=26_000)],
                sells=[Sell(symbol="NVDA", amount_usd=35_000)],
                rationale="One after-tax sell-funded switch.",
            ),
            report=GateReport(status=GateStatus.ACCEPT, failures=[]),
            attempts=1,
        )

    monkeypatch.setattr(
        "argosy.services.allocation_author.reliable.authored_allocation",
        fake_author,
    )
    client = TestClient(create_app())
    resp = client.get(
        "/api/portfolio/deploy-cash",
        params={"cash_usd": 0, "allow_sells": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["authored"]["status"] == "accepted"
    assert captured["packet"]["deployable_usd"] == 0
    assert [row["proposal_id"] for row in captured["packet"]["current_recommendations"]] == [
        38,
        39,
    ]
    assert captured["packet"]["allow_sells"] is True

    from argosy.config import get_settings

    get_settings.cache_clear()
