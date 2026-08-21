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


# --- Moonshot-sleeve US-situs carve-out (Ariel, 2026-08-21) --------------------
# domain_knowledge/tax/us/estate_tax_nonresidents.md, "Sleeve carve-out for the
# x10 moonshot sleeve". Sleeve attribution is derived from the plan_menu entry
# carrying the X10 mandate — NEVER from the author's free-text Buy.sleeve field
# (observed empty/unreliable on a live run).

_MOONSHOT_SLEEVE_LABEL = "High-growth / high-potential"


def _moonshot_packet(target_pct=8.0, book_usd=4_150_000.0, tickers=("RGTI", "ACHR"),
                      deployable_usd=50_000.0, **over):
    """A packet with a moonshot plan_menu entry carrying the X10 mandate, plus a
    core US-equity entry with NO mandate (RKT lives only there)."""
    p = _packet(
        deployable_usd=deployable_usd,
        known_symbols={"EXUS", "SPMV", "FWRA", "SCHD", "NVDA", "RGTI", "ACHR", "RKT"},
        plan_menu=[
            {
                "sleeve": _MOONSHOT_SLEEVE_LABEL,
                "target_pct": target_pct,
                "tickers": list(tickers),
                "domiciles": ["US"] * len(tickers),
                "mandate": "SLEEVE MANDATE — x10 ASYMMETRY (binding).",
            },
            {
                "sleeve": "US equity (core)",
                "target_pct": 20.0,
                "tickers": ["RKT", "SCHD"],
                "domiciles": ["US", "US"],
                # no "mandate" key -> not the moonshot sleeve
            },
        ],
        nvda={"lookthrough_usd": 2_296_000.0, "book_usd": book_usd, "pct": 55.3, "cap_pct": 60.0},
    )
    p.update(over)
    return p


def _moonshot_buy(symbol="RGTI", amount_usd=50_000.0, disclosed=True):
    justification = (
        "the strongest x10-asymmetry candidate in the moonshot sleeve; it is a "
        "US-situs single name and adds to the NRA estate-tax base (up to 40% "
        "marginal above the $60K exemption) described in "
        "domain_knowledge/tax/us/estate_tax_nonresidents.md"
        if disclosed else
        "the strongest x10-asymmetry candidate in the moonshot sleeve"
    )
    return Buy(symbol=symbol, amount_usd=amount_usd, sleeve="",
               justification=justification, claimed_us_weight=1.0)


def _c4_buy(symbol, amount_usd, floored):
    label = "FLOORED" if floored else "UNFLOORED"
    return Buy(
        symbol=symbol, amount_usd=amount_usd, sleeve="",
        justification=(
            f"x10 moonshot sleeve. {label}: downside math stated. It is a US-situs "
            "single name and adds to the NRA estate-tax base (up to 40% marginal "
            "above the $60K exemption) per "
            "domain_knowledge/tax/us/estate_tax_nonresidents.md"
        ),
        claimed_us_weight=1.0,
    )


def test_moonshot_us_situs_buy_with_disclosure_accepts():
    """Composition note (2026-08-21, mandate (c4)): this was ONE unlabelled $50k
    buy, which now correctly trips (c4) -- an undeclared floor scores as NO floor,
    so a single unlabelled name makes the tranche 100% unfloored. Recomposed as a
    compliant pair (unfloored $10k of $50k = 20%, under both the one-third and the
    half-of-largest-floored ceilings) so this test keeps exercising what it was
    written for -- plan-menu sleeve attribution, the estate disclosure, and the
    derived US-situs cap -- not the composition rule. The FLOORED/UNFLOORED labels
    are FIXTURE VALUES exercising arithmetic, not claims about the real companies."""
    """US-situs RGTI, attributed to the moonshot sleeve via the plan menu (NOT via
    Buy.sleeve, which is deliberately left blank here to prove attribution doesn't
    depend on it), sized under the derived cap, with the estate disclosure -> ACCEPT."""
    packet = _moonshot_packet()
    # Derived cap: 40% x (8.0% x $4.15M) = 40% x $332,000 = $132,800. $50k buy fits.
    p = AllocationProposal(
        cash_to_deploy=50_000.0, cash_to_reserve=0.0,
        buys=[_c4_buy("ACHR", 40_000.0, True), _c4_buy("RGTI", 10_000.0, False)],
        sells=[], holds=[], rationale="fund the moonshot sleeve's asymmetry-first pick",
    )
    r = verify_allocation_proposal(p, packet)
    assert r.status == GateStatus.ACCEPT, r.failures


def test_moonshot_us_situs_buy_without_disclosure_is_revision():
    """Same buy, same sizing, but the justification never names the US-situs/
    estate consequence -> REVISION_REQUIRED (fixable by the author), not BLOCK
    and not a silent ACCEPT."""
    packet = _moonshot_packet()
    p = AllocationProposal(
        cash_to_deploy=50_000.0, cash_to_reserve=0.0,
        buys=[_moonshot_buy(amount_usd=50_000.0, disclosed=False)],
        sells=[], holds=[], rationale="fund the moonshot sleeve's asymmetry-first pick",
    )
    r = verify_allocation_proposal(p, packet)
    assert r.status == GateStatus.REVISION_REQUIRED
    assert any(f.code == "moonshot_estate_disclosure_missing" for f in r.failures)


def test_core_sleeve_us_situs_buy_still_blocked():
    """RKT is US-situs but lives only under the CORE 'US equity (core)' menu entry
    (no X10 mandate) -> the carve-out must NOT apply; still BLOCKED, same as today."""
    packet = _moonshot_packet()
    p = AllocationProposal(
        cash_to_deploy=50_000.0, cash_to_reserve=0.0,
        buys=[Buy(symbol="RKT", amount_usd=50_000.0, sleeve="US equity (core)",
                  justification="core US financials pick", claimed_us_weight=1.0)],
        sells=[], holds=[], rationale="fill the core US-equity gap",
    )
    r = verify_allocation_proposal(p, packet)
    assert r.status == GateStatus.BLOCK
    assert any(f.code == "us_situs" for f in r.failures)


def test_moonshot_sleeve_us_situs_total_over_cap_is_revision():
    """Two moonshot US-situs buys totalling $200k blow the derived $132,800 cap
    (40% x 8.0% x $4.15M) -> REVISION_REQUIRED with the cap failure, even though
    each individual buy discloses the estate consequence correctly."""
    packet = _moonshot_packet(tickers=("RGTI", "ACHR"), deployable_usd=200_000.0)
    p = AllocationProposal(
        cash_to_deploy=200_000.0, cash_to_reserve=0.0,
        buys=[_moonshot_buy(symbol="RGTI", amount_usd=120_000.0),
              _moonshot_buy(symbol="ACHR", amount_usd=80_000.0)],
        sells=[], holds=[], rationale="load up the moonshot sleeve's top two picks",
    )
    r = verify_allocation_proposal(p, packet)
    assert r.status == GateStatus.REVISION_REQUIRED
    assert any(f.code == "moonshot_us_situs_cap" for f in r.failures)


def test_ambiguous_sleeve_attribution_fails_closed_as_core():
    """RGTI is US-situs but the packet has NO plan_menu at all (sleeve attribution
    cannot be established) -> must be treated as CORE and BLOCKED, never silently
    treated as moonshot just because the symbol happens to be one the sleeve could
    plausibly hold."""
    packet = _packet(known_symbols={"RGTI"})  # no plan_menu, no nvda/book info
    p = AllocationProposal(
        cash_to_deploy=50_000.0, cash_to_reserve=0.0,
        buys=[_moonshot_buy(amount_usd=50_000.0)],
        sells=[], holds=[], rationale="fund the moonshot sleeve's asymmetry-first pick",
    )
    r = verify_allocation_proposal(p, packet)
    assert r.status == GateStatus.BLOCK
    assert any(f.code == "us_situs" for f in r.failures)


def test_nvda_still_sanctioned_inside_moonshot_packet():
    """NVDA remains sanctioned regardless of the moonshot machinery — proves the
    carve-out didn't change NVDA's existing exemption."""
    packet = _moonshot_packet()
    p = AllocationProposal(
        cash_to_deploy=50_000.0, cash_to_reserve=0.0,
        buys=[Buy(symbol="NVDA", amount_usd=50_000.0, sleeve="",
                  justification="add to the sanctioned NVDA sleeve", claimed_us_weight=1.0)],
        sells=[], holds=[], rationale="top up NVDA",
    )
    r = verify_allocation_proposal(p, packet)
    assert not any(f.code == "us_situs" for f in r.failures)
    assert not any(f.code.startswith("moonshot_") for f in r.failures)


# --- Mandate (c4): unfloored growth stories take a SMALLER cut ------------------
# Ariel's 2026-08-21 ruling. These pin the ARITHMETIC only -- whether a FLOORED
# label is TRUE is a judgement call for the blind re-deriver, never for this gate.
# The live 2026-08-21 run (RXRX/TEM floored, RGTI/OKLO unfloored) is the ACCEPT
# case, so a regression that breaks a compliant real plan is caught here.

_C4_TICKERS = ("RXRX", "TEM", "RGTI", "OKLO")


def _c4_packet(**over):
    return _moonshot_packet(tickers=_C4_TICKERS, deployable_usd=100_000.0, **over)


def _c4_codes(buys):
    total = round(sum(b.amount_usd for b in buys), 2)
    p = AllocationProposal(
        cash_to_deploy=total, cash_to_reserve=0.0, buys=buys,
        sells=[], holds=[], rationale="fund the moonshot sleeve",
    )
    res = verify_allocation_proposal(p, _c4_packet())
    return {f.code for f in res.failures}


def test_c4_real_2026_08_21_sleeve_passes():
    """The sleeve the live run actually produced: unfloored $3,300 of $10,000
    (under the $3,333 one-third cap) and each unfloored under half of RXRX's
    $3,500. Compliant -> neither (c4) code fires."""
    codes = _c4_codes([
        _c4_buy("RXRX", 3_500.0, True), _c4_buy("TEM", 3_200.0, True),
        _c4_buy("RGTI", 1_700.0, False), _c4_buy("OKLO", 1_600.0, False),
    ])
    assert "moonshot_c4_unfloored_share" not in codes
    assert "moonshot_c4_unfloored_name_size" not in codes


def test_c4_unfloored_share_over_one_third_is_flagged():
    """Push the unfloored pair to $4,000 of $10,700 (37%) -> over the one-third
    ceiling. This is the drift the prose could not prevent."""
    codes = _c4_codes([
        _c4_buy("RXRX", 3_500.0, True), _c4_buy("TEM", 3_200.0, True),
        _c4_buy("RGTI", 2_000.0, False), _c4_buy("OKLO", 2_000.0, False),
    ])
    assert "moonshot_c4_unfloored_share" in codes


def test_c4_single_unfloored_over_half_of_largest_floored_is_flagged():
    """RGTI at $1,900 exceeds half of RXRX's $3,500 (=$1,750), even though the
    combined unfloored share stays under one third."""
    codes = _c4_codes([
        _c4_buy("RXRX", 3_500.0, True), _c4_buy("TEM", 3_200.0, True),
        _c4_buy("RGTI", 1_900.0, False), _c4_buy("OKLO", 500.0, False),
    ])
    assert "moonshot_c4_unfloored_name_size" in codes


def test_c4_unlabelled_name_counts_as_unfloored():
    """Mandate (c2): an undeclared floor is scored as NO floor. Here EVERY name is
    unlabelled, so the whole sleeve reads unfloored and blows the one-third cap --
    the author cannot dodge (c4) by simply omitting the label."""
    plain = [
        Buy(symbol=s, amount_usd=a, sleeve="",
            justification=("x10 moonshot sleeve pick. US-situs single name adding to "
                           "the NRA estate-tax base (40% marginal above the $60K "
                           "exemption) per "
                           "domain_knowledge/tax/us/estate_tax_nonresidents.md"),
            claimed_us_weight=1.0)
        for s, a in (("RXRX", 3_500.0), ("TEM", 3_200.0), ("RGTI", 1_700.0), ("OKLO", 1_600.0))
    ]
    assert "moonshot_c4_unfloored_share" in _c4_codes(plain)
