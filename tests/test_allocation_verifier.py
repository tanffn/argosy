"""The deployment VERIFIER — determinism gates the fleet's authored allocation
(ACCEPT / REVISION_REQUIRED / BLOCK). It checks facts; it never re-decides ("this
violates the facts", never "therefore buy X"). This is the spine of the fleet-
authors / determinism-verifies inversion.

The acceptance test IS the failure that motivated the pivot: a proposal that treats
FWRA (~62% US) as ex-US diversification, or that skips the known NVDA-sale CGT
reserve, must be bounced for revision — not silently accepted.
"""
from __future__ import annotations

from argosy.services.allocation_author.proposal import AllocationProposal, Buy, Sell
from argosy.services.allocation_author.verifier import GateStatus, verify_allocation_proposal


def _packet(**over):
    p = {
        "deployable_usd": 180_000.0,
        "holdings": {"SCHD": 264_000.0, "NVDA": 2_296_000.0},
        "known_symbols": {"FUSA", "SPMV", "EXUS", "FWRA", "CSPX", "SCHD", "NVDA", "VEUR"},
    }
    p.update(over)
    return p


def _ok_proposal():
    # Deploys the full net-of-tax amount into a TRUE ex-US fund (EXUS us≈0) + a
    # low-vol sleeve. No tax reserve — CGT is paid from the sale that realizes it.
    return AllocationProposal(
        cash_to_deploy=180_000.0, cash_to_reserve=0.0,
        buys=[Buy(symbol="EXUS", amount_usd=130_000.0, sleeve="International developed (ex-US)",
                  justification="true ex-US diversification", claimed_us_weight=0.0),
              Buy(symbol="SPMV", amount_usd=50_000.0, sleeve="US low-volatility",
                  justification="uncovered low-vol factor", claimed_us_weight=1.0)],
        sells=[], holds=[], rationale="diversify ex-US",
    )


def test_clean_proposal_accepts():
    r = verify_allocation_proposal(_ok_proposal(), _packet())
    assert r.status == GateStatus.ACCEPT, r.failures


def test_fwra_treated_as_exus_is_bounced():
    """The exact failure: buying FWRA and calling it ex-US, when the registry knows
    FWRA is ~62% US. Must be REVISION_REQUIRED, not accepted."""
    p = AllocationProposal(
        cash_to_deploy=180_000.0, cash_to_reserve=0.0,
        buys=[Buy(symbol="FWRA", amount_usd=180_000.0,
                  sleeve="International developed (ex-US)",
                  justification="ex-US diversification", claimed_us_weight=0.0)],
        sells=[], holds=[], rationale="x",
    )
    r = verify_allocation_proposal(p, _packet())
    assert r.status == GateStatus.REVISION_REQUIRED
    assert any("FWRA" in f.detail and "US" in f.detail for f in r.failures)


def test_conservation_failure_is_bounced():
    p = _ok_proposal().model_copy(update={"cash_to_deploy": 999_999.0})
    r = verify_allocation_proposal(p, _packet())
    assert r.status in (GateStatus.REVISION_REQUIRED, GateStatus.BLOCK)
    assert any("conserv" in f.detail.lower() or "sum" in f.detail.lower() for f in r.failures)


def test_sell_exceeding_holdings_is_blocked():
    p = _ok_proposal().model_copy(update={
        "sells": [Sell(symbol="SCHD", amount_usd=500_000.0, reason="migrate")],
    })
    r = verify_allocation_proposal(p, _packet())
    assert r.status == GateStatus.BLOCK
    assert any("SCHD" in f.detail for f in r.failures)


def test_invented_ticker_is_blocked():
    p = _ok_proposal().model_copy(update={
        "buys": [Buy(symbol="ZZZZ", amount_usd=80_000.0, sleeve="?", justification="?",
                     claimed_us_weight=0.0)],
    })
    r = verify_allocation_proposal(p, _packet())
    assert r.status == GateStatus.BLOCK
    assert any("ZZZZ" in f.detail for f in r.failures)


def test_schema_forbids_negative_money():
    """Defense-in-depth: the schema itself rejects a negative reserve/deploy/amount."""
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AllocationProposal(cash_to_deploy=80_000.0, cash_to_reserve=-100.0)


def test_negative_reserve_balancing_overdeploy_is_blocked():
    """The exploit: a negative reserve balances an over-deploy through the pure
    equality checks. Built via model_construct to simulate a schema bypass — the
    verifier must BLOCK it regardless (it's the authoritative money gate)."""
    p = AllocationProposal.model_construct(
        cash_to_deploy=180_100.0, cash_to_reserve=-100.0,
        buys=[Buy.model_construct(symbol="EXUS", amount_usd=180_100.0, sleeve="ex-US",
                                  justification="", claimed_us_weight=0.0)],
        sells=[], holds=[], rationale="",
    )
    r = verify_allocation_proposal(p, _packet())
    assert r.status == GateStatus.BLOCK
    assert any(f.code == "negative_amount" for f in r.failures)


def test_missing_claimed_us_weight_is_bounced():
    """A buy with no claimed_us_weight can't be cross-checked — must be REVISION."""
    p = _ok_proposal().model_copy(update={
        "buys": [Buy(symbol="EXUS", amount_usd=80_000.0, sleeve="ex-US",
                     justification="", claimed_us_weight=None)],
    })
    r = verify_allocation_proposal(p, _packet())
    assert r.status == GateStatus.REVISION_REQUIRED
    assert any(f.code == "missing_us_weight" for f in r.failures)


def test_fwra_evasion_via_neutral_sleeve_still_caught():
    """The evasion the reviewer flagged: buy FWRA into a 'Global diversifier' sleeve
    with no 'ex-US' words. Omitting claimed_us_weight now trips missing_us_weight;
    supplying a false 0.0 trips lookthrough_claim. Either way it can't pass ACCEPT."""
    omitted = AllocationProposal(
        cash_to_deploy=180_000.0,
        buys=[Buy(symbol="FWRA", amount_usd=180_000.0, sleeve="Global diversifier",
                  justification="adds non-NVDA breadth", claimed_us_weight=None)],
    )
    r1 = verify_allocation_proposal(omitted, _packet())
    assert r1.status == GateStatus.REVISION_REQUIRED
    assert any(f.code == "missing_us_weight" for f in r1.failures)

    false_claim = omitted.model_copy(update={
        "buys": [Buy(symbol="FWRA", amount_usd=80_000.0, sleeve="Global diversifier",
                     justification="adds non-NVDA breadth", claimed_us_weight=0.0)],
    })
    r2 = verify_allocation_proposal(false_claim, _packet())
    assert r2.status == GateStatus.REVISION_REQUIRED
    assert any(f.code == "lookthrough_claim" for f in r2.failures)


def test_sell_proceeds_credited_to_conservation():
    """A deconcentration sell adds to the funds allocated: deploy+reserve must equal
    deployable + proceeds. Redeploying the proceeds balances; ignoring them fails."""
    # deployable 180k + sell 50k = 230k available; deploy all 230k.
    ok = AllocationProposal(
        cash_to_deploy=230_000.0,
        buys=[Buy(symbol="EXUS", amount_usd=230_000.0, sleeve="ex-US",
                  claimed_us_weight=0.0)],
        sells=[Sell(symbol="NVDA", amount_usd=50_000.0, reason="deconcentrate")],
        rationale="trim NVDA and redeploy the proceeds plus cash into ex-US",
    )
    r_ok = verify_allocation_proposal(ok, _packet())
    assert r_ok.status == GateStatus.ACCEPT, r_ok.failures

    # Same sell but only the original 180k is placed → 50k proceeds vanish.
    leak = ok.model_copy(update={
        "cash_to_deploy": 180_000.0,
        "buys": [Buy(symbol="EXUS", amount_usd=180_000.0, sleeve="ex-US",
                     claimed_us_weight=0.0)],
    })
    r_leak = verify_allocation_proposal(leak, _packet())
    assert r_leak.status == GateStatus.REVISION_REQUIRED
    assert any(f.code == "conservation" for f in r_leak.failures)


def test_blank_rationale_on_a_real_move_is_bounced():
    """A money recommendation must carry its reasoning: an otherwise-clean proposal
    with a blank/whitespace rationale is REVISION_REQUIRED, so the loop re-authors
    until the move is explained. It never reaches ACCEPT without a rationale."""
    blank = _ok_proposal().model_copy(update={"rationale": "   "})
    r = verify_allocation_proposal(blank, _packet())
    assert r.status == GateStatus.REVISION_REQUIRED
    assert any(f.code == "missing_rationale" for f in r.failures)

    # Same proposal WITH a rationale accepts — the check is completeness, not the
    # decision (it never dictates what to buy).
    r_ok = verify_allocation_proposal(_ok_proposal(), _packet())
    assert r_ok.status == GateStatus.ACCEPT, r_ok.failures


def test_empty_known_symbols_fails_closed():
    """No known-symbol universe → every buy is unvalidatable and BLOCKED (never
    silently admitted)."""
    p = _ok_proposal()
    r = verify_allocation_proposal(p, _packet(known_symbols=set()))
    assert r.status == GateStatus.BLOCK
    assert any(f.code == "invented_ticker" for f in r.failures)
