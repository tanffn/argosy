"""The deployment VERIFIER — determinism gates the fleet's authored allocation
(ACCEPT / REVISION_REQUIRED / BLOCK). It checks facts; it never re-decides ("this
violates the facts", never "therefore buy X"). This is the spine of the fleet-
authors / determinism-verifies inversion.

The acceptance test IS the failure that motivated the pivot: a proposal that treats
FWRA (~62% US) as ex-US diversification, or that skips the known NVDA-sale CGT
reserve, must be bounced for revision — not silently accepted.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from argosy.services.allocation_author.proposal import (
    AllocationProposal,
    AuthoredOrderIntent,
    Buy,
    Sell,
)
from argosy.services.allocation_author.verifier import GateStatus, verify_allocation_proposal
from argosy.services.order_sheet import CandidateComparison, OutcomeScenario


def _packet(**over):
    p = {
        "deployable_usd": 180_000.0,
        "holdings": {"SCHD": 264_000.0, "NVDA": 2_296_000.0},
        "known_symbols": {"FUSA", "SPMV", "EXUS", "FWRA", "CSPX", "SCHD", "NVDA", "VEUR"},
        "sale_tax_inputs": {
            "NVDA": {
                "current_price_usd": 100,
                "lots": [
                    {
                        "lot_id": "nvda-1",
                        "quantity": 1000,
                        "cost_basis_usd": 50_000,
                    }
                ],
                "policy": {
                    "effective_tax_rate": 0.25,
                    "method": "verified Israeli CGT",
                    "authoritative": True,
                    "source": "verified lot/FX tax layer",
                },
                "friction_usd": 0,
                "as_of": "2026-08-25T12:00:00Z",
            }
        },
    }
    p.update(over)
    return p


def _intent(thesis_type="diversifier", upside=2) -> AuthoredOrderIntent:
    kwargs = {}
    if str(thesis_type).lower().endswith("convexity"):
        kwargs = {
            "outcome_scenarios": [
                OutcomeScenario(label="wipeout", probability_pct=30, terminal_multiple=0.1, terminal_date=date(2030, 8, 26), rationale="Thesis fails."),
                OutcomeScenario(label="base", probability_pct=40, terminal_multiple=1, terminal_date=date(2030, 8, 26), rationale="Mixed outcome."),
                OutcomeScenario(label="bull", probability_pct=20, terminal_multiple=3, terminal_date=date(2030, 8, 26), rationale="One success."),
                OutcomeScenario(label="moonshot", probability_pct=10, terminal_multiple=10, terminal_date=date(2030, 8, 26), rationale="Multiple successes."),
            ],
            "probability_confidence": "LOW",
            "probability_basis": "Clinical base rates and current evidence.",
        }
    return AuthoredOrderIntent(
        thesis="Fill the governing sleeve gap.",
        thesis_type=thesis_type,
        falsifier="The instrument no longer supplies the intended exposure.",
        catalyst_description="Next scheduled portfolio review",
        catalyst_date="2027-01-31",
        expectation="The allocation gap closes.",
        expectation_due_date="2027-02-28",
        success_measure="sleeve moves toward target without a risk breach",
        expected_upside_multiple=upside,
        **kwargs,
    )


def test_convexity_intent_without_probabilities_requires_revision():
    intent = _intent("convexity", 10).model_copy(
        update={"outcome_scenarios": [], "probability_confidence": None, "probability_basis": None}
    )
    proposal = AllocationProposal(
        cash_to_deploy=50_000,
        cash_to_reserve=130_000,
        buys=[Buy(symbol="GLUE", amount_usd=50_000, claimed_us_weight=1.0, order_intent=intent)],
        sells=[],
        rationale="Probability model omitted.",
    )
    report = verify_allocation_proposal(proposal, _packet(known_symbols={"GLUE"}))
    codes = {f.code for f in report.failures}
    assert "convexity_probabilities_missing" in codes
    assert "probability_basis_missing" in codes


def _ok_proposal():
    # Deploys the full net-of-tax amount into a TRUE ex-US fund (EXUS us≈0) + a
    # low-vol sleeve. No tax reserve — CGT is paid from the sale that realizes it.
    return AllocationProposal(
        cash_to_deploy=180_000.0,
        cash_to_reserve=0.0,
        buys=[
            Buy(
                symbol="EXUS",
                amount_usd=130_000.0,
                sleeve="International developed (ex-US)",
                justification="true ex-US diversification",
                claimed_us_weight=0.0,
                order_intent=_intent(),
            ),
            Buy(
                symbol="SPMV",
                amount_usd=50_000.0,
                sleeve="US low-volatility",
                justification="uncovered low-vol factor",
                claimed_us_weight=1.0,
                order_intent=_intent(),
            ),
        ],
        sells=[],
        holds=[],
        rationale="diversify ex-US",
    )


def test_clean_proposal_accepts():
    r = verify_allocation_proposal(_ok_proposal(), _packet())
    assert r.status == GateStatus.ACCEPT, r.failures


def _discovery_row(ticker: str, rank: int, score: float) -> dict:
    return {
        "ticker": ticker,
        "rank": rank,
        "score": score,
        "fresh_as_of": "2026-08-26T06:00:00+00:00",
        "estimator": {
            "go": True,
            "conviction": "MED",
            "sentiment": 0.5,
        },
        "fleet": {"verdict": "BUY", "conviction": "MED", "thesis_md": f"{ticker} thesis"},
    }


def _candidate_comparison(ticker: str, rank: int, score: float, selected: bool):
    return CandidateComparison(
        ticker=ticker,
        selection="SELECTED" if selected else "NOT_SELECTED",
        radar_rank=rank,
        radar_score=score,
        research_verdict="BUY",
        research_conviction="MED",
        evidence_fresh_as_of=datetime(2026, 8, 26, 6, tzinfo=UTC),
        key_advantage=f"{ticker} advantage",
        key_risk=f"{ticker} risk",
        why=f"{ticker} {'won' if selected else 'lost'} the comparative judgment.",
        outcome_scenarios=[
            OutcomeScenario(label="wipeout", probability_pct=30, terminal_multiple=0.1, terminal_date=date(2030, 8, 26), rationale="Thesis fails."),
            OutcomeScenario(label="base", probability_pct=50, terminal_multiple=1, terminal_date=date(2030, 8, 26), rationale="Mixed outcome."),
            OutcomeScenario(label="upside", probability_pct=20, terminal_multiple=5, terminal_date=date(2030, 8, 26), rationale="Thesis succeeds."),
        ],
        probability_confidence="LOW",
        probability_basis="Equal-basis industry rates and current evidence.",
        recommended_position_usd=130_000 if selected else 0,
        smaller_position_usd=65_000 if selected else None,
        why_not_smaller="The upside would not materially affect the portfolio." if selected else None,
        larger_position_usd=200_000 if selected else 20_000,
        why_not_larger="The evidence does not warrant that much capital.",
        split_considered=True,
        split_why="Independent failure modes were compared before choosing one allocation.",
        sizing_why="Selected amount matches the order." if selected else "No independently warranted slot this run.",
    )


def test_discovery_buy_requires_explicit_comparison_with_every_buy_finalist():
    packet = _packet(
        discovery_candidates=[
            _discovery_row("EXUS", 26, 76.9),
            _discovery_row("REPL", 16, 77.5),
            _discovery_row("QURE", 17, 77.5),
        ]
    )
    report = verify_allocation_proposal(_ok_proposal(), packet)
    assert "candidate_comparison_missing" in {f.code for f in report.failures}


def test_discovery_buy_blocks_when_higher_ranked_name_is_unevaluated():
    selected = _discovery_row("EXUS", 15, 80.1)
    unevaluated = {
        "ticker": "ONDS",
        "rank": 1,
        "score": 108.2,
        "fresh_as_of": "2026-08-29T07:29:58+00:00",
        "estimator": None,
        "fleet": None,
    }
    packet = _packet(discovery_candidates=[unevaluated, selected])
    proposal = _ok_proposal().model_copy(
        update={
            "candidate_comparisons": [
                _candidate_comparison("EXUS", 15, 80.1, True)
            ]
        }
    )

    report = verify_allocation_proposal(proposal, packet)

    assert report.status == GateStatus.BLOCK
    failure = next(
        f for f in report.failures if f.code == "discovery_evaluation_incomplete"
    )
    assert "ONDS" in failure.detail


def test_discovery_comparison_records_winner_losers_and_raw_grades():
    packet = _packet(
        discovery_candidates=[
            _discovery_row("EXUS", 26, 76.9),
            _discovery_row("REPL", 16, 77.5),
            _discovery_row("QURE", 17, 77.5),
        ]
    )
    proposal = _ok_proposal().model_copy(update={
        "candidate_comparisons": [
            _candidate_comparison("EXUS", 26, 76.9, True),
            _candidate_comparison("REPL", 16, 77.5, False),
            _candidate_comparison("QURE", 17, 77.5, False),
        ]
    })
    report = verify_allocation_proposal(proposal, packet)
    comparison_failures = {
        f.code for f in report.failures if f.code.startswith("candidate_")
    }
    assert comparison_failures == set(), report.failures


@pytest.mark.parametrize("grade", ["WATCH", "PASS"])
def test_resolving_research_checks_non_buy_source_grade(grade):
    now = datetime.now(UTC)
    row = _discovery_row("REPL", 16, 77.5)
    row["fleet"]["verdict"] = grade
    row["fresh_as_of"] = now.isoformat()
    comparison = _candidate_comparison("REPL", 16, 77.5, False)
    comparison.research_verdict = grade
    comparison.evidence_fresh_as_of = now
    packet = _packet(discovery_candidates=[row], allocation_research_tasks=[{"tickers": ["REPL"]}])
    proposal = _ok_proposal().model_copy(update={"candidate_comparisons": [comparison]})
    assert verify_allocation_proposal(proposal, packet).status == GateStatus.ACCEPT
    comparison.research_verdict = "BUY"
    assert "candidate_grade_mismatch" in {f.code for f in verify_allocation_proposal(proposal, packet).failures}
    packet["discovery_candidates"] = []
    assert "research_resolution_evidence_missing" in {f.code for f in verify_allocation_proposal(proposal, packet).failures}


def test_every_discovery_finalist_requires_equal_basis_probabilities_and_sizing():
    packet = _packet(discovery_candidates=[_discovery_row("EXUS", 26, 76.9)])
    incomplete = _candidate_comparison("EXUS", 26, 76.9, True).model_copy(
        update={
            "outcome_scenarios": [],
            "probability_confidence": None,
            "probability_basis": None,
            "recommended_position_usd": 10_000,
            "sizing_why": None,
        }
    )
    proposal = _ok_proposal().model_copy(update={"candidate_comparisons": [incomplete]})
    codes = {f.code for f in verify_allocation_proposal(proposal, packet).failures}
    assert "candidate_probabilities_missing" in codes
    assert "candidate_probability_basis_missing" in codes
    assert "candidate_sizing_missing" in codes

    mismatched = _candidate_comparison("EXUS", 26, 76.9, True).model_copy(
        update={
            "recommended_position_usd": 10_000,
            "smaller_position_usd": 5_000,
            "larger_position_usd": 20_000,
        }
    )
    proposal = _ok_proposal().model_copy(update={"candidate_comparisons": [mismatched]})
    codes = {f.code for f in verify_allocation_proposal(proposal, packet).failures}
    assert "candidate_sizing_order_mismatch" in codes


def test_sub_one_percent_nonconvex_buy_is_bounced_inside_author_loop():
    proposal = AllocationProposal(
        cash_to_deploy=180_000.0,
        buys=[
            Buy(
                symbol="FUSA",
                amount_usd=10_000.0,
                claimed_us_weight=1.0,
                order_intent=_intent("income", 2),
            ),
            Buy(
                symbol="EXUS",
                amount_usd=170_000.0,
                claimed_us_weight=0.0,
                order_intent=_intent(),
            ),
        ],
        rationale="Allocate without economically immaterial fragments.",
    )
    report = verify_allocation_proposal(proposal, _packet())
    assert report.status == GateStatus.REVISION_REQUIRED
    assert "small_position_without_convexity" in {f.code for f in report.failures}
    assert "small_position_insufficient_asymmetry" in {f.code for f in report.failures}


def test_sub_one_percent_order_is_allowed_when_existing_position_exceeds_floor():
    packet = _packet(
        holdings={"FUSA": 30_000.0, "NVDA": 2_296_000.0},
        known_symbols={"FUSA", "NVDA"},
    )
    proposal = AllocationProposal(
        cash_to_deploy=180_000.0,
        buys=[
            Buy(
                symbol="FUSA",
                amount_usd=180_000.0,
                claimed_us_weight=1.0,
                order_intent=_intent("income", 2),
            )
        ],
        rationale="Top up an existing material position.",
    )
    report = verify_allocation_proposal(proposal, packet)
    assert report.status == GateStatus.ACCEPT, report.failures


def test_exus_words_in_a_negated_justification_do_not_override_numeric_fact():
    proposal = AllocationProposal(
        cash_to_deploy=180_000.0,
        buys=[
            Buy(
                symbol="CSPX",
                amount_usd=180_000.0,
                sleeve="US core equity",
                justification="fills US core rather than the ex-US sleeve",
                claimed_us_weight=1.0,
                order_intent=_intent(),
            )
        ],
        rationale="Fill the structured US-core gap.",
    )
    report = verify_allocation_proposal(proposal, _packet())
    assert report.status == GateStatus.ACCEPT, report.failures


def test_fwra_treated_as_exus_is_bounced():
    """The exact failure: buying FWRA and calling it ex-US, when the registry knows
    FWRA is ~62% US. Must be REVISION_REQUIRED, not accepted."""
    p = AllocationProposal(
        cash_to_deploy=180_000.0,
        cash_to_reserve=0.0,
        buys=[
            Buy(
                symbol="FWRA",
                amount_usd=180_000.0,
                sleeve="International developed (ex-US)",
                justification="ex-US diversification",
                claimed_us_weight=0.0,
            )
        ],
        sells=[],
        holds=[],
        rationale="x",
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
    p = _ok_proposal().model_copy(
        update={
            "sells": [Sell(symbol="SCHD", amount_usd=500_000.0, reason="migrate")],
        }
    )
    r = verify_allocation_proposal(p, _packet())
    assert r.status == GateStatus.BLOCK
    assert any("SCHD" in f.detail for f in r.failures)


def test_invented_ticker_is_blocked():
    p = _ok_proposal().model_copy(
        update={
            "buys": [
                Buy(
                    symbol="ZZZZ",
                    amount_usd=80_000.0,
                    sleeve="?",
                    justification="?",
                    claimed_us_weight=0.0,
                )
            ],
        }
    )
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
        cash_to_deploy=180_100.0,
        cash_to_reserve=-100.0,
        buys=[
            Buy.model_construct(
                symbol="EXUS",
                amount_usd=180_100.0,
                sleeve="ex-US",
                justification="",
                claimed_us_weight=0.0,
            )
        ],
        sells=[],
        holds=[],
        rationale="",
    )
    r = verify_allocation_proposal(p, _packet())
    assert r.status == GateStatus.BLOCK
    assert any(f.code == "negative_amount" for f in r.failures)


def test_missing_claimed_us_weight_is_bounced():
    """A buy with no claimed_us_weight can't be cross-checked — must be REVISION."""
    p = _ok_proposal().model_copy(
        update={
            "buys": [
                Buy(
                    symbol="EXUS",
                    amount_usd=80_000.0,
                    sleeve="ex-US",
                    justification="",
                    claimed_us_weight=None,
                )
            ],
        }
    )
    r = verify_allocation_proposal(p, _packet())
    assert r.status == GateStatus.REVISION_REQUIRED
    assert any(f.code == "missing_us_weight" for f in r.failures)


def test_fwra_evasion_via_neutral_sleeve_still_caught():
    """The evasion the reviewer flagged: buy FWRA into a 'Global diversifier' sleeve
    with no 'ex-US' words. Omitting claimed_us_weight now trips missing_us_weight;
    supplying a false 0.0 trips lookthrough_claim. Either way it can't pass ACCEPT."""
    omitted = AllocationProposal(
        cash_to_deploy=180_000.0,
        buys=[
            Buy(
                symbol="FWRA",
                amount_usd=180_000.0,
                sleeve="Global diversifier",
                justification="adds non-NVDA breadth",
                claimed_us_weight=None,
            )
        ],
    )
    r1 = verify_allocation_proposal(omitted, _packet())
    assert r1.status == GateStatus.REVISION_REQUIRED
    assert any(f.code == "missing_us_weight" for f in r1.failures)

    false_claim = omitted.model_copy(
        update={
            "buys": [
                Buy(
                    symbol="FWRA",
                    amount_usd=80_000.0,
                    sleeve="Global diversifier",
                    justification="adds non-NVDA breadth",
                    claimed_us_weight=0.0,
                )
            ],
        }
    )
    r2 = verify_allocation_proposal(false_claim, _packet())
    assert r2.status == GateStatus.REVISION_REQUIRED
    assert any(f.code == "lookthrough_claim" for f in r2.failures)


def test_sell_proceeds_credited_to_conservation():
    """A deconcentration sell adds to the funds allocated: deploy+reserve must equal
    deployable + proceeds. Redeploying the proceeds balances; ignoring them fails."""
    # $50k gross on $25k selected basis -> $6.25k tax -> $43.75k net.
    # deployable 180k + net sell 43.75k = 223.75k available.
    ok = AllocationProposal(
        cash_to_deploy=223_750.0,
        buys=[
            Buy(
                symbol="EXUS",
                amount_usd=223_750.0,
                sleeve="ex-US",
                claimed_us_weight=0.0,
                order_intent=_intent(),
            )
        ],
        sells=[
            Sell(
                symbol="NVDA",
                amount_usd=50_000.0,
                reason="deconcentrate",
                order_intent=_intent("compounder"),
            )
        ],
        rationale="trim NVDA and redeploy the proceeds plus cash into ex-US",
    )
    r_ok = verify_allocation_proposal(ok, _packet())
    assert r_ok.status == GateStatus.ACCEPT, r_ok.failures

    # Same sell but only the original 180k is placed → 50k proceeds vanish.
    leak = ok.model_copy(
        update={
            "cash_to_deploy": 180_000.0,
            "buys": [
                Buy(symbol="EXUS", amount_usd=180_000.0, sleeve="ex-US", claimed_us_weight=0.0)
            ],
        }
    )
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


def _moonshot_packet(
    target_pct=8.0, book_usd=4_150_000.0, tickers=("RGTI", "ACHR"), deployable_usd=50_000.0, **over
):
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
        if disclosed
        else "the strongest x10-asymmetry candidate in the moonshot sleeve"
    )
    return Buy(
        symbol=symbol,
        amount_usd=amount_usd,
        sleeve="",
        justification=justification,
        claimed_us_weight=1.0,
    )


def _c4_buy(symbol, amount_usd, cls):
    """cls is a mandate (c) sleeve class. True/False are accepted for the
    pre-2026-08-21 FLOORED/UNFLOORED binary and map onto EARNING_POWER /
    FUNDED_OPTIONALITY -- the old labels themselves now fail closed."""
    if cls is True:
        cls = "EARNING_POWER"
    elif cls is False:
        cls = "FUNDED_OPTIONALITY"
    label = cls
    intent = _intent("convexity", 10).model_copy(
        update={
            "downside_class": cls,
            "acknowledges_us_situs_estate_cost": True,
        }
    )
    return Buy(
        symbol=symbol,
        amount_usd=amount_usd,
        sleeve="",
        justification=(
            f"x10 moonshot sleeve. {label}: class evidence stated. It is a US-situs "
            "single name and adds to the NRA estate-tax base (up to 40% marginal "
            "above the $60K exemption) per "
            "domain_knowledge/tax/us/estate_tax_nonresidents.md"
        ),
        claimed_us_weight=1.0,
        order_intent=intent,
    )


def test_moonshot_us_situs_buy_with_disclosure_accepts():
    """US-situs RGTI, attributed to the moonshot sleeve via the plan menu (NOT via
    Buy.sleeve, which is deliberately left blank here to prove attribution doesn't
    depend on it), sized under the derived cap, with the estate disclosure -> ACCEPT."""
    packet = _moonshot_packet()
    # Derived cap: 40% x (8.0% x $4.15M) = 40% x $332,000 = $132,800. $50k buy fits.
    p = AllocationProposal(
        cash_to_deploy=50_000.0,
        cash_to_reserve=0.0,
        buys=[_c4_buy("RGTI", 50_000.0, "FUNDED_OPTIONALITY")],
        sells=[],
        holds=[],
        rationale="fund the moonshot sleeve's asymmetry-first pick",
    )
    r = verify_allocation_proposal(p, packet)
    assert r.status == GateStatus.ACCEPT, r.failures


def test_moonshot_us_situs_buy_without_disclosure_is_revision():
    """Same buy, same sizing, but the justification never names the US-situs/
    estate consequence -> REVISION_REQUIRED (fixable by the author), not BLOCK
    and not a silent ACCEPT."""
    packet = _moonshot_packet()
    p = AllocationProposal(
        cash_to_deploy=50_000.0,
        cash_to_reserve=0.0,
        buys=[_moonshot_buy(amount_usd=50_000.0, disclosed=False)],
        sells=[],
        holds=[],
        rationale="fund the moonshot sleeve's asymmetry-first pick",
    )
    r = verify_allocation_proposal(p, packet)
    assert r.status == GateStatus.REVISION_REQUIRED
    assert any(f.code == "moonshot_estate_disclosure_missing" for f in r.failures)


def test_core_sleeve_us_situs_buy_still_blocked():
    """RKT is US-situs but lives only under the CORE 'US equity (core)' menu entry
    (no X10 mandate) -> the carve-out must NOT apply; still BLOCKED, same as today."""
    packet = _moonshot_packet()
    p = AllocationProposal(
        cash_to_deploy=50_000.0,
        cash_to_reserve=0.0,
        buys=[
            Buy(
                symbol="RKT",
                amount_usd=50_000.0,
                sleeve="US equity (core)",
                justification="core US financials pick",
                claimed_us_weight=1.0,
            )
        ],
        sells=[],
        holds=[],
        rationale="fill the core US-equity gap",
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
        cash_to_deploy=200_000.0,
        cash_to_reserve=0.0,
        buys=[
            _moonshot_buy(symbol="RGTI", amount_usd=120_000.0),
            _moonshot_buy(symbol="ACHR", amount_usd=80_000.0),
        ],
        sells=[],
        holds=[],
        rationale="load up the moonshot sleeve's top two picks",
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
        cash_to_deploy=50_000.0,
        cash_to_reserve=0.0,
        buys=[_moonshot_buy(amount_usd=50_000.0)],
        sells=[],
        holds=[],
        rationale="fund the moonshot sleeve's asymmetry-first pick",
    )
    r = verify_allocation_proposal(p, packet)
    assert r.status == GateStatus.BLOCK
    assert any(f.code == "us_situs" for f in r.failures)


def test_nvda_still_sanctioned_inside_moonshot_packet():
    """NVDA remains sanctioned regardless of the moonshot machinery — proves the
    carve-out didn't change NVDA's existing exemption."""
    packet = _moonshot_packet()
    p = AllocationProposal(
        cash_to_deploy=50_000.0,
        cash_to_reserve=0.0,
        buys=[
            Buy(
                symbol="NVDA",
                amount_usd=50_000.0,
                sleeve="",
                justification="add to the sanctioned NVDA sleeve",
                claimed_us_weight=1.0,
            )
        ],
        sells=[],
        holds=[],
        rationale="top up NVDA",
    )
    r = verify_allocation_proposal(p, packet)
    assert not any(f.code == "us_situs" for f in r.failures)
    assert not any(f.code.startswith("moonshot_") for f in r.failures)


# --- Mandate (c4): composition is reviewed judgment, not a numeric gate --------

_C4_TICKERS = ("RXRX", "TEM", "RGTI", "OKLO")


def _c4_packet(**over):
    return _moonshot_packet(tickers=_C4_TICKERS, deployable_usd=100_000.0, **over)


def _c4_codes(buys):
    total = round(sum(b.amount_usd for b in buys), 2)
    p = AllocationProposal(
        cash_to_deploy=total,
        cash_to_reserve=0.0,
        buys=buys,
        sells=[],
        holds=[],
        rationale="fund the moonshot sleeve",
    )
    res = verify_allocation_proposal(p, _c4_packet())
    return {f.code for f in res.failures}


def test_c4_funded_optionality_split_is_not_rejected_by_a_fixed_ratio():
    """The team judges whether the split is sound; deterministic verification only
    enforces funding, eligibility, estate arithmetic, and declared intent."""
    codes = _c4_codes(
        [
            _c4_buy("RXRX", 20_000.0, "FUNDED_OPTIONALITY"),
            _c4_buy("TEM", 20_000.0, "FUNDED_OPTIONALITY"),
        ]
    )
    assert not any(code.startswith("moonshot_c4_") for code in codes)


def test_pending_fund_resolution_uses_fund_evidence_not_a_fake_moonshot_grade():
    now = datetime.now(UTC)
    comparison = CandidateComparison(ticker="IWQU", selection="NOT_SELECTED",
        research_verdict="HOLD", research_conviction="MED", evidence_fresh_as_of=now,
        key_advantage="Diversified quality", key_risk="Factor underperformance", why="Core already funded",
        recommended_position_usd=0)
    packet = _packet(allocation_research_tasks=[{"tickers": ["IWQU"], "research_result": {
        "as_of": now.isoformat(), "tickers": {"IWQU": {"kind": "fund_vehicle", "report_id": 123,
            "verdict": "HOLD", "conviction": "MED"}}}}])
    proposal = _ok_proposal().model_copy(update={"candidate_comparisons": [comparison]})
    report = verify_allocation_proposal(proposal, packet)
    assert report.status == GateStatus.ACCEPT, report.failures
    comparison.research_verdict = "BUY"
    report = verify_allocation_proposal(proposal, packet)
    assert any(f.code == "research_resolution_evidence_missing" for f in report.failures)
