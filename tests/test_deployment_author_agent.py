"""DeploymentAuthorAgent — the LLM that AUTHORS the AllocationProposal. We test the
prompt renders the load-bearing facts + the verifier feedback on a revision; the
live call is exercised via the reliability wrapper + flow tests (no LLM here)."""
from __future__ import annotations

import json

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.deployment_author import DeploymentAuthorAgent
from argosy.services.allocation_author.proposal import AllocationProposal
from argosy.services.allocation_author.verifier import GateFailure


def _packet():
    return {
        "deployable_usd": 180_000.0,
        "holdings": {"NVDA": 600_000.0, "SCHD": 264_000.0},
        "known_symbols": {"EXUS", "SPMV", "EIMI", "NVDA", "SCHD"},
        "plan_menu": [
            {"sleeve": "Ex-US developed", "target_pct": 15.0, "current_pct": 2.7,
             "gap_to_target_pct": 12.3, "tickers": ["EXUS"], "domiciles": ["IE"]},
            {"sleeve": "US low-vol", "target_pct": 20.0, "current_pct": 0.0,
             "gap_to_target_pct": 20.0, "tickers": ["SPMV"], "domiciles": ["IE"]},
        ],
        "nvda": {"lookthrough_usd": 600_000.0, "book_usd": 1_000_000.0,
                 "pct": 60.0, "cap_pct": 30.0},
        "reserve": {"target_usd": 100_000.0, "current_usd": 127_000.0,
                    "shortfall_usd": 0.0},
        "instrument_facts": [
            {"symbol": "FWRA", "us_weight": 0.62,
             "source": "FTSE All-World factsheet", "confidence": "verified"},
            {"symbol": "EXUS", "us_weight": 0.0,
             "source": "MSCI World ex-USA", "confidence": "verified"},
        ],
        "policy_signals": {"nvda_policy_sell": {"due": False}},
        "decision_calibration": {
            "benchmark": {
                "symbol": "SPY",
                "compared": 6,
                "beat_rate": 0.5,
                "mean_excess_return_pct": -0.01,
                "recent": [{
                    "ticker": "GLUE",
                    "recommendation": "BUY",
                    "subject_return_pct": 0.12,
                    "benchmark_return_pct": 0.20,
                    "decision_excess_return_pct": -0.08,
                }],
            },
            "order_sheet": {
                "scored_predictions": 4,
                "hit_rate": 0.5,
                "sample_size_warning": True,
                "is_stale": False,
            },
            "sources": [{
                "source": "internal_per_position_thesis",
                "scored": 20,
                "hit_rate": 0.4,
                "mean_pnl_pct": -0.02,
                "sample_size_warning": False,
                "is_stale": False,
            }],
            "recent_verdict_outcomes": [{
                "ticker": "SCHD", "verdict": "SELL", "grade": "miss",
                "price_move_pct": 3.0,
            }],
        },
        "user_constraints": "earliest safe retirement; reduce NVDA toward cap",
    }


def test_agent_config():
    a = DeploymentAuthorAgent(user_id="ariel")
    assert a.agent_role == "deployment_author"
    assert a.output_model is AllocationProposal
    assert a.require_citations is False


def test_author_compares_feasible_uses_without_cash_concentration_veto():
    system, _ = DeploymentAuthorAgent(user_id="ariel").build_prompt(packet=_packet())
    assert "retaining current holdings" in system
    assert "after tax and friction over the same horizon" in system
    assert "not an automatic concentration veto" in system
    assert "do not compare a plan-approved" not in system


@pytest.mark.real_seam
def test_real_agent_dispatch_parses_authored_proposal(monkeypatch):
    """Exercise BaseAgent.run; only the external model call is replaced."""
    payload = {
        "cash_to_deploy": 180_000,
        "cash_to_reserve": 0,
        "buys": [{
            "symbol": "EXUS",
            "amount_usd": 180_000,
            "sleeve": "Ex-US developed",
            "justification": "Fill the largest verified plan gap.",
            "claimed_us_weight": 0,
        }],
        "sells": [],
        "holds": ["NVDA", "SCHD"],
        "rationale": "One funded list.",
    }
    async def fake_call(self, *, system, user, **kwargs):
        assert "EXUS" in user and "deployment" in system.lower()
        return ModelCall(
            text=json.dumps(payload), tokens_in=10, tokens_out=10, model="test-model"
        )

    monkeypatch.setattr(DeploymentAuthorAgent, "_call_model", fake_call)
    report = DeploymentAuthorAgent(user_id="ariel").run_sync(packet=_packet())
    assert isinstance(report.output, AllocationProposal)
    assert report.output.buys[0].symbol == "EXUS"


def test_prompt_carries_the_judgment_calls():
    a = DeploymentAuthorAgent(user_id="ariel")
    system, user = a.build_prompt(packet=_packet())
    blob = (system + "\n" + user).lower()
    # concentration — don't add US to a 60%-NVDA book
    assert "60" in user and "nvda" in blob
    # look-through — FWRA is US-heavy, not ex-US
    assert "fwra" in blob and "62" in user
    # plan-menu-only + conservation + per-buy us weight claim
    assert "exus" in blob and "spmv" in blob
    assert "claimed_us_weight" in blob
    # domicile awareness
    assert "domicile" in blob or "ucits" in blob
    # net-of-tax: the prompt must NOT tell the author to pre-reserve a future sale's tax
    assert "net-of-tax" in blob
    assert "cash_reserved_for_tax" not in blob
    # plan-fit from within: per-sleeve gap shown + instruction to fill under-target first
    assert "gap" in blob and "under-target" in blob
    assert "12.3" in user  # the ex-US sleeve's supplied gap
    # Closed-loop calibration reaches the author as context, not a hard gate.
    assert "outcome calibration" in blob
    assert "schd sell: miss" in blob
    assert "s&p-relative decision evidence" in blob
    assert "glue buy" in blob and "decision excess -8.0%" in blob
    assert "uncalibrated" in blob
    assert "not a mechanical gate" in blob


def test_prompt_treats_sub_one_percent_moonshots_as_convexity_not_a_floor():
    system, _ = DeploymentAuthorAgent(user_id="ariel").build_prompt(packet=_packet())
    blob = system.lower()
    assert "zero, one, or multiple moonshot names" in blob
    assert "reject one merely because" in blob
    assert "below 1%" in blob
    assert ">=5x" in blob
    assert "splitting the sleeve" in blob
    assert "position-size floor" in blob
    assert "not as a command to touch every" in blob
    assert "moonshot sleeve may use us-situs" in blob
    assert "fmv-at-death" in blob
    assert "probability-aware convexity sizing" in blob
    assert "outcome_scenarios" in blob
    assert "probabilities must sum to 100" in blob
    assert "possible 10x with no probability is not a sizing case" in blob


def test_prompt_requires_comparison_of_same_grade_discovery_finalists():
    packet = _packet()
    packet["discovery_candidates"] = [
        {
            "ticker": ticker,
            "rank": rank,
            "score": score,
            "fresh_as_of": "2026-08-26T06:00:00+00:00",
            "fleet": {
                "verdict": "BUY",
                "conviction": "MED",
                "thesis_md": f"{ticker} has a distinct evidence-backed thesis.",
            },
        }
        for ticker, rank, score in (
            ("GLUE", 26, 76.9), ("REPL", 16, 77.5), ("QURE", 17, 77.5)
        )
    ]
    system, user = DeploymentAuthorAgent(user_id="ariel").build_prompt(packet=packet)
    blob = system + "\n" + user
    assert "DISCOVERY FINALISTS REQUIRING EXPLICIT" in blob
    assert all(ticker in blob for ticker in ("GLUE", "REPL", "QURE"))
    assert "candidate_comparisons" in system
    assert "Coarse BUY/MED grades are not a tie-breaker" in system
    assert "recommended_position_usd" in system
    assert "same horizon" in system
    assert "$10k-vs-$26k explicit" in system
    assert "smaller_position_usd" in system and "larger_position_usd" in system
    assert "split_considered" in system and "split_why" in system
    assert "QUALITATIVE ONLY" in system
    assert "explicitly reconcile that reversal" in system
    assert "Do not merely assert" in system
    assert all(
        field in system
        for field in (
            "`ticker`",
            "`selection`",
            "`evidence_fresh_as_of`",
            "`key_advantage`",
            "`key_risk`",
            "`why`",
        )
    )


def test_candidate_comparison_accepts_fleet_aliases_without_losing_requirements():
    proposal = AllocationProposal.model_validate({
        "cash_to_deploy": 0,
        "candidate_comparisons": [{
            "symbol": "REPL",
            "status": "rejected",
            "rank": 16,
            "score": 77.5,
            "verdict": "BUY",
            "conviction": "MED",
            "fresh_as_of": "2026-08-26T06:00:00+00:00",
            "advantage": "Commercial-stage platform evidence.",
            "risk": "Short runway and dilution risk.",
            "rationale": "Not selected because the catalyst-adjusted payoff lost comparatively.",
        }],
    })
    row = proposal.candidate_comparisons[0]
    assert row.ticker == "REPL"
    assert row.selection == "NOT_SELECTED"
    assert row.radar_rank == 16
    assert row.evidence_fresh_as_of.isoformat() == "2026-08-26T06:00:00+00:00"


def test_order_rows_accept_ticker_alias_from_fleet_json():
    proposal = AllocationProposal.model_validate({
        "cash_to_deploy": 10_000,
        "buys": [{"ticker": "CSPX", "amount_usd": 10_000}],
        "sells": [{"ticker": "NVDA", "amount_usd": 0}],
    })
    assert proposal.buys[0].symbol == "CSPX"
    assert proposal.sells[0].symbol == "NVDA"


def test_revision_prompt_includes_verifier_failures():
    a = DeploymentAuthorAgent(user_id="ariel")
    fb = [
        GateFailure("lookthrough_claim",
                    "FWRA is ~62% US — it cannot be treated as ex-US.", "revision"),
        GateFailure("conservation",
                    "deploy+reserve $170,000 != deployable $180,000.", "revision"),
    ]
    system, user = a.build_prompt(packet=_packet(), feedback=fb)
    assert "FWRA is ~62% US" in user
    assert "!= deployable" in user
    assert "revise" in user.lower() or "correct" in user.lower()


def test_author_does_not_implement_infeasible_reviewer_fragment():
    system, _ = DeploymentAuthorAgent(user_id="ariel").build_prompt(packet=_packet())
    assert "whole resulting position, not the addition" in system
    assert "no requirement to fund every core sleeve" in system
    assert "reviewer-requested replacement must satisfy these same funded sizing constraints" in system
    assert "without increasing a staged sale ceiling" in system


def test_no_feedback_prompt_has_no_revision_block():
    a = DeploymentAuthorAgent(user_id="ariel")
    _, user = a.build_prompt(packet=_packet(), feedback=None)
    assert "previous proposal" not in user.lower()


def test_prompt_keeps_staged_sale_policy_and_pending_research_together():
    packet = _packet()
    packet["staged_sell_policies"] = {"NVDA": {"max_gross_usd": 50000, "execution_style": "staged_tranche"}}
    packet["allocation_research_tasks"] = [{"tickers": ["KURA", "TYRA"], "research_question": "Source the endpoint"}]
    agent = DeploymentAuthorAgent.__new__(DeploymentAuthorAgent)
    _, prompt = agent.build_prompt(packet=packet)
    assert "staged_tranche" in prompt
    assert "Source the endpoint" in prompt


def test_recovery_feedback_is_an_explicit_phase_not_optional_ticker_advice():
    agent = DeploymentAuthorAgent.__new__(DeploymentAuthorAgent)
    system, _ = agent.build_prompt(packet=_packet(), feedback=[
        GateFailure("core_research_recovery", "Separate the unresolved choice", "revision"),
    ])
    assert "CURRENT PHASE: INDEPENDENT CORE / PENDING RESEARCH RECOVERY" in system
    assert "Do NOT propose another" in system
    assert "research_separation_blocker" in system
