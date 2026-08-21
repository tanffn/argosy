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
    "SLEEVE MANDATE - x10 ASYMMETRY (binding). This is the moonshot sleeve: its "
    "job is names that can plausibly 10x, NOT safe growth compounders that might "
    "2x if they are lucky.\n"
    "  (a) CAP-MATH TEST: a candidate must plausibly 10x in 5-10 years given its "
    "CURRENT market cap vs its addressable outcome. This mechanically favors "
    "sub-~$20-30B, earlier-stage names; a >$50B company needs an EXTRAORDINARY "
    "written justification - a $70-120B company that can 'maybe 2x' is the "
    "OPPOSITE of this sleeve's job.\n"
    "  (b) ACCEPTED PER-NAME LOSS = 100% is a SIZING rule, not a ranking rule. "
    "Every name is sized so a total loss is survivable. Generic 'quality' or "
    "'defensibility' (wide moat, strong brand) is a COMPOUNDER trait and remains "
    "correctly irrelevant here.\n"
    "  (c) DO NOT USE THE WORD 'FLOOR'. Rewritten 2026-08-21 after adversarial "
    "review: the previous mandate told you to find a downside FLOOR, and agents "
    "duly reported one - by relabelling ordinary operating viability as downside "
    "protection. Positive gross profit, 'real revenue' and even positive "
    "operating cash flow are VIABILITY EVIDENCE, not floors: sales do not belong "
    "to shareholders, and cash flow can vanish, be working-capital timing, or "
    "coexist with heavy capex and stock comp. Even price/tangible-book below 1 "
    "is not mechanically protective - book is not liquidation value; inventory, "
    "receivables and PP&E need haircuts; debt, leases, pensions, environmental "
    "and preferred claims rank ahead of you; and absent a catalyst nothing forces "
    "price toward book. Classify each name into EXACTLY ONE of:\n"
    "      ASSET_BACKED - conservatively realizable assets exceed ALL claims plus "
    "expected cash burn, ideally with a catalyst (liquidation, sale, buyback, "
    "activist). Evidence = haircutted liquidation value / FULLY DILUTED market "
    "cap. The real Graham test is nearer market cap below two-thirds of a "
    "haircutted NCAV, NOT merely P/TB < 1.\n"
    "      EARNING_POWER - normalized earnings or free cash flow justify the price "
    "through a full cycle. Evidence = EV/normalized EBIT, FCF yield after capex, "
    "replacement economics. This is where a cyclical trough belongs.\n"
    "      FUNDED_OPTIONALITY - enough capital to reach a DEFINED inflection "
    "without refinancing, plus genuinely nonlinear upside. Evidence = runway in "
    "months vs the milestone date, maturities, dilution-adjusted scenario value. "
    "Net cash as a % of market cap is a CUSHION STATISTIC and belongs here, never "
    "in ASSET_BACKED - management spends the cash, and markets routinely assign "
    "negative enterprise value when expected destruction exceeds it.\n"
    "  (c2) WRITE THE EVIDENCE DOWN in downside_math, naming the class and the "
    "numbers behind it, with a source date. An UNCLASSIFIED name is scored as "
    "FUNDED_OPTIONALITY (the smallest cut) - you cannot earn a larger allocation "
    "by leaving the field vague.\n"
    "  (c3) CALIBRATION - SanDisk (SNDK) on 2025-08-21, corrected twice and now "
    "classified correctly. $45.50, market cap $6.66B on FY25 revenue $7.36B (P/S "
    "0.91x); twelve months later $1,600.62, +3,418%. It was NOT ASSET_BACKED: "
    "$4.999B of its $9.216B equity was GOODWILL, so tangible book was $4.217B and "
    "it traded at 1.58x TANGIBLE book, with NCAV of only $1.317B. It was NOT "
    "'losing money': FY25 gross profit +$2.212B, operating income +$0.507B, "
    "operating cash flow +$0.084B - the GAAP loss was a NONCASH goodwill "
    "impairment. SNDK was EARNING_POWER: a depressed cyclical/spinoff valuation "
    "on real cash-generating operations, plus a demand catalyst. Do not cite it "
    "as evidence that assets protected anyone.\n"
    "  (c4) SIZING BY CLASS (Ariel, 2026-08-21: a growth story is fine, but a "
    "smaller cut because it is riskier). FUNDED_OPTIONALITY names take the "
    "SMALLEST cut: no single one above HALF the weight of the largest "
    "ASSET_BACKED or EARNING_POWER name, and together they must not exceed ONE "
    "THIRD of the sleeve.\n"
    "  (c5) BASE RATES - state these honestly rather than implying a promise the "
    "evidence does not support. Extreme winners are barely predictable: across "
    "12,238 firm-decades and 22 characteristics, models explain ~0.8% of the "
    "variation in top-decile decade returns (Bessembinder), and market-to-book "
    "does not reliably predict them. Bounded downside is a MYTH for this asset "
    "class: the 100 greatest wealth creators still averaged a 32.5% drawdown "
    "DURING their winning decade, and 51.6% in the decade before. What IS "
    "predictable is the LEFT tail - leverage, weak profitability, high volatility "
    "and distress reliably predict losers. So this sleeve's real edge is "
    "REJECTING likely losers and diversifying across survivable optionality, NOT "
    "picking the 10x. Never tell the owner a name cannot fall 90%.\n"
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
