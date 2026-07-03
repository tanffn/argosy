"""DeploymentAuthorAgent — the LLM that AUTHORS the AllocationProposal. We test the
prompt renders the load-bearing facts + the verifier feedback on a revision; the
live call is exercised via the reliability wrapper + flow tests (no LLM here)."""
from __future__ import annotations

from argosy.agents.deployment_author import DeploymentAuthorAgent
from argosy.services.allocation_author.proposal import AllocationProposal
from argosy.services.allocation_author.verifier import GateFailure


def _packet():
    return {
        "deployable_usd": 180_000.0,
        "holdings": {"NVDA": 600_000.0, "SCHD": 264_000.0},
        "known_symbols": {"EXUS", "SPMV", "EIMI", "NVDA", "SCHD"},
        "plan_menu": [
            {"sleeve": "Ex-US developed", "target_pct": 15.0,
             "tickers": ["EXUS"], "domiciles": ["IE"]},
            {"sleeve": "US low-vol", "target_pct": 20.0,
             "tickers": ["SPMV"], "domiciles": ["IE"]},
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
        "user_constraints": "earliest safe retirement; reduce NVDA toward cap",
    }


def test_agent_config():
    a = DeploymentAuthorAgent(user_id="ariel")
    assert a.agent_role == "deployment_author"
    assert a.output_model is AllocationProposal
    assert a.require_citations is False


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


def test_no_feedback_prompt_has_no_revision_block():
    a = DeploymentAuthorAgent(user_id="ariel")
    _, user = a.build_prompt(packet=_packet(), feedback=None)
    assert "previous proposal" not in user.lower()
