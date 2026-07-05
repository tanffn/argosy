"""The deploy decision team: blind reviewer prompt + objection reconciliation +
fail-open. No live LLM — review_fn injected."""
from __future__ import annotations

from types import SimpleNamespace

from argosy.agents.deployment_reviewer import (
    DeploymentReviewerAgent,
    DeploymentReviewOutput,
    ReviewObjection,
)
from argosy.services.deploy_decision_team import run_deploy_decision_team


def _buy(sym, amt, sleeve=""):
    return SimpleNamespace(symbol=sym, amount_usd=amt, sleeve=sleeve)


def _proposal(*buys):
    return SimpleNamespace(buys=list(buys))


def _packet():
    return {
        "nvda": {"pct": 58.0, "lookthrough_usd": 2_300_000, "book_usd": 3_990_000, "cap_pct": 13.0},
        "instrument_facts": [{"symbol": "R1GR", "us_weight": 1.0}, {"symbol": "EXUS", "us_weight": 0.0}],
        "plan_menu": [{"sleeve": "US growth", "target_pct": 11.0, "current_pct": 4.0}],
        "holdings": {"NVDA": 2_296_000.0},
    }


def test_reviewer_prompt_is_blind_and_lensed():
    agent = DeploymentReviewerAgent.__new__(DeploymentReviewerAgent)
    system, user = DeploymentReviewerAgent.build_prompt(
        agent, lens="concentration", packet=_packet(),
        buys=[{"symbol": "R1GR", "amount_usd": 18000, "sleeve": "US growth"}],
    )
    assert "YOUR LENS is concentration" in system
    assert "you have NOT seen its reasoning" in system   # blind
    assert "BUY R1GR $18,000" in user
    assert "reason withheld" in user                      # no author rationale leaks in


def test_team_flags_objected_buys_and_approves_the_rest():
    # Concentration reviewer refutes R1GR (NVDA-heavy); nothing objects to EXUS.
    def _review(lens, packet, buys, *, user_id="ariel"):
        if lens == "concentration":
            return DeploymentReviewOutput(lens=lens, objections=[
                ReviewObjection(ticker="R1GR", concern="~14% NVDA — not a diversifier", severity="block"),
            ])
        return DeploymentReviewOutput(lens=lens, objections=[])

    decision = run_deploy_decision_team(
        _packet(), _proposal(_buy("R1GR", 18000, "US growth"), _buy("EXUS", 26000, "Intl")),
        lenses=("concentration", "diversification", "prudence"),
        review_fn=_review,
    )
    assert [b.symbol for b in decision.approved] == ["EXUS"]
    assert len(decision.flagged) == 1 and decision.flagged[0]["symbol"] == "R1GR"
    assert decision.flagged[0]["objections"][0]["lens"] == "concentration"
    assert not decision.all_clear
    assert decision.reviewers_ran == 3 and not decision.degraded


def test_team_is_fail_open_when_a_reviewer_dies():
    def _review(lens, packet, buys, *, user_id="ariel"):
        if lens == "diversification":
            raise RuntimeError("claude.exe timeout")
        return DeploymentReviewOutput(lens=lens, objections=[])

    decision = run_deploy_decision_team(
        _packet(), _proposal(_buy("EXUS", 26000)),
        lenses=("concentration", "diversification", "prudence"),
        review_fn=_review,
    )
    # the dead reviewer is skipped, the trade is NOT blocked, and degradation is flagged
    assert decision.reviewers_ran == 2 and decision.reviewers_expected == 3
    assert decision.degraded is True
    assert decision.all_clear and [b.symbol for b in decision.approved] == ["EXUS"]


def test_team_decision_maps_to_dto():
    # The route maps a TeamDecision -> TeamReviewDTO; lock that shape.
    from argosy.services.contracts import TeamFlaggedBuyDTO, TeamObjectionDTO, TeamReviewDTO

    def _review(lens, packet, buys, *, user_id="ariel"):
        if lens == "concentration":
            return DeploymentReviewOutput(lens=lens, objections=[
                ReviewObjection(ticker="R1GR", concern="14% NVDA", severity="block")])
        return DeploymentReviewOutput(lens=lens, objections=[])

    d = run_deploy_decision_team(
        _packet(), _proposal(_buy("R1GR", 18000, "growth"), _buy("EXUS", 26000, "intl")),
        review_fn=_review,
    )
    dto = TeamReviewDTO(
        reviewers_ran=d.reviewers_ran, reviewers_expected=d.reviewers_expected,
        degraded=d.degraded, approved=[b.symbol for b in d.approved],
        flagged=[TeamFlaggedBuyDTO(symbol=f["symbol"], amount_usd=f["amount_usd"],
                 objections=[TeamObjectionDTO(**o) for o in f["objections"]]) for f in d.flagged],
    )
    assert dto.approved == ["EXUS"]
    assert dto.flagged[0].symbol == "R1GR"
    assert dto.flagged[0].objections[0].severity == "block"


def test_team_enriches_facts_with_nvda_lookthrough():
    captured = {}

    def _review(lens, packet, buys, *, user_id="ariel"):
        captured["facts"] = packet.get("instrument_facts")
        return DeploymentReviewOutput(lens=lens, objections=[])

    run_deploy_decision_team(_packet(), _proposal(_buy("R1GR", 18000)),
                             lenses=("concentration",), review_fn=_review)
    r1gr = next(f for f in captured["facts"] if f["symbol"] == "R1GR")
    assert r1gr["nvda_weight"] > 0.1  # raw NVDA ground truth handed to the reviewer
