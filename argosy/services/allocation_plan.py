"""Canonical target-allocation plan — the agreed asset-class mix the plan
deconcentrates toward, and the single source the synthesizer + glidepath bind
to.

Provenance: a multi-agent investment panel (four lenses — long-hold dividend,
total-market Boglehead, risk-&-FX, capital-preservation — proposed, then
adversarially critiqued each other, then a synthesizer reconciled one mix with
per-class agreement levels + dissent). The full panel transcript lives in the
session review artifact; the *agreed* output is encoded here as the canonical
plan input so every downstream surface reads ONE allocation, not a side file.

Two numbers are not free panel choices and are handled specially so nothing is
a magic constant:

  * **Strategic single-stock (NVDA)** is held at ``NVDA_TARGET_PCT`` — a
    CAP-DERIVED figure decided by Argosy's allocation analysis, not a user
    pick. The binding constraint is the PLAN-LOOK-THROUGH concentration cap
    (``DEFAULT_NVDA_CAP_PCT`` = 13%, the MIN-of-four-constraints figure):
    because the index sleeves themselves carry NVDA weight, the direct target
    must sit well below the headline cap. At 8% direct the plan's total NVDA
    look-through is ~11.5% (< 13%) with the current instrument set; at 12% it
    breaches (~16.8%). Re-validated every synthesis against live index
    weights — the constant here is a seed, not authority.

  * **Fixed-income / cash** weight is DERIVED, not asserted. FI is sized as the
    MINIMUM weight (NVDA held fixed, the other equity sleeves kept at their agreed
    ratios, FI split cash/short-IG bonds by ``CASH_FRAC_OF_FI``) at which the
    allocation's COVARIANCE-blended sigma (``sigma_glidepath.covariance_sigma``)
    sits on the phase-aware anchor. The anchor is risk-tolerance POLICY: in the
    accumulation phase (salary covers expenses, no withdrawals → no sequence risk)
    it is ``SIGMA_DIVERSIFIED`` (0.18, the same σ the retirement Monte-Carlo
    assumes as its post-deconcentration floor), which the covariance blend sizes
    to ~8% FI; as retirement nears the anchor glides down (``anchor_sigma_for_phase``)
    so FI rebuilds toward ~15% pre-retirement and ~20% in drawdown.

The portfolio sigma is the covariance blend σ_p = sqrt(wᵀ Σ w), NOT a linear
weighted average: the linear blend assumes ρ=1 (no diversification credit) and
over-states a diversified book's volatility, so as the FI SIZER it produced a
knife-edge over-reserved defensive sleeve. The correlation tiers live (and are
documented) in ``sigma_glidepath``. Remaining model caveat carried in the
rationale: the MC holds mu_real constant regardless of the FI weight, so it sees
FI's volatility benefit but not its return drag.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from typing import TYPE_CHECKING

from argosy.agents.plan_synthesizer_types import SynthTarget

if TYPE_CHECKING:
    from argosy.services.alternatives_types import AlternativesSleeveDecision
from argosy.services.retirement.scenario_mc import (
    DEFAULT_NVDA_CAP_PCT,
    SIGMA_DIVERSIFIED,
)
from argosy.services.retirement.sigma_calibration import _SIGMA_BY_CLASS
from argosy.services.sigma_glidepath import (
    covariance_sigma,
    map_glidepath_class_to_sigma_class,
    sigma_from_composition,
)
from argosy.services.target_allocation_doc import AllocationInstrument

# --- The two specially-handled weights (auditable, not magic). ---------------
# CAP-DERIVED by Argosy's allocation analysis: the 13% DEFAULT_NVDA_CAP_PCT is
# a LOOK-THROUGH cap, and the index sleeves already carry NVDA, so the direct
# target must leave room for indirect exposure. 8.0 direct ⇒ ~11.5% plan
# look-through < 13% with the current instruments; 12 breaches. Re-validated
# every synthesis — this constant is a SEED, not authority.
#
# REFACTOR NEEDED (Ariel, 2026-07-07): "NVDA" here is the current tenant's
# instance of a generic class — a concentrated EMPLOYER-EQUITY position
# accumulated via RSU vesting, not purchased by choice. The sleeve (symbol,
# target, cap inputs, situs exception, glide) belongs in per-user plan state,
# not a ticker-named constant. See the 2026-07-07 handover open-items note.
NVDA_TARGET_PCT = 8.0
assert NVDA_TARGET_PCT <= DEFAULT_NVDA_CAP_PCT * 100.0 + 1e-9

# The FI sleeve is split into a liquid cash/T-bill tranche (home of the
# earmarked ILS hedge + the 2-year deconcentration working capital + the
# near-term bridge buffer) and a short-duration IG-bond tranche (yield on the
# rest). Cash-heavy because bridge liquidity + the shekel-appreciation hedge
# dominate the sleeve's job; a parameter, not a law.
CASH_FRAC_OF_FI = 0.70

# --- Alternatives sleeve (TEAM-SOURCED, not hardcoded). ----------------------
# The Alternatives sleeve's SIZE and INSTRUMENTS are derived by the agent fleet
# (sourced -> deterministically verified -> estate-gated -> debated -> sized by
# the fund manager) and supplied to the engine as an AlternativesSleeveDecision.
# There is no fixed % and no fixed instrument list here: a 0% sleeve (no
# decision) is a valid outcome and produces NO alternatives class. The engine
# holds the supplied sleeve as a fixed policy weight subtracted before the
# equity sleeves are renormalised; FI remains the sigma-solver and absorbs the
# sleeve's SOURCED sigma to keep the blended sigma on the anchor.
_ALTERNATIVES_LABEL = "Alternatives"
_ALTERNATIVES_SIGMA_CLASS = "alternatives"
_ALTERNATIVES_SNAPSHOT_CATEGORY = "Alternative"

# --- High-growth / high-potential sleeve (TEAM/DISCOVERY-SOURCED). -----------
# A PERMANENT strategic ~5% "moonshot" sleeve sourced GLOBALLY (US / EU / ISR /
# anywhere) under the binding x10-ASYMMETRY mandate (Ariel 2026-07-06, see
# high_potential_sleeve.X10_SLEEVE_MANDATE): names that can plausibly 10x on
# cap-math, ranked asymmetry-first with accepted per-name loss = 100% — NOT
# conviction-safety. Deliberately NOT estate-gated (unlike the core sleeves):
# US-situs estate exposure on this small sleeve is an accepted cost of x10
# upside (Ariel's call). Modeled exactly like the Alternatives sleeve: a fixed
# weight held out before the equity renorm, whose SOURCED sigma is fed to the FI
# solver so the book's covariance-blended sigma stays on the anchor (a volatile
# sleeve correctly forces slightly MORE FI). Correlation tier = concentrated_equity
# (single-stock high-beta). A 0% sleeve adds no class (byte-identical default).
HIGH_GROWTH_LABEL = "High-growth / high-potential"
HIGH_GROWTH_SIGMA_CLASS = "high_growth_basket"
HIGH_GROWTH_SNAPSHOT_CATEGORY = "Individual Stocks"
# Fleet-sourced default (codex-verified): a diversified ~8-12 name global high-growth
# basket blends to ~0.35, NOT the 0.45 single-stock level, and correlates ~0.60 with
# the NVDA single name (not 1.0) — see the high_growth_basket tier in sigma_glidepath.
HIGH_GROWTH_DEFAULT_SIGMA = 0.35


# --- Sleeve label migration (relabel without breaking durable overrides). ----
# Sleeve labels are KEYS in persisted ``target_allocation_overrides_json`` rows
# (option-B durable authored overrides survive re-synthesis), so an honest
# relabel must keep legacy keys working. ``normalize_override_labels`` migrates
# legacy keys to the current label; an explicit current-label entry wins over a
# legacy alias on collision.
# 2026-07-06 relabel: "US growth tilt (ex-NVDA)" → "Global quality growth
# (ex-NVDA-dense)". The sleeve's primary is IWQU (iShares Edge MSCI World
# Quality Factor) — a WORLD quality-factor fund, ~33% ex-US and NOT literally
# ex-NVDA (~6.45% NVDA at the 2026-07 holdings refresh). The old label
# overstated both the US-ness and the NVDA exclusion (v65 blind-review finding).
US_GROWTH_LEGACY_LABEL = "US growth tilt (ex-NVDA)"
GLOBAL_QUALITY_GROWTH_LABEL = "Global quality growth (ex-NVDA-dense)"
SLEEVE_LABEL_ALIASES: dict[str, str] = {
    US_GROWTH_LEGACY_LABEL: GLOBAL_QUALITY_GROWTH_LABEL,
}


def normalize_sleeve_label(label: str) -> str:
    """Map a (possibly legacy) sleeve label to its current canonical label."""
    return SLEEVE_LABEL_ALIASES.get(label, label)


def normalize_override_labels(overrides: dict[str, float]) -> dict[str, float]:
    """Migrate legacy sleeve-label keys in an authored-overrides dict to the
    current labels. On collision (both the legacy and the current key present)
    the explicit CURRENT-label entry wins — a fresh edit outranks a carried
    legacy row."""
    if not overrides:
        return dict(overrides or {})
    legacy = {k: v for k, v in overrides.items() if k in SLEEVE_LABEL_ALIASES}
    current = {k: v for k, v in overrides.items() if k not in SLEEVE_LABEL_ALIASES}
    out: dict[str, float] = {}
    for k, v in {**legacy, **current}.items():
        out[SLEEVE_LABEL_ALIASES.get(k, k)] = v
    return out


# --- Agreed equity/alts sleeves (the panel's mix, NVDA + FI handled above). --
# ``ratio`` is the panel's agreed RELATIVE weight among the non-NVDA, non-FI
# sleeves; absolute weights are filled by renormalisation once FI is derived.
@dataclass(frozen=True)
class _PanelSleeve:
    label: str            # engine-safe label (maps 1:1 onto a sigma-class)
    ratio: float          # agreed relative weight among the equity/alts sleeves
    sigma_class: str
    snapshot_category: str  # portfolio-snapshot category for today's anchor
    agreement: str
    rationale: str
    dissent: str = ""
    instruments: tuple[AllocationInstrument, ...] = ()


_EQUITY_SLEEVES: tuple[_PanelSleeve, ...] = (
    _PanelSleeve(
        label="US broad-market core",
        ratio=31.0,
        instruments=(
            AllocationInstrument(
                symbol="CSPX", role="primary", weight_within_class_pct=100.0, domicile="IE",
                rationale=(
                    "S&P 500 core via the Irish-domiciled UCITS CSPX (Acc, ~0.07% TER), "
                    "NOT US-domiciled VOO. For a non-US-person, UCITS shares are NOT "
                    "US-situs, so this preserves the economic exposure without adding to "
                    "the ~$1M US estate-tax tail (no US-Israel estate treaty; $60K NRA "
                    "exemption, up to 40%). Cite domain_knowledge/tax/us/estate_tax_nonresidents.md. "
                    "The household already holds CSPX."
                ),
            ),
        ),
        sigma_class="us_equity",
        snapshot_category="Core Equity",
        agreement="moderate",
        rationale=(
            "Cheapest, most tax-efficient total-market return engine (VOO/VTI), "
            "sized to clear the MC central 5.0%-real hurdle. Reconciled down from "
            "the Boglehead 40 (under-funds the bridge income need) and up from the "
            "long-hold 20 (over-tilted to income). Deploy NEW NVDA-proceeds cash "
            "here rather than selling appreciated non-NVDA sleeves."
        ),
        dissent="Lens range 20-40; income lenses pulled it toward ~22-24, Boglehead to 40.",
    ),
    _PanelSleeve(
        label="Dividend-quality income",
        ratio=11.0,
        instruments=(
            AllocationInstrument(
                symbol="FUSA", role="primary", weight_within_class_pct=100.0, domicile="IE",
                rationale=(
                    "US quality-income via the Irish UCITS FUSA (Fidelity US Quality "
                    "Income, Acc, ~0.25% TER), NOT US-domiciled SCHD. There is no exact "
                    "SCHD twin in UCITS form — FUSA is the closest US-quality-dividend "
                    "wrapper and tilts slightly more mega-cap/quality-growth (an accepted "
                    "drift). Chosen because UCITS shares avoid US-situs estate exposure "
                    "for a non-US-person; cite estate_tax_nonresidents.md. SCHD itself is "
                    "fundamentally sound — the swap is for DOMICILE, not for any momentum/"
                    "fundamental weakness."
                ),
            ),
        ),
        sigma_class="us_equity",
        snapshot_category="Dividend",
        agreement="moderate",
        rationale=(
            "Trimmed to ~11% (from a ~17-18% over-weight) to fund the higher growth "
            "tilt for the accumulation-phase wealth-maximization mandate, while "
            "retaining the quality/profitability drawdown cushion. "
            "US quality-factor sleeve (the quality/profitability tilt that historically "
            "cushions drawdowns) implemented via the ACCUMULATING UCITS FUSA — "
            "deliberately accumulating, NOT distributing: for an Israeli holder a "
            "distributed dividend is a non-deferrable annual tax event (~25-30%), so the "
            "sleeve harvests total return through CONTROLLED SALES (CGT timed by the "
            "household) rather than forced dividend income. Same sequence-risk defense as "
            "a dividend sleeve, without the annual dividend-tax drag. Matches the "
            "household's long-hold style. (If a true cash-distributing income stream is "
            "wanted in drawdown, switch to the distributing share FUSD and accept the "
            "dividend-tax event — a deliberate, separate choice.)"
        ),
        dissent=(
            "Style-vs-tax split (long-hold 30 keystone vs Boglehead 8): Israeli "
            "25-30% dividend tax is a non-deferrable annual event; quantify the "
            "drag vs forced-sale-avoidance against the pension-stack waterfall."
        ),
    ),
    _PanelSleeve(
        label="International developed (ex-US)",
        ratio=11.0,
        instruments=(
            AllocationInstrument(
                symbol="EXUS", role="primary", weight_within_class_pct=100.0, domicile="IE",
                rationale=(
                    "Developed-world ex-US equity via the Irish UCITS EXUS (Xtrackers "
                    "MSCI World ex-USA, Acc, ~0.15% TER), NOT US-domiciled VEA. Closest "
                    "ex-US developed twin; lacks small-caps and carries minor MSCI/FTSE "
                    "country drift (accepted). UCITS domicile keeps it off the US-situs "
                    "estate base; cite estate_tax_nonresidents.md."
                ),
            ),
        ),
        sigma_class="intl_equity",
        snapshot_category="International",
        agreement="moderate",
        rationale=(
            "Lifted hard from ~2% — the book's biggest diversification gap. Held at "
            "~11 (with emerging markets split into its own sleeve) because ex-US "
            "developed equity hedges USD-CONCENTRATION but NOT the named shekel-"
            "appreciation risk (it is EUR/JPY/GBP, not NIS), and the engine models "
            "its sigma (0.20) above US equity (0.18). DIVERSIFIER ADJUDICATION "
            "(2026-07-06, gold ON TRIAL, author + blind re-derivation both "
            "independently concluded GOLD LOSES): the ~4pp deepening of this sleeve "
            "(authored override) IS the plan's diversifier decision — EXUS (0% US, "
            "0% NVDA, EUR/JPY/GBP) beat a physical-gold slice, and beat WSML "
            "(51.5% US) / INFR (62.6% US) / EIMI deepening (~48% Taiwan+Korea "
            "AI-chain overlap) on actual look-through. ADJUDICATED BY MODEL "
            "(2026-07-06 v65 remediation, covariance kernel + return arithmetic; "
            "the retirement MC is single-mu so per-class returns are arithmetic, "
            "not simulated): a 3% gold sleeve (sigma 0.16 per the framework's "
            "sourced gold-ETC vol; funded -2pp US core / -1pp EXUS) cuts blended "
            "sigma 0.1682 -> 0.1644 (framework rho_gold-equity 0.25; 0.1631 at "
            "the reviewers' long-run rho 0.0; the 0.1682 baseline is the "
            "adjudication's -2pp-core/-1pp-EXUS funding-mix scenario, vs the "
            "plan's as-staged 0.1715 — a blind re-derivation confirmed the "
            "verdict sign is unchanged at the 0.1715 basis) — but the book is "
            "ALREADY under the 0.18 anchor without it, so the sigma credit buys "
            "no solvency the plan needs. Cost: gold's long-run real return ~0.7-1.0%/yr (UBS/CS "
            "Global Investment Returns Yearbook 1900-2023 ~0.7%; Erb & Harvey "
            "2013 ~0-1%) vs the MC's 5.0% real equity base = ~0.12pp/yr expected "
            "real-return drag on the book. Geometric-growth net (g = mu - "
            "sigma^2/2, the model's own arithmetic): -0.056pp/yr at central "
            "parameters, still -0.021pp/yr at gold's BEST case (rho 0.0, 1.5% "
            "real) — gold lowers the compounding rate in every parameterization, "
            "so it is OUT on the numbers. The 'produces nothing' objection is "
            "thus quantified, not rhetorical: the insurance premium (~0.12pp/yr) "
            "exceeds the variance benefit (~0.06-0.08pp/yr CE) for this book. "
            "Funded by trimming US broad-market core -3pp and US low-vol -1pp."
        ),
        dissent=(
            "Direction (lift from ~2%) is the strongest cross-lens agreement; "
            "magnitude 7-18 contested. NO lens's international weight hedges the "
            "named ILS risk — that lives in the FI sleeve's earmarked ILS tranche."
        ),
    ),
    _PanelSleeve(
        label="Emerging-markets equity",
        ratio=4.0,
        instruments=(
            AllocationInstrument(
                symbol="EIMI", role="primary", weight_within_class_pct=100.0, domicile="IE",
                rationale=(
                    "Emerging-markets equity via the Irish UCITS EIMI (iShares Core MSCI "
                    "EM IMI, Acc, ~0.18% TER), NOT a US-domiciled EM ETF (VWO/IEMG). "
                    "Broad EM-IMI (large/mid/small) for the growth + diversification "
                    "breadth the developed-only book was missing; the household already "
                    "holds EIMI. UCITS domicile keeps it off the US-situs estate base; "
                    "cite estate_tax_nonresidents.md."
                ),
            ),
        ),
        sigma_class="emerging_equity",
        snapshot_category="International",  # EM is bucketed under International today
        agreement="moderate",
        rationale=(
            "Re-added as an explicit ~4% sleeve (the original plan carried EM; the "
            "prior Argosy target dropped it). EM adds growth and breadth uncorrelated "
            "enough to earn a place under the covariance blend, at a higher modeled "
            "sigma (0.25) the FI solver accounts for. Sized small — it is higher-vol "
            "and adds some USD/global-cycle beta, not an ILS hedge."
        ),
        dissent=(
            "Magnitude 3-5 contested; EM's higher sigma and governance/FX tail argue "
            "for a modest sleeve rather than a full market-cap EM weight."
        ),
    ),
    _PanelSleeve(
        label=GLOBAL_QUALITY_GROWTH_LABEL,
        ratio=13.0,
        instruments=(
            AllocationInstrument(
                symbol="IWQU", role="primary", weight_within_class_pct=100.0, domicile="IE",
                rationale=(
                    "Quality-growth via the Irish UCITS IWQU (iShares Edge MSCI World "
                    "Quality Factor, Acc, ISIN IE00BP3QZ601, ~0.25% TER, ~EUR 4.9bn). "
                    "PLAN-CHANGE TEAM DECISION (2026-07-06; author + blind re-derivation "
                    "diverged QDVB-vs-IWQU, reconciled to IWQU — the author conceded the "
                    "blind reviewer): replaces R1GR (Russell 1000 Growth UCITS), which was "
                    "13.93% NVDA (justETF IE000NITTFF2, 2026-05-29) — the densest NVDA "
                    "instrument in the plan on a book deconcentrating from NVDA; the old "
                    "open item ('no UCITS true-ex-NVDA growth ETF exists') is closed by "
                    "accepting a quality-growth FACTOR engine instead of a growth index. "
                    "IWQU is 5.11% NVDA (justETF IE00BP3QZ601, 2026-05-29) — at the "
                    "client's <=~5% target, ~-63% vs R1GR — and its mega-cap-AI top-3 "
                    "(MSFT 5.76 + AAPL 5.58 + NVDA 5.11 = ~16.4%) is far below R1GR's "
                    "~36%. Honest trade-offs recorded: (a) WORLD quality — ~33% of the "
                    "sleeve is ex-US and overlaps the International sleeve (directionally "
                    "aligned with de-concentrating a ~92%-US book; manage via look-through); "
                    "(b) 5.11% NVDA is at the tolerance edge and quality indices rebalance — "
                    "re-pull holdings before execution. HOLDINGS REFRESH 2026-07: NVDA is "
                    "now IWQU's #1 holding at ~6.45% (stockanalysis.com / iShares product "
                    "page, 2026-07) — ABOVE the ~5% target edge; the look-through table "
                    "models it at 0.065 conservative-high (LOOKTHROUGH_VERSION 4) and the "
                    "13% NVDA look-through cap is verified against that number. "
                    "Durable-zero-NVDA alternatives "
                    "(XDEW equal-weight / SPY4 mid-cap / XRS2 small-cap) REJECTED: they "
                    "abandon the large-cap-growth factor this sleeve exists for (an FI "
                    "cost); momentum (IWMO/XDEM, ~2% NVDA today) REJECTED: the low NVDA is "
                    "regime-dependent (can rotate back ~5-6% at a semi-annual rebalance) "
                    "and only ~50% US. UCITS domicile avoids US-situs estate exposure; "
                    "cite estate_tax_nonresidents.md."
                ),
            ),
        ),
        sigma_class="world_quality_equity",
        snapshot_category="Growth",
        agreement="moderate",
        rationale=(
            "Raised to ~13% (from a ~6% sliver) as the core of the accumulation-phase "
            "wealth-maximization mandate: a salaried, no-withdrawal, 5+yr investor is "
            "under-served by a token growth weight. Quality-growth compounding via "
            "UCITS IWQU (replaced R1GR 2026-07-06 — 13.93% NVDA was re-adding the "
            "single-name risk the glide sheds). HONEST LABEL (v65 remediation): IWQU "
            "is a WORLD quality-factor fund, ~33% ex-US — this sleeve is neither "
            "US-only nor growth-index, so it is labeled and risk-modeled as world "
            "quality equity (world_quality_equity, 0.17), not US large-cap growth "
            "(0.21). DISCLOSURE — total ex-US shift of the v65 refinement: ~+7.6pp "
            "of the book (EXUS +4.0pp headline authored override, plus ~+3.6pp "
            "embedded here: 11% sleeve x ~33% ex-US via IWQU), on top of the EM "
            "sleeve. Still bounded below NVDA-stacking territory — NVDA already "
            "supplies concentrated high-beta tech at the cap, so growth is held "
            "below the point where correlated tech beta re-adds the factor risk the "
            "deconcentration sheds. The '(ex-NVDA-dense)' suffix is stripped by the "
            "sigma-mapper's exclusion regex, so the label maps to "
            "world_quality_equity, not the 0.45 single-stock class."
        ),
        dissent=(
            "Magnitude raised per the rebuild verdict (growth was under-weight for the "
            "prime directive); upper bound is the NVDA-correlated-beta ceiling."
        ),
    ),
    _PanelSleeve(
        label="US low-volatility equity",
        ratio=6.0,
        instruments=(
            AllocationInstrument(
                symbol="SPMV", role="primary", weight_within_class_pct=100.0, domicile="IE",
                rationale=(
                    "US min-volatility via the Irish UCITS SPMV (iShares S&P 500 Min "
                    "Volatility, Acc, ~0.20% TER), NOT US-domiciled USMV. Kept US-only "
                    "(the WORLD min-vol UCITS MVOL would break the plan's US/ex-US split). "
                    "UCITS domicile avoids US-situs estate exposure; cite estate_tax_nonresidents.md."
                ),
            ),
        ),
        sigma_class="low_vol_equity",
        snapshot_category="Defensive",
        agreement="moderate",
        rationale=(
            "Min-vol / quality-defensive equity (USMV-like) damping early-bridge "
            "drawdowns while still paying a dividend. Modeled at its true ~0.13 "
            "risk (a real equity sleeve), NOT the 0.06 IG-bond floor it used to be "
            "mis-mapped to. Trimmed to 6 to avoid double-counting the value/quality "
            "factor it shares with the dividend sleeve."
        ),
        dissent="Magnitude 4-12; open question whether it is distinct from dividend-quality.",
    ),
    _PanelSleeve(
        label="Real assets (REIT/TIPS)",
        ratio=2.0,
        instruments=(
            AllocationInstrument(
                symbol="DPYA", role="primary", weight_within_class_pct=100.0, domicile="IE",
                rationale=(
                    "Real-assets sliver via the Irish UCITS DPYA (iShares Developed "
                    "Markets Property Yield, Acc, ~0.59% TER), NOT US-domiciled VNQ. This "
                    "is developed-WORLD property (not US-only REIT), an accepted broadening "
                    "for a 1% token sleeve. UCITS domicile avoids US-situs estate exposure; "
                    "cite estate_tax_nonresidents.md."
                ),
            ),
        ),
        sigma_class="real_estate",
        snapshot_category="Alternative",
        agreement="contested",
        rationale=(
            "Token REIT/TIPS sliver as a thin inflation/late-life-tail hedge. Kept "
            "minimal: the household is a transparency-valuing long-hold investor, "
            "not an alts buyer, and US REITs are USD-denominated (no ILS hedge)."
        ),
        dissent="0 (Boglehead) vs 7 (Risk); 1 is nearly the Boglehead position.",
    ),
)

_NVDA_SLEEVE = _PanelSleeve(
    label="Strategic single-stock (NVDA)",
    ratio=0.0,  # fixed weight, not part of the renormalised ratios
    instruments=(
        AllocationInstrument(
            symbol="NVDA", role="primary", weight_within_class_pct=100.0, domicile="US",
            rationale="The strategic single-stock position itself.",
        ),
    ),
    sigma_class="concentrated_equity",
    snapshot_category="Individual Stocks",
    agreement="contested",
    rationale=(
        f"Held at {NVDA_TARGET_PCT:.0f}% — CAP-DERIVED by Argosy's allocation "
        "analysis against the 13% LOOK-THROUGH concentration cap (the "
        "MIN-of-four-constraints ceiling: sequence / tail-loss / "
        "risk-contribution / tax-liquidity). The cap counts NVDA held INSIDE "
        "the index sleeves too: with the current instrument set, an 8% direct "
        "target puts total plan look-through at ~11.5% (< 13% with headroom "
        "for drift); a 12% direct target breaches at ~16.8%. Retains the "
        "conviction position + low-basis CGT deferral within the cap. Pair "
        "with a trim-on-breach band. NVDA's ~0.45 single-name sigma remains a "
        "dominant variance contributor even at 8% — the accepted residual "
        "idiosyncratic tail. Re-validated every synthesis; the constant is a "
        "seed, not authority."
    ),
    dissent=(
        "Panel lenses spanned 13 (long-hold/Boglehead/risk) vs 10 "
        "(capital-preservation) on the DIRECT weight; the binding constraint "
        "is the 13% look-through cap. With current instruments the cap "
        "clears up to ~9.5 direct; 8 is held as a DELIBERATE ~1.5pp drift "
        "buffer — index look-through weights demonstrably drift (IWQU's NVDA "
        "weight moved +1.3pp in five weeks) and a breach forces trades. "
        "Buffer size is re-validated every synthesis (~NIS 87k of deployable "
        "book per point: over-buffering is anti-goal, under-buffering forces "
        "churn)."
    ),
)

# --- Output model ------------------------------------------------------------
@dataclass(frozen=True)
class AllocationClass:
    label: str
    target_pct: float
    sigma_class: str
    snapshot_category: str
    agreement: str
    rationale: str
    dissent: str = ""
    instruments: tuple[AllocationInstrument, ...] = ()


@dataclass(frozen=True)
class TargetAllocation:
    classes: list[AllocationClass]
    blended_sigma: float
    anchor_sigma: float
    fi_pct: float
    nvda_pct: float
    cash_pct: float
    bonds_pct: float
    overall_rationale: str
    residual_disagreements: str
    provenance: str = "multi-agent allocation panel (4 lenses → adversarial critique → synthesis)"
    deployable_nis: float | None = None


def _blended_sigma_for(
    weights: dict[str, float],
    *,
    alt_label: str | None = None,
    alt_sigma: float | None = None,
    hg_label: str | None = None,
    hg_sigma: float | None = None,
) -> float:
    """Covariance-aware blended sigma. When an Alternatives sleeve is present its
    sigma is the SOURCED ``alt_sigma`` (pinned by label), not the fixed class
    constant — so a gold-only sleeve blends at 0.16 and an 80/20 gold/BTC sleeve
    at 0.268, exactly as the team sourced it — while its CORRELATION to the rest of
    the book uses the ``alternatives`` tier. The high-growth sleeve is pinned the
    same way against its SOURCED ``hg_sigma`` in the ``concentrated_equity`` tier."""
    alt_pinned = alt_label is not None and alt_sigma is not None
    hg_pinned = hg_label is not None and hg_sigma is not None
    if not alt_pinned and not hg_pinned:
        return sigma_from_composition(weights)
    items: list[tuple[str, float, float]] = []
    for label, pct in weights.items():
        if pct <= 0:
            continue
        if alt_pinned and label == alt_label:
            items.append((_ALTERNATIVES_SIGMA_CLASS, pct, alt_sigma))
        elif hg_pinned and label == hg_label:
            items.append((HIGH_GROWTH_SIGMA_CLASS, pct, hg_sigma))
        else:
            cls = map_glidepath_class_to_sigma_class(label)
            items.append((cls, pct, _SIGMA_BY_CLASS.get(cls, 0.20)))
    return covariance_sigma(items)


def _renormalise(
    *,
    nvda_pct: float,
    fi_pct: float,
    alternatives_pct: float = 0.0,
    high_growth_pct: float = 0.0,
) -> dict[str, float]:
    """Hold NVDA + FI + Alternatives + High-growth fixed; distribute the rest among
    the equity sleeves at their agreed ratios; split FI into cash + short-IG bonds.

    The team-sourced Alternatives and High-growth weights are subtracted off the
    book BEFORE the equity sleeves are sized (they displace the non-NVDA risky
    sleeves pro rata), so a larger fixed sleeve shrinks equity and — via its sigma —
    indirectly forces more FI to hold the anchor. A ``*_pct=0`` sleeve adds no class."""
    other_total = 100.0 - nvda_pct - fi_pct - alternatives_pct - high_growth_pct
    ratio_sum = sum(s.ratio for s in _EQUITY_SLEEVES)
    weights: dict[str, float] = {
        s.label: s.ratio / ratio_sum * other_total for s in _EQUITY_SLEEVES
    }
    weights[_NVDA_SLEEVE.label] = nvda_pct
    if alternatives_pct > 0:
        weights[_ALTERNATIVES_LABEL] = alternatives_pct
    if high_growth_pct > 0:
        weights[HIGH_GROWTH_LABEL] = high_growth_pct
    weights["Cash & T-bills (incl. ILS tranche)"] = fi_pct * CASH_FRAC_OF_FI
    weights["Short-duration IG bonds"] = fi_pct * (1.0 - CASH_FRAC_OF_FI)
    return weights


# --- Phase-aware risk anchor -------------------------------------------------
# The covariance blend is a FIXED risk MODEL; the anchor is the risk-tolerance
# POLICY, and it is phase-aware. In ACCUMULATION (salary covers expenses, no
# withdrawals → no sequence risk) the book may run at the steady-state diversified
# anchor (``SIGMA_DIVERSIFIED`` 0.18 — the same σ the retirement MC assumes as its
# post-deconcentration floor), which the covariance blend sizes to ~8% FI. As
# ACTUAL retirement nears, sequence risk demands a LOWER portfolio σ (a larger
# defensive sleeve): the anchor glides DOWN, lifting FI toward ~15% in the final
# years and ~20% once the portfolio is being drawn. The anchor is derived from
# years-to-retirement; no per-phase FI percentage is hardcoded.
ACCUMULATION_ANCHOR = SIGMA_DIVERSIFIED  # 0.18 → ~8% FI under the covariance blend
PRESERVATION_ANCHOR = 0.165              # ~15% FI, reached as retirement arrives
DRAWDOWN_ANCHOR = 0.155                  # ~20% FI once the portfolio funds spending
PRESERVATION_GLIDE_YEARS = 3.0           # FI rebuild window ahead of retirement


def anchor_sigma_for_phase(years_to_retirement: float | None) -> float:
    """Phase-aware σ anchor the FI solver targets, derived from years-to-actual-
    retirement. ``None`` or beyond the rebuild window → the accumulation anchor;
    inside the window it glides linearly to the preservation anchor; at/after
    retirement → the drawdown anchor. A LOWER anchor forces MORE fixed income."""
    if years_to_retirement is None or years_to_retirement >= PRESERVATION_GLIDE_YEARS:
        return ACCUMULATION_ANCHOR
    if years_to_retirement <= 0.0:
        return DRAWDOWN_ANCHOR
    frac = years_to_retirement / PRESERVATION_GLIDE_YEARS  # 1→accumulation, 0→preservation
    return round(
        PRESERVATION_ANCHOR + (ACCUMULATION_ANCHOR - PRESERVATION_ANCHOR) * frac, 4
    )


def derive_fi_weight(
    *,
    anchor_sigma: float = SIGMA_DIVERSIFIED,
    nvda_pct: float = NVDA_TARGET_PCT,
    alternatives_pct: float = 0.0,
    alternatives_sigma: float = 0.0,
    high_growth_pct: float = 0.0,
    high_growth_sigma: float = 0.0,
    fi_step: float = 0.01,
    fi_lo: float = 8.0,
    fi_hi: float = 35.0,
) -> float:
    """Minimum FI weight (in ``fi_step`` increments) at which the allocation's
    engine-blended sigma sits at/under the steady-state anchor — the sigma the
    optimizer used to certify the earliest-safe age. Self-consistency, not a
    chosen constant. A team-sourced Alternatives sleeve is held at
    ``alternatives_pct`` and its SOURCED ``alternatives_sigma`` is what FI must
    offset (a higher sourced sigma forces more FI); the High-growth sleeve is
    held + offset identically via ``high_growth_pct`` / ``high_growth_sigma``."""
    alt_label = _ALTERNATIVES_LABEL if alternatives_pct > 0 else None
    alt_sigma = alternatives_sigma if alternatives_pct > 0 else None
    hg_label = HIGH_GROWTH_LABEL if high_growth_pct > 0 else None
    hg_sigma = high_growth_sigma if high_growth_pct > 0 else None

    def clears(weight: float) -> bool:
        weights = _renormalise(
            nvda_pct=nvda_pct, fi_pct=weight, alternatives_pct=alternatives_pct,
            high_growth_pct=high_growth_pct,
        )
        return _blended_sigma_for(
            weights, alt_label=alt_label, alt_sigma=alt_sigma,
            hg_label=hg_label, hg_sigma=hg_sigma,
        ) <= (anchor_sigma + 1e-9)

    fi = fi_lo
    while fi <= fi_hi:
        if clears(fi):
            # Revalidate the 2dp-ROUNDED return: rounding the raw solver value down
            # (possible when fi_step < 0.01) could land just under the anchor again,
            # so bump by 0.01 until the value we actually return clears.
            candidate = round(fi, 2)
            while candidate <= fi_hi and not clears(candidate):
                candidate = round(candidate + 0.01, 2)
            return candidate
        fi += fi_step
    return round(fi_hi, 2)


_FI_CASH = AllocationClass(
    label="Cash & T-bills (incl. ILS tranche)",
    target_pct=0.0,
    sigma_class="cash",
    snapshot_category="Cash",
    agreement="contested",
    rationale=(
        "Liquid sequence-of-returns shock absorber + home of the only TRUE "
        "shekel-appreciation hedge (an earmarked ILS-denominated / short-makam "
        "tranche) + the 2-year deconcentration working capital. Sized as part of "
        "the DERIVED FI weight (see plan rationale): enough to fund the bridge "
        "from interest, not forced equity sales, in a strong-shekel or down year."
    ),
    dissent=(
        "FI was the panel's most-contested class (8/13/24/29). It is DERIVED, not "
        "asserted: the minimum weight at which the book's covariance-blended sigma "
        "sits AT OR UNDER the phase-aware anchor (the solver floors and discretizes, so the blended sigma typically lands slightly below it). In accumulation (salary = safety net) that "
        "is ~8%; the sleeve rebuilds toward ~15% as retirement nears."
    ),
    instruments=(
        AllocationInstrument(
            symbol="IB01", role="primary", weight_within_class_pct=100.0, domicile="IE",
            rationale=(
                "0-1yr US Treasuries via the Irish UCITS IB01 (iShares $ Treasury Bond "
                "0-1yr, Acc, ~0.07% TER), NOT US-domiciled SGOV. Cleanest of all for a "
                "non-US-person is holding T-bills / USD deposits DIRECTLY (estate-exempt "
                "under IRC §2105(b)(1)/§871(h)); IB01 is the ETF fallback for trading "
                "convenience and is non-US-situs as a UCITS wrapper. The earmarked ILS "
                "short-makam hedge tranche is held within this sleeve. Cite "
                "estate_tax_nonresidents.md."
            ),
        ),
    ),
)
_FI_BONDS = AllocationClass(
    label="Short-duration IG bonds",
    target_pct=0.0,
    sigma_class="bonds",
    snapshot_category="Defensive",
    agreement="contested",
    rationale=(
        "Short-duration investment-grade bonds (SGOV/short Treasuries) — the "
        "yield-bearing remainder of the derived FI sleeve, kept short to limit "
        "real-rate/re-investment risk on the bridge ladder."
    ),
    dissent="Part of the contested FI sleeve; weight follows the derived FI total.",
    instruments=(
        AllocationInstrument(
            symbol="IBTA", role="primary", weight_within_class_pct=100.0, domicile="IE",
            rationale=(
                "1-3yr US Treasuries via the Irish UCITS IBTA (iShares $ Treasury Bond "
                "1-3yr, Acc, ~0.07% TER), NOT US-domiciled VGSH. As with the cash sleeve, "
                "a direct 1-3y Treasury ladder is cleanest for a non-US-person; IBTA is "
                "the non-US-situs ETF fallback. Cite estate_tax_nonresidents.md."
            ),
        ),
    ),
)


def _candidate_to_instrument(c) -> AllocationInstrument:
    """Convert a verified Alternatives candidate to a canonical instrument, using
    the verifier-RESOLVED domicile/ISIN (never the agent's raw claim)."""
    isin = c.verification.resolved_isin or c.isin
    return AllocationInstrument(
        symbol=c.symbol,
        role="primary",
        weight_within_class_pct=c.weight_within_sleeve_pct,
        rationale=f"[{c.asset_class}] ISIN {isin} (conviction={c.conviction}) {c.thesis_md}".strip(),
        domicile=c.verification.resolved_domicile or c.domicile,
    )


def _apply_authored_overrides(
    weights: dict[str, float],
    authored_overrides: dict[str, float],
) -> dict[str, float]:
    """Apply authored sleeve overrides to engine-derived weights, renormalising
    the non-overridden sleeves to fill the remainder proportionally.

    Validation (fail loud):
    - Any override value < 0 → ValueError.
    - Any override key not present in ``weights`` (unknown label) → ValueError.
    - sum(override values) > 100 + eps → ValueError.
    - All sleeves are overridden but their sum is not ≈ 100 → ValueError.

    When no overrides, returns ``weights`` unchanged (purity: identical path).

    Sigma anchor: an authored override MAY move the portfolio's blended_sigma
    off the derived anchor; this is intentional for option-B authored allocation.
    The engine respects the override and reports the resulting blended_sigma. It
    is the downstream risk/invariant gate's job to flag a sigma-anchor breach —
    not the engine's to warn or clamp.

    CASH_FRAC_OF_FI: the 70/30 cash/short-IG split is NOT re-enforced when only
    one FI sub-sleeve is overridden granularly (accepted limitation of granular
    authored overrides).
    """
    if not authored_overrides:
        return weights

    # Legacy sleeve-label keys (persisted durable overrides predating a relabel)
    # are migrated to the current labels before validation, so a relabel never
    # breaks stored plan rows (see SLEEVE_LABEL_ALIASES).
    authored_overrides = normalize_override_labels(authored_overrides)

    eps = 1e-6
    known_labels = set(weights.keys())

    # --- Validation ---
    for label, pct in authored_overrides.items():
        if label not in known_labels:
            raise ValueError(
                f"Unknown sleeve label in authored_overrides: {label!r}. "
                f"Known labels: {sorted(known_labels)}"
            )
        if pct < 0.0:
            raise ValueError(
                f"authored_overrides contains a negative value for {label!r}: {pct}. "
                "Sleeve targets must be >= 0."
            )

    override_sum = sum(authored_overrides.values())
    if override_sum > 100.0 + eps:
        raise ValueError(
            f"authored_overrides sum {override_sum:.4f} exceeds 100. "
            "The overridden sleeves cannot consume more than the full book."
        )

    # --- Determine non-overridden sleeves and their engine-derived base total ---
    overridden = set(authored_overrides.keys())
    non_overridden = {lbl: w for lbl, w in weights.items() if lbl not in overridden}

    # When ALL sleeves are overridden: verify sum ≈ 100, use as-is.
    if not non_overridden:
        if abs(override_sum - 100.0) > 0.05:
            raise ValueError(
                f"All sleeves are overridden but their values sum to {override_sum:.4f}, "
                "not ~100. When overriding every sleeve the values must sum to 100."
            )
        return {lbl: authored_overrides[lbl] for lbl in weights}

    # --- Renormalise the non-overridden set to fill the remainder ---
    remainder = 100.0 - override_sum
    base_non_overridden_sum = sum(non_overridden.values())

    result: dict[str, float] = {}
    for lbl in weights:
        if lbl in overridden:
            result[lbl] = authored_overrides[lbl]
        else:
            if base_non_overridden_sum > eps:
                result[lbl] = non_overridden[lbl] / base_non_overridden_sum * remainder
            else:
                # Degenerate: all non-overridden sleeves had 0 weight; split evenly.
                result[lbl] = remainder / len(non_overridden)

    _total = sum(result.values())
    if abs(_total - 100.0) > 1e-6:
        raise ValueError(
            f"allocation weights sum to {_total:.8f}, expected 100.0"
        )
    return result


def build_target_allocation(
    *,
    anchor_sigma: float = SIGMA_DIVERSIFIED,
    nvda_pct: float = NVDA_TARGET_PCT,
    alternatives_sleeve: AlternativesSleeveDecision | None = None,
    years_to_retirement: float | None = None,
    fi_step: float = 0.01,
    deployable_nis: float | None = None,
    authored_overrides: dict[str, float] | None = None,
    high_growth_pct: float = 0.0,
    high_growth_sigma: float = HIGH_GROWTH_DEFAULT_SIGMA,
    high_growth_instruments: "tuple[AllocationInstrument, ...]" = (),
) -> TargetAllocation:
    """Assemble the canonical target allocation with the FI weight derived to the
    steady-state sigma anchor via the covariance-aware blend. Pure: no DB, no clock.

    ``years_to_retirement`` selects the phase-aware anchor (see
    ``anchor_sigma_for_phase``): when supplied it OVERRIDES ``anchor_sigma`` so the
    defensive sleeve rebuilds as retirement nears. When omitted the book is sized
    at the explicit ``anchor_sigma`` (default = the accumulation anchor).

    ``alternatives_sleeve`` is the TEAM's verified, sized decision. When ``None``
    (or a 0% decision) there is NO alternatives class and the book is the
    equity-panel + NVDA + FI baseline. When supplied, its ``target_pct`` is held as
    a fixed policy weight (subtracted before equity renorm) and its SOURCED
    ``sleeve_sigma`` flows into the FI solver.

    ``authored_overrides`` pins named sleeve targets (by label) to human/fleet-
    authored values, surviving re-synthesis. The engine's derived value for each
    named sleeve is replaced by the override; the remaining sleeves are renormalised
    to fill the remainder proportionally. ``None`` (or ``{}``) is byte-identical to
    the un-overridden result. Keys must be exact sleeve labels as returned by this
    function; unknown labels raise ``ValueError`` immediately."""
    if years_to_retirement is not None:
        anchor_sigma = anchor_sigma_for_phase(years_to_retirement)
    alternatives_pct = (
        alternatives_sleeve.target_pct
        if (alternatives_sleeve and alternatives_sleeve.target_pct > 0)
        else 0.0
    )
    alternatives_sigma = (
        alternatives_sleeve.sleeve_sigma if alternatives_pct > 0 else 0.0
    )
    hg_pct = float(high_growth_pct) if high_growth_pct and high_growth_pct > 0 else 0.0
    hg_sigma = float(high_growth_sigma) if hg_pct > 0 else 0.0

    fi_pct = derive_fi_weight(
        anchor_sigma=anchor_sigma, nvda_pct=nvda_pct,
        alternatives_pct=alternatives_pct, alternatives_sigma=alternatives_sigma,
        high_growth_pct=hg_pct, high_growth_sigma=hg_sigma,
        fi_step=fi_step,
    )
    weights = _renormalise(
        nvda_pct=nvda_pct, fi_pct=fi_pct, alternatives_pct=alternatives_pct,
        high_growth_pct=hg_pct,
    )
    # The high-growth sleeve is a FIXED strategic weight (like NVDA / Alternatives),
    # so pin it THROUGH the authored-override renorm — otherwise it is treated as a
    # non-overridden sleeve and drifts off its target when other sleeves are pinned
    # (e.g. a 5% sleeve crept to 5.15% alongside the v63 NVDA/core/growth overrides).
    effective_overrides = dict(authored_overrides or {})
    if hg_pct > 0 and HIGH_GROWTH_LABEL not in effective_overrides:
        effective_overrides[HIGH_GROWTH_LABEL] = round(weights[HIGH_GROWTH_LABEL], 2)
    weights = _apply_authored_overrides(weights, effective_overrides)

    classes: list[AllocationClass] = []
    for s in _EQUITY_SLEEVES:
        classes.append(
            AllocationClass(
                label=s.label,
                target_pct=round(weights[s.label], 2),
                sigma_class=s.sigma_class,
                snapshot_category=s.snapshot_category,
                agreement=s.agreement,
                rationale=s.rationale,
                dissent=s.dissent,
                instruments=s.instruments,
            )
        )
    if alternatives_pct > 0:
        classes.append(
            AllocationClass(
                label=_ALTERNATIVES_LABEL,
                target_pct=round(weights[_ALTERNATIVES_LABEL], 2),
                sigma_class=_ALTERNATIVES_SIGMA_CLASS,
                snapshot_category=_ALTERNATIVES_SNAPSHOT_CATEGORY,
                agreement="team-sourced",
                rationale=alternatives_sleeve.rationale_md,
                dissent="; ".join(alternatives_sleeve.violations),
                instruments=tuple(
                    _candidate_to_instrument(c) for c in alternatives_sleeve.instruments
                ),
            )
        )
    if hg_pct > 0 and HIGH_GROWTH_LABEL in weights:
        classes.append(
            AllocationClass(
                label=HIGH_GROWTH_LABEL,
                target_pct=round(weights[HIGH_GROWTH_LABEL], 2),
                sigma_class=HIGH_GROWTH_SIGMA_CLASS,
                snapshot_category=HIGH_GROWTH_SNAPSHOT_CATEGORY,
                agreement="team-sourced",
                rationale=(
                    "Permanent high-growth / high-potential 'moonshot' sleeve — the x10 "
                    "ASYMMETRY sleeve (binding mandate): names that can plausibly 10x in "
                    "5-10 years on cap-math (current market cap vs addressable outcome; "
                    "favors sub-~$20-30B earlier-stage; >$50B needs an extraordinary "
                    "written justification), sourced GLOBALLY (US/EU/ISR/anywhere) and "
                    "deliberately NOT estate-gated. Accepted per-name loss is 100%, so "
                    "defensibility never boosts rank; rank and deploy fill order are "
                    "asymmetry-first (upside-asymmetry x plausibility — the instrument "
                    "weights ARE the asymmetry rank). Safe maybe-2x compounders belong "
                    "in the core/growth sleeves, not here. Held as a fixed strategic "
                    "weight; its sourced sigma feeds the FI solver so the book's "
                    "covariance sigma stays on the anchor."
                ),
                dissent="",
                instruments=tuple(high_growth_instruments or ()),
            )
        )
    classes.append(
        AllocationClass(
            label=_NVDA_SLEEVE.label,
            target_pct=round(weights[_NVDA_SLEEVE.label], 2),
            sigma_class=_NVDA_SLEEVE.sigma_class,
            snapshot_category=_NVDA_SLEEVE.snapshot_category,
            agreement=_NVDA_SLEEVE.agreement,
            rationale=_NVDA_SLEEVE.rationale,
            dissent=_NVDA_SLEEVE.dissent,
            instruments=_NVDA_SLEEVE.instruments,
        )
    )
    cash_pct = round(weights["Cash & T-bills (incl. ILS tranche)"], 2)
    bonds_pct = round(weights["Short-duration IG bonds"], 2)
    classes.append(AllocationClass(**{**_FI_CASH.__dict__, "target_pct": cash_pct}))
    classes.append(AllocationClass(**{**_FI_BONDS.__dict__, "target_pct": bonds_pct}))

    # Derive the reported fi_pct / nvda_pct from the post-override weights so the
    # TargetAllocation summary fields stay consistent with the class target_pcts.
    reported_fi_pct = round(cash_pct + bonds_pct, 2)
    reported_nvda_pct = round(weights[_NVDA_SLEEVE.label], 2)
    reported_alternatives_pct = (
        round(weights[_ALTERNATIVES_LABEL], 2) if alternatives_pct > 0 else 0.0
    )

    # Report blended sigma on the SAME unrounded weights the FI solver certified
    # against the anchor — computing it from the 2dp-rounded class target_pcts
    # instead lets per-class rounding accumulate and read fractionally OVER the
    # anchor (e.g. 0.1801 > 0.18), contradicting "FI is derived TO the anchor".
    blended = _blended_sigma_for(
        weights,
        alt_label=_ALTERNATIVES_LABEL if alternatives_pct > 0 else None,
        alt_sigma=alternatives_sigma if alternatives_pct > 0 else None,
        hg_label=HIGH_GROWTH_LABEL if hg_pct > 0 else None,
        hg_sigma=hg_sigma if hg_pct > 0 else None,
    )
    alts_clause = (
        f"a {reported_alternatives_pct:.1f}% team-sourced Alternatives sleeve (σ {alternatives_sigma:.3f}), "
        if alternatives_pct > 0
        else "no Alternatives sleeve (team sized it to 0%), "
    )
    overall = (
        f"Reconciled target for the deployable book at the end of the 2-year "
        f"deconcentration. Total equity ~{100 - reported_fi_pct - reported_alternatives_pct:.0f}% (return "
        f"engine + income/quality core + international + a min-vol damper), NVDA "
        f"{reported_nvda_pct:.0f}% direct (cap-derived so total plan look-through "
        f"clears the 13% concentration cap), {alts_clause}FI/cash {reported_fi_pct:.1f}% "
        f"DERIVED as the minimum weight at which the COVARIANCE-blended sigma "
        f"{blended:.4f} sits at or under the {anchor_sigma} anchor (solver floors/discretization land it slightly below). The anchor is phase-aware: "
        f"in accumulation (salary covers expenses, no withdrawals) it is the 0.18 "
        f"diversified steady-state, sized to a low single-digit FI; it glides down "
        f"to rebuild FI toward ~15% as actual retirement nears."
    )
    residual = (
        "FI sizing — derived to the phase anchor via the covariance blend (NVDA fixed, "
        "70/30 cash/short-IG). Caveats: correlation tiers are documented strategic "
        "long-run estimates (an adversarial reviewer can reconcile them in sigma_glidepath), "
        "and the MC holds mu_real constant regardless of FI (sees the volatility benefit, "
        "not the return drag). | Strategic-NVDA: panel lenses spanned 10-13 direct; "
        "the target is CAP-DERIVED by Argosy (8 direct ⇒ ~11.5% plan look-through "
        "< the 13% cap; the cap clears up to ~9.5 direct — 8 holds a deliberate "
        "~1.5pp drift buffer since look-through weights drift; 12 breaches), "
        "re-validated every synthesis. | FX hedge not fully neutralised at portfolio level — even with "
        "International 12 + the ILS cash tranche, most of the book stays USD-correlated. "
        "| Implementation: deploy NEW NVDA-proceeds cash into the target classes; do NOT "
        "force-sell appreciated non-NVDA sleeves (avoids fresh CGT)."
    )
    return TargetAllocation(
        classes=classes,
        blended_sigma=round(blended, 4),
        anchor_sigma=anchor_sigma,
        fi_pct=reported_fi_pct,
        nvda_pct=reported_nvda_pct,
        cash_pct=cash_pct,
        bonds_pct=bonds_pct,
        overall_rationale=overall,
        residual_disagreements=residual,
        deployable_nis=deployable_nis,
    )


# --- Redistribution schedule (the Q1..Q8 transformation) ---------------------
@dataclass(frozen=True)
class RedistributionWaypoint:
    label: str
    quarter: int            # 1..N
    target_date: date       # first-of-quarter date
    pct: float              # composition % at this quarter
    snapshot_category: str | None = None  # B1/H5: explicit glidepath anchor


@dataclass(frozen=True)
class RedistributionSchedule:
    today_composition: dict[str, float]
    end_target: dict[str, float]
    quarters: int
    start: date
    waypoints: list[RedistributionWaypoint] = field(default_factory=list)


def _add_months(start: date, months: int) -> date:
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(start.day, 28)
    return date(year, month, day)


def build_redistribution_schedule(
    *,
    today_composition: dict[str, float],
    target: TargetAllocation,
    start: date,
    quarters: int = 8,
) -> RedistributionSchedule:
    """Linearly transform today's full-book composition into the target over
    ``quarters`` quarters. NVDA tapers from today toward its 12% cap; every
    other class glides from today toward its target. Each intermediate quarter's
    composition sums to 100 by construction (a convex blend of two mixes that
    each sum to 100), so the chart's stacked bands stay coherent.

    The optimizer's chosen sell-down is a 2-year, equal-annual-tranche taper, so
    a linear quarterly glide is faithful to that cadence (front-loaded only in
    the sense of the 2-year-vs-5-year horizon choice the optimizer already made).
    """
    end_target = {c.label: c.target_pct for c in target.classes}
    label_to_cat = {c.label: c.snapshot_category for c in target.classes}
    labels = list(dict.fromkeys(list(today_composition) + list(end_target)))
    waypoints: list[RedistributionWaypoint] = []
    n = max(1, quarters)
    for q in range(1, n + 1):
        frac = q / n
        qdate = _add_months(start, 3 * q)
        for label in labels:
            t0 = float(today_composition.get(label, 0.0))
            t1 = float(end_target.get(label, 0.0))
            waypoints.append(
                RedistributionWaypoint(
                    label=label,
                    quarter=q,
                    target_date=qdate,
                    pct=round(t0 + (t1 - t0) * frac, 4),
                    snapshot_category=label_to_cat.get(label),
                )
            )
    return RedistributionSchedule(
        today_composition=dict(today_composition),
        end_target=end_target,
        quarters=n,
        start=start,
        waypoints=waypoints,
    )


def to_waypoint_targets(
    schedule: RedistributionSchedule,
    *,
    stated_at: date,
) -> list[SynthTarget]:
    """Emit one ``pct_of_portfolio`` SynthTarget per (class, quarter) so the plan
    literally carries the Q1..Q8 schedule and ``allocation_glidepath`` renders the
    staged transformation. Rationale is stamped on the FINAL-quarter waypoint of
    each class (the end-state weight) so the chart label carries the why."""
    end_labels = set(schedule.end_target)
    out: list[SynthTarget] = []
    for w in schedule.waypoints:
        is_final = w.quarter == schedule.quarters
        rationale = ""
        if is_final and w.label in end_labels:
            rationale = f"End-state target {w.pct:.1f}% of the deployable book."
        out.append(
            SynthTarget(
                label=w.label,
                value=w.pct,
                unit="pct_of_portfolio",
                stated_at=stated_at,
                revisit_after=w.target_date,
                rationale=rationale,
                source_section="allocation_redistribution",
                snapshot_category=w.snapshot_category,  # B1/H5: explicit anchor
            )
        )
    return out


def to_synth_targets(
    alloc: TargetAllocation,
    *,
    stated_at: date,
    revisit_after: date,
) -> list[SynthTarget]:
    """End-state target per class (single waypoint). The quarterly transition
    waypoints are layered on by the redistribution-schedule builder."""
    return [
        SynthTarget(
            label=c.label,
            value=c.target_pct,
            unit="pct_of_portfolio",
            stated_at=stated_at,
            revisit_after=revisit_after,
            rationale=c.rationale,
            source_section="allocation_target",
            snapshot_category=c.snapshot_category,  # B1/H5: explicit anchor
        )
        for c in alloc.classes
    ]


__all__ = [
    "AllocationClass",
    "TargetAllocation",
    "NVDA_TARGET_PCT",
    "CASH_FRAC_OF_FI",
    "GLOBAL_QUALITY_GROWTH_LABEL",
    "US_GROWTH_LEGACY_LABEL",
    "SLEEVE_LABEL_ALIASES",
    "normalize_sleeve_label",
    "normalize_override_labels",
    "build_target_allocation",
    "derive_fi_weight",
    "to_synth_targets",
    "_apply_authored_overrides",
]
