"""Tests for the alternatives subflow (source -> verify -> debate -> decide).

The three agent seams are monkeypatched; the verification + assembly logic is
real. Covers: hallucinated-only -> 0% without debate, clean+approve -> sleeve
with computed sigma, verification-empty -> reviewers never run, <2 reviewers ->
insufficient_data, FM 0% -> 0% sleeve, FM picks an unknown symbol -> 0%.
"""
from __future__ import annotations

import argosy.orchestrator.flows.plan_synthesis.alternatives_phase as phase
from argosy.agents.alternatives_reviewers import AltFundManagerVerdict, AltReviewReport, AltSelection
from argosy.agents.alternatives_sourcer import AlternativesProposal, AssetProposal


def _asset(symbol, domicile, isin, weight, asset_class="precious_metals"):
    return AssetProposal(
        symbol=symbol, name=f"{symbol} fund", asset_class=asset_class, domicile=domicile,
        isin=isin, weight_within_sleeve_pct=weight, conviction="HIGH",
        thesis_md="diversifier", cites=["src"],
    )


def _proposal(assets):
    return AlternativesProposal(
        sleeve_pct=3.0, rationale_md="x", proposals=assets, cited_sources=["src"]
    )


def _reviews(n=3):
    return [
        AltReviewReport(stance="support", sleeve_pct_view=3.0, key_points_md="ok")
        for _ in range(n)
    ]


def _patch(monkeypatch, *, proposal, reviews=None, verdict=None, reviewer_spy=None):
    monkeypatch.setattr(phase, "_run_sourcer", lambda *a, **k: proposal)
    if reviewer_spy is not None:
        monkeypatch.setattr(phase, "_run_reviewers", reviewer_spy)
    elif reviews is not None:
        monkeypatch.setattr(phase, "_run_reviewers", lambda *a, **k: reviews)
    if verdict is not None:
        monkeypatch.setattr(phase, "_run_fund_manager", lambda *a, **k: verdict)


def test_hallucinated_only_yields_zero_percent_without_debate(monkeypatch):
    called = {"reviewers": False}

    def _spy(*a, **k):
        called["reviewers"] = True
        return _reviews()

    _patch(monkeypatch,
           proposal=_proposal([_asset("HALLUC", "JE", "JE00FAKE0000", 100.0, "crypto")]),
           reviewer_spy=_spy)
    decision = phase.run_alternatives_phase(user_id="ariel", macro_context={})
    assert decision.decision == "0_percent"
    assert decision.target_pct == 0.0 and not decision.instruments
    # Hard gate first: no verified candidates => reviewers never run.
    assert called["reviewers"] is False


def test_clean_proposal_with_approval_yields_sleeve(monkeypatch):
    verdict = AltFundManagerVerdict(
        decision="approve", target_pct=3.0,
        selected=[AltSelection(symbol="SGLD", weight_within_sleeve_pct=80.0),
                  AltSelection(symbol="IGLN", weight_within_sleeve_pct=20.0)],
        rationale_md="hold a gold sleeve",
    )
    _patch(monkeypatch,
           proposal=_proposal([_asset("SGLD", "IE", "IE00B579F325", 80.0),
                               _asset("IGLN", "IE", "IE00B4ND3602", 20.0)]),
           reviews=_reviews(3), verdict=verdict)
    decision = phase.run_alternatives_phase(user_id="ariel", macro_context={})
    assert decision.decision == "approve"
    assert decision.target_pct == 3.0
    assert {i.symbol for i in decision.instruments} == {"SGLD", "IGLN"}
    # gold-only sleeve -> sourced sigma 0.16 (not the fixed 0.268)
    assert round(decision.sleeve_sigma, 3) == 0.16


def test_insufficient_reviewers_yields_insufficient_data(monkeypatch):
    verdict = AltFundManagerVerdict(decision="approve", target_pct=3.0, selected=[], rationale_md="x")
    _patch(monkeypatch,
           proposal=_proposal([_asset("SGLD", "IE", "IE00B579F325", 100.0)]),
           reviews=_reviews(1), verdict=verdict)
    decision = phase.run_alternatives_phase(user_id="ariel", macro_context={})
    assert decision.decision == "insufficient_data"
    assert decision.target_pct == 0.0


def test_fund_manager_zero_percent(monkeypatch):
    verdict = AltFundManagerVerdict(
        decision="0_percent", target_pct=0.0, selected=[], rationale_md="not worth the risk"
    )
    _patch(monkeypatch,
           proposal=_proposal([_asset("SGLD", "IE", "IE00B579F325", 100.0)]),
           reviews=_reviews(3), verdict=verdict)
    decision = phase.run_alternatives_phase(user_id="ariel", macro_context={})
    assert decision.decision == "0_percent"
    assert not decision.instruments


def test_fund_manager_unknown_symbol_drops_to_zero(monkeypatch):
    verdict = AltFundManagerVerdict(
        decision="approve", target_pct=3.0,
        selected=[AltSelection(symbol="NOTVERIFIED", weight_within_sleeve_pct=100.0)],
        rationale_md="x",
    )
    _patch(monkeypatch,
           proposal=_proposal([_asset("SGLD", "IE", "IE00B579F325", 100.0)]),
           reviews=_reviews(3), verdict=verdict)
    decision = phase.run_alternatives_phase(user_id="ariel", macro_context={})
    assert decision.decision == "insufficient_data"
    assert not decision.instruments


def test_fund_manager_target_clamped_to_cap(monkeypatch):
    verdict = AltFundManagerVerdict(
        decision="approve", target_pct=9.0,
        selected=[AltSelection(symbol="SGLD", weight_within_sleeve_pct=100.0)],
        rationale_md="too big",
    )
    _patch(monkeypatch,
           proposal=_proposal([_asset("SGLD", "IE", "IE00B579F325", 100.0)]),
           reviews=_reviews(3), verdict=verdict)
    decision = phase.run_alternatives_phase(user_id="ariel", macro_context={})
    assert decision.target_pct == 4.0
    assert any("clamp" in v.lower() for v in decision.violations)
