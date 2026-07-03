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
        "cgt_liability_usd": 100_000.0,   # a pending NVDA sale will owe ~$100k CGT
        "holdings": {"SCHD": 264_000.0, "NVDA": 2_296_000.0},
        "known_symbols": {"FUSA", "SPMV", "EXUS", "FWRA", "CSPX", "SCHD", "NVDA", "VEUR"},
    }
    p.update(over)
    return p


def _ok_proposal():
    # Deploys most, reserves for the CGT bill, buys a TRUE ex-US fund (EXUS us≈0).
    return AllocationProposal(
        cash_to_deploy=80_000.0, cash_to_reserve=0.0, cash_reserved_for_tax=100_000.0,
        buys=[Buy(symbol="EXUS", amount_usd=50_000.0, sleeve="International developed (ex-US)",
                  justification="true ex-US diversification", claimed_us_weight=0.0),
              Buy(symbol="SPMV", amount_usd=30_000.0, sleeve="US low-volatility",
                  justification="uncovered low-vol factor", claimed_us_weight=1.0)],
        sells=[], holds=[], rationale="diversify ex-US; reserve for CGT",
    )


def test_clean_proposal_accepts():
    r = verify_allocation_proposal(_ok_proposal(), _packet())
    assert r.status == GateStatus.ACCEPT, r.failures


def test_fwra_treated_as_exus_is_bounced():
    """The exact failure: buying FWRA and calling it ex-US, when the registry knows
    FWRA is ~62% US. Must be REVISION_REQUIRED, not accepted."""
    p = AllocationProposal(
        cash_to_deploy=80_000.0, cash_to_reserve=0.0, cash_reserved_for_tax=100_000.0,
        buys=[Buy(symbol="FWRA", amount_usd=80_000.0,
                  sleeve="International developed (ex-US)",
                  justification="ex-US diversification", claimed_us_weight=0.0)],
        sells=[], holds=[], rationale="x",
    )
    r = verify_allocation_proposal(p, _packet())
    assert r.status == GateStatus.REVISION_REQUIRED
    assert any("FWRA" in f.detail and "US" in f.detail for f in r.failures)


def test_missing_tax_reserve_is_bounced():
    p = _ok_proposal()
    p = p.model_copy(update={"cash_reserved_for_tax": 0.0, "cash_to_deploy": 180_000.0})
    r = verify_allocation_proposal(p, _packet())
    assert r.status == GateStatus.REVISION_REQUIRED
    assert any("tax" in f.detail.lower() or "cgt" in f.detail.lower() for f in r.failures)


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
