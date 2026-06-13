"""Tests for the ETP-aware alternatives reviewer fleet + sleeve fund-manager.

No live LLM — build_prompt / schema / role wiring are deterministic. The key
contract: reviewer prompts are wrapper/structure-oriented (NOT equity-valuation),
and the FM can land on a 0% sleeve.
"""
from __future__ import annotations

import pytest

from argosy.agents.alternatives_reviewers import (
    AltExposureStructureAnalyst,
    AltFundManagerVerdict,
    AltMacroDiversificationAnalyst,
    AltReviewReport,
    AltRiskLiquidityTaxAnalyst,
    AlternativesFundManagerAgent,
)
from argosy.services.alternatives_types import (
    VerificationEvidence,
    VerificationResult,
    VerifiedAlternativesCandidate,
)


def _candidate(symbol="SGLD"):
    return VerifiedAlternativesCandidate(
        symbol=symbol, name="Invesco Physical Gold ETC", asset_class="precious_metals",
        domicile="IE", isin="IE00B579F325", weight_within_sleeve_pct=100.0,
        conviction="HIGH", thesis_md="gold hedge",
        verification=VerificationResult(
            symbol=symbol, verified=True, severity="GREEN", reason="ok",
            evidence=VerificationEvidence(
                isin_checksum_ok=True, isin_prefix="IE", domicile_coherent=True,
                registry_hit=True, source_url="https://issuer"),
            resolved_isin="IE00B579F325", resolved_domicile="IE"),
    )


REVIEWERS = [
    AltExposureStructureAnalyst,
    AltMacroDiversificationAnalyst,
    AltRiskLiquidityTaxAnalyst,
]


@pytest.mark.parametrize("cls", REVIEWERS)
def test_reviewer_role_and_output_model(cls):
    agent = cls(user_id="ariel")
    assert agent.agent_role in {
        "alt_exposure_structure", "alt_macro_diversification", "alt_risk_liquidity_tax",
    }
    assert agent.output_model is AltReviewReport
    # Roles resolve to a non-Haiku model via the registry.
    assert "haiku" not in (agent.model or "").lower()


@pytest.mark.parametrize("cls", REVIEWERS)
def test_reviewer_prompt_is_etp_not_equity(cls):
    agent = cls(user_id="ariel")
    system, user = agent.build_prompt(
        verified_candidates=[_candidate()], macro_context={"regime": "late-cycle"}
    )
    blob = (system + user).lower()
    assert "wrapper" in blob
    # NEVER asks for operating-company valuation inputs.
    assert "fair value" not in blob
    assert "p/e" not in blob and "ev/ebitda" not in blob
    # The verified candidate is presented to the lens.
    assert "sgld" in blob


def test_reviewer_report_supports_zero_view():
    r = AltReviewReport(stance="oppose", sleeve_pct_view=0.0, key_points_md="no case")
    assert r.sleeve_pct_view == 0.0


def test_fund_manager_role_and_schema():
    fm = AlternativesFundManagerAgent(user_id="ariel")
    assert fm.agent_role == "alternatives_fund_manager"
    assert fm.output_model is AltFundManagerVerdict


def test_fund_manager_opts_out_of_citation_gate():
    # AltFundManagerVerdict has no cited_sources field; the citation gate (on by
    # default in BaseAgent) would otherwise fail every FM run.
    assert AlternativesFundManagerAgent(user_id="ariel").require_citations is False


def test_reviewers_opt_out_of_citation_gate():
    # Reviewers give qualitative judgement over already-cited verified candidates
    # and legitimately reason without quoting a document; a non-empty cited_sources
    # hard gate made them fail live. cited_sources stays optional, not gated.
    for cls in REVIEWERS:
        assert cls(user_id="ariel").require_citations is False


def test_fund_manager_prompt_allows_zero_percent():
    fm = AlternativesFundManagerAgent(user_id="ariel")
    system, user = fm.build_prompt(
        verified_candidates=[_candidate()],
        reviews=[AltReviewReport(stance="neutral", sleeve_pct_view=2.0, key_points_md="x")],
        macro_context={},
    )
    blob = (system + user).lower()
    assert "0_percent" in blob or "0%" in blob
    assert "insufficient_data" in blob


def test_fm_verdict_zero_percent_round_trips():
    v = AltFundManagerVerdict(
        decision="0_percent", target_pct=0.0, selected=[],
        rationale_md="risk not worth it",
    )
    assert v.target_pct == 0.0 and not v.selected
