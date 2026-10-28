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

from dataclasses import dataclass
from typing import Literal

Conviction = Literal["HIGH", "MEDIUM", "LOW"]
Vehicle = Literal["ucits_thematic", "single_name"]

# --- Binding sleeve mandate ----------------------------------------------

# Keep the public name for existing consumers, but version the actual mandate:
# the owner explicitly includes established, macro-aligned 5x businesses. The
# prior 10x-only/small-cap doctrine is not the current investment objective.
MOONSHOT_MANDATE_VERSION = "time-aware-structural-upside-v2"
X10_SLEEVE_MANDATE = (
    "SLEEVE MANDATE - TIME-AWARE STRUCTURAL UPSIDE, including moonshots. "
    "This is a bounded high-upside sleeve, not a safe slow-growth substitute for the core. "
    "There is NO fixed 5x or 10x eligibility hurdle: a credible 2x in six months can "
    "be more attractive than a speculative 5x in ten years.\n"
    "  (a) SEEK A BUSINESS MECHANISM, NOT A SMALL MARKET CAP. Investigate established "
    "companies as well as emerging businesses: technology adoption, an industry "
    "bottleneck, earnings inflection, market-share capture, restructuring or a "
    "cyclical recovery can create nonlinear equity upside. Market cap is scale "
    "evidence, not a small-cap eligibility rule. Explain PER-SHARE outcomes from "
    "today's price over explicit dates: short catalysts and multi-year transformations "
    "both qualify for investigation. State revenue, margins, cash flow, valuation "
    "and dilution assumptions. Compare returns per unit of time with realistic "
    "uncertainty and downside; do not mechanically extrapolate a six-month double "
    "into a repeatable annual return. An ambitious scenario is not a base case.\n"
    "  (b) QUALITY MATTERS WHEN IT MAKES THE UPSIDE ACHIEVABLE. Competitive advantage, "
    "execution, customer demand, funding and cash generation can increase the "
    "probability of capturing a structural change. They must NOT be dismissed as "
    "irrelevant compounder traits, nor substitute for upside at the CURRENT price. "
    "A past rally alone proves neither exhaustion nor future success.\n"
    "  (c) UNDERWRITE BOTH TAILS. Give downside/base/upside scenarios and their "
    "assumptions, compare the thesis with the benchmark alternative, and distinguish "
    "evidence from speculation. Do not invent calibrated probabilities. Recognize "
    "correlated macro, technology and financing risks across candidates and holdings. "
    "Macro alignment is conditional: identify what happens if the regime reverses.\n"
    "  (d) CLASSIFY DOWNSIDE EVIDENCE in downside_math as ASSET_BACKED, EARNING_POWER "
    "or FUNDED_OPTIONALITY. ASSET_BACKED requires conservatively realizable assets "
    "after ALL senior claims and expected burn, not book value or net cash alone. "
    "EARNING_POWER requires normalized earnings/FCF through a cycle after capex and "
    "dilution. FUNDED_OPTIONALITY requires runway through a DEFINED inflection, "
    "maturities and dilution-adjusted scenarios. Unclassified evidence is not stronger "
    "than funded optionality. Revenue, gross profit and operating viability are NOT "
    "a downside FLOOR. Every stock can suffer a severe or total loss.\n"
    "  (e) SIZING IS PORTFOLIO JUDGMENT. A 100% loss must remain survivable. Compare "
    "zero, one or several names, failure-mode independence and whole-portfolio outcome "
    "contribution. Do not require very-high conviction for every small position; "
    "uncertainty can justify a smaller researched allocation. Below roughly 1% of "
    "book, test whether the plausible upside meaningfully changes the portfolio. "
    "Do not force a purchase to fill the sleeve, impose arbitrary splits, or assume "
    "a profitable business is safe at any valuation.\n"
    "  (f) RESEARCH IS NOT AN ORDER. Missing facts mean investigate or WATCH with "
    "specific unresolved questions, not a fabricated endpoint or automatic PASS. "
    "State dated catalysts, falsifiers and the next review point. Rank by evidenced "
    "upside plausibility, downside and portfolio contribution, not headline multiple "
    "or generic safety alone. Do not retrofit known winners into proof of foresight."
)

# Weight per grade. Within THIS sleeve a grade reflects time-aware asymmetry:
# HIGH means the strongest evidenced upside relative to downside and horizon,
# not the largest advertised multiple or the safest / most defensible name. The
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
