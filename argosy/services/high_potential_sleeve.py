"""High-potential ("satellite") sleeve — the med-high-risk slice the user asked
to carve out of a cash deployment (≥5% of the redeployed cash).

Design (user decisions, 2026-06-11 — see project_s18_reinvest_ucits_sleeve):
  * **Blend vehicle:** a UCITS thematic/growth CORE (Irish-domiciled, NOT
    US-situs — keeps the sleeve off the estate-tax base) plus a smaller
    single-name CARVE-OUT (true convexity; these single names ARE US-situs and
    the user consciously accepts the estate-tax hit on that small slice).
  * **Blend names:** seeded with the household's existing convictions + a few
    new ideas; the agent fleet validates/augments + final-sizes (the seed list
    here is the advisor's first pass, clearly fleet-refinable — NOT a frozen
    recommendation).
  * **Sizing is DERIVED, not magic:** each candidate's dollar size is its
    conviction weight (HIGH=3 / MEDIUM=2 / LOW=1) renormalised across the sleeve
    budget, within the vehicle split. No hand-picked dollar figures.

This module owns the deterministic sizing + the seed candidate set. The verdict
on WHICH names + their conviction is the fleet's job once a live synth runs; the
seeds carry ``source='advisor_seed'`` so a consumer can tell seed from
fleet-validated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Conviction = Literal["HIGH", "MEDIUM", "LOW"]
Vehicle = Literal["ucits_thematic", "single_name"]

# --- Binding sleeve mandate (Ariel, 2026-07-06) -------------------------------
# The permanent ~5% high-growth / moonshot sleeve's criterion is x10 ASYMMETRY,
# NOT conviction-safety. Ariel: "it should be the x10 sleeve, not maybe x2 if
# they are lucky (higher risk)." Every surface that grades, ranks, sizes, or
# fills this sleeve embeds this text — the first live tranche went to $70-120B
# maybe-2x compounders (NU/MELI/CRWD) precisely because the fill order was
# conviction-safety-descending; that is the failure mode this mandate bans.
X10_SLEEVE_MANDATE = (
    "SLEEVE MANDATE — x10 ASYMMETRY (binding). This is the moonshot sleeve: its "
    "job is names that can plausibly 10x, NOT safe growth compounders that might "
    "2x if they are lucky.\n"
    "  (a) CAP-MATH TEST: a candidate must plausibly 10x in 5-10 years given its "
    "CURRENT market cap vs its addressable outcome (what is the company worth if "
    "the thesis works, and is 10x that cap a believable end-state?). This "
    "mechanically favors sub-~$20-30B, earlier-stage names; a >$50B company "
    "needs an EXTRAORDINARY written justification — a $70-120B company that can "
    "'maybe 2x' is the OPPOSITE of this sleeve's job (it belongs in the "
    "core/growth sleeves, not here).\n"
    "  (b) ACCEPTED PER-NAME LOSS = 100% is a SIZING rule, not a ranking "
    "rule. Every name is sized so a total loss is survivable. Do NOT conflate "
    "two different things: 'quality/defensibility' (wide moat, strong brand, "
    "steady margins) is a COMPOUNDER trait and remains correctly irrelevant "
    "here; a DOWNSIDE FLOOR (assets, net cash, or real revenue that "
    "mechanically limits how far the price can fall) is NOT the same thing, "
    "and it MUST boost rank — a floor is precisely what makes an asymmetry "
    "asymmetric.\n"
    "  (c) RANK = (plausible upside multiple x plausibility) / plausible "
    "DOWNSIDE. Asymmetry is a RATIO and BOTH terms are mandatory. A name "
    "ranked on upside alone is ranked on VARIANCE, and variance is symmetric "
    "— it delivers -80% exactly as readily as +800%. At equal upside x "
    "plausibility, a floored name MUST outrank an unfloored one.\n"
    "  (c2) WRITE THE FLOOR DOWN. For every name, state what mechanically "
    "stops the price: price/book, net cash vs market cap, or revenue at a "
    "defensible multiple. 'No floor' is a permitted answer but MUST be "
    "declared as such — an undeclared floor is scored as no floor.\n"
    "  (c3) CALIBRATION - the archetype this sleeve exists to catch, stated "
    "ACCURATELY (corrected 2026-08-21 after an adversarial review found the "
    "first version materially wrong). SanDisk (SNDK) on 2025-08-21: $45.50, "
    "market cap $6.66B on FY25 revenue of $7.36B - P/S 0.91x, i.e. a real "
    "operating business priced at less than one year of sales. Twelve months "
    "later: $1,600.62, +3,418%. WHAT THE FLOOR WAS NOT: do NOT cite book value. "
    "Reported equity was $9.216B but $4.999B of that was GOODWILL, so tangible "
    "book was $4.217B and the stock traded at 1.58x TANGIBLE book, NOT below "
    "it; net current asset value was only $1.317B against a $6.66B cap. Nor "
    "was it \"losing money\": FY25 gross profit was +$2.212B, operating income "
    "+$0.507B and operating cash flow +$0.084B - the headline GAAP loss was "
    "dominated by a NONCASH goodwill impairment. WHAT IT ACTUALLY WAS: a "
    "depressed cyclical/spinoff VALUATION on real, cash-generating operations, "
    "plus an identifiable demand catalyst (AI memory/storage). That is the "
    "pattern to hunt - cash-generating operations the market has priced for "
    "stagnation - NOT liquidation-value protection. CONTRAST: over that same "
    "window the pre-revenue archetype delivered its downside and none of its "
    "upside (RXRX -30%, ACHR -35%, RGTI +13%; drawdowns 58-77%). "
    "SURVIVOR-BIAS WARNING: SNDK is ONE winner. Cheap cyclicals that never "
    "inflect, that dilute, or that delist are the base rate and are invisible "
    "in this single case. Treat (c3) as an illustration of the SHAPE of the "
    "trade, NEVER as evidence that cheapness alone predicts a 10x.\n"
    "  (c4) BOTH ARCHETYPES ARE ELIGIBLE — SIZED DIFFERENTLY (Ariel, 2026-08-21). "
    "An unfloored pure GROWTH STORY (pre-revenue, narrative-led) is NOT banned "
    "from this sleeve; it is simply riskier, so it takes a SMALLER cut. Binding "
    "sizing rule: no single unfloored name may exceed HALF the weight of the "
    "largest floored name, and unfloored names together must not exceed ONE "
    "THIRD of the sleeve. Rank still runs on (c): a floored name and an "
    "unfloored name of equal upside are not equals, and the weights must show "
    "it. Label each name FLOORED or UNFLOORED in its downside_math so the "
    "sizing is auditable.\n"
    "  (d) DEPLOY FILL ORDER = asymmetry-first: tranches fill the "
    "highest-asymmetry names first (the sleeve's stored instrument weights ARE "
    "the asymmetry rank), never safety-first."
)

# Weight per grade. NOTE (x10 mandate): within THIS sleeve a grade is an
# ASYMMETRY grade — HIGH means the strongest (upside x plausibility)/DOWNSIDE
# under the cap-math test above, NOT the safest / most defensible name. The
# graders (quick_estimator / discovery_grader) are prompted with the mandate so
# the existing weight math below ranks and fills asymmetry-first by meaning.
_CONVICTION_WEIGHT: dict[str, float] = {"HIGH": 3.0, "MEDIUM": 2.0, "LOW": 1.0}


@dataclass(frozen=True)
class SleeveCandidate:
    ticker: str
    name: str
    vehicle: Vehicle
    conviction: Conviction
    thesis: str
    us_situs: bool  # True => single US name/ETF, adds estate-tax exposure
    held_today: bool = False
    source: str = "advisor_seed"  # advisor_seed | fleet_validated


@dataclass(frozen=True)
class SleeveAllocation:
    candidate: SleeveCandidate
    amount_usd: float
    pct_of_sleeve: float


# --- The advisor's first-pass candidate set (fleet-refinable). ----------------
# Split ~60% UCITS thematic core / ~40% single-name carve-out by construction of
# the conviction weights below; the exact split falls out of the sizing math.
_SEED_CANDIDATES: tuple[SleeveCandidate, ...] = (
    # ---- UCITS thematic core (non-US-situs) ----
    SleeveCandidate(
        ticker="SMGB", name="VanEck Semiconductor UCITS",
        vehicle="ucits_thematic", conviction="HIGH", us_situs=False,
        thesis=(
            "Diversified exposure to the AI/semiconductor secular build-out across "
            "the whole chip complex (designers, foundries, equipment) instead of a "
            "single-name NVDA bet — captures the theme while spreading idiosyncratic "
            "risk. Irish UCITS, so it does NOT add to the US estate-tax base."
        ),
    ),
    SleeveCandidate(
        ticker="WTAI", name="WisdomTree Artificial Intelligence UCITS",
        vehicle="ucits_thematic", conviction="HIGH", us_situs=False,
        thesis=(
            "Broad AI value-chain basket (compute, software, applications) for "
            "upside beyond semis. Higher dispersion than a Nasdaq tracker; UCITS "
            "domicile keeps it non-US-situs."
        ),
    ),
    # ---- Single-name carve-out (US-situs — accepted estate-tax on this slice) ----
    SleeveCandidate(
        ticker="AMD", name="Advanced Micro Devices", held_today=True,
        vehicle="single_name", conviction="MEDIUM", us_situs=True,
        thesis=(
            "The #2 AI-accelerator with the MI300/MI400 ramp and a credible path to "
            "inference share against a richly-priced NVDA; cheaper relative to its "
            "growth. Real convexity if it takes even a modest slice of the AI-compute "
            "TAM. Risk: out-executes NVDA's CUDA moat — unproven at scale."
        ),
    ),
    SleeveCandidate(
        ticker="SOFI", name="SoFi Technologies", held_today=True,
        vehicle="single_name", conviction="MEDIUM", us_situs=True,
        thesis=(
            "Digital-bank member growth + a profitability inflection (GAAP-positive, "
            "fee-income mix shift, bank-charter funding edge). High-potential fintech "
            "compounder. Risk: consumer-credit cycle + rate sensitivity."
        ),
    ),
    SleeveCandidate(
        ticker="TSLA", name="Tesla", held_today=True,
        vehicle="single_name", conviction="LOW", us_situs=True,
        thesis=(
            "Pure optionality on robotaxi/FSD + Optimus on top of the auto/energy "
            "base — large left-and-right tail. Sized small: rich valuation, high "
            "volatility, execution + key-person risk. A lottery-leg, not a core bet."
        ),
    ),
)


def ucits_thematic_seeds() -> tuple[SleeveCandidate, ...]:
    """The non-US-situs UCITS thematic CORE seeds — kept as the sleeve core
    even when the single-name carve-out is sourced live from the trend radar."""
    return tuple(c for c in _SEED_CANDIDATES if c.vehicle == "ucits_thematic")


def build_high_potential_sleeve(
    sleeve_budget_usd: float,
    candidates: tuple[SleeveCandidate, ...] | None = None,
) -> list[SleeveAllocation]:
    """Conviction-weighted sizing of the sleeve across ``candidates``.

    Each candidate gets ``conviction_weight / Σ conviction_weight × budget``.
    Deterministic; returns ``[]`` for a non-positive budget or empty candidates.
    Sorted by amount descending.
    """
    cands = candidates if candidates is not None else _SEED_CANDIDATES
    if sleeve_budget_usd <= 0 or not cands:
        return []
    total_weight = sum(_CONVICTION_WEIGHT[c.conviction] for c in cands)
    out: list[SleeveAllocation] = []
    for c in cands:
        w = _CONVICTION_WEIGHT[c.conviction] / total_weight
        out.append(SleeveAllocation(
            candidate=c,
            amount_usd=round(w * sleeve_budget_usd, 2),
            pct_of_sleeve=round(w * 100.0, 2),
        ))
    out.sort(key=lambda a: -a.amount_usd)
    return out


def sleeve_vehicle_split(allocs: list[SleeveAllocation]) -> dict[str, float]:
    """% of the sleeve in each vehicle (ucits_thematic vs single_name)."""
    out: dict[str, float] = {}
    for a in allocs:
        out[a.candidate.vehicle] = out.get(a.candidate.vehicle, 0.0) + a.pct_of_sleeve
    return {k: round(v, 2) for k, v in out.items()}


__all__ = [
    "X10_SLEEVE_MANDATE",
    "SleeveCandidate",
    "SleeveAllocation",
    "build_high_potential_sleeve",
    "sleeve_vehicle_split",
    "ucits_thematic_seeds",
]
