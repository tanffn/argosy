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

  * **Strategic single-stock (NVDA)** is held at ``NVDA_TARGET_PCT`` — Ariel's
    explicit sign-off within the optimizer's 10-13% band (the optimizer cap is
    ``DEFAULT_NVDA_CAP_PCT`` = 13%, the MIN-of-four-constraints figure). Held
    just below the hard cap so post-transformation drift doesn't immediately
    breach the do-not-re-concentrate ceiling.

  * **Fixed-income / cash** weight is DERIVED, not asserted. The panel's
    reconciled estimate (16%) was contested (8/13/24/29 across lenses) and, with
    the corrected sigma-classes, actually blends ABOVE the plan's steady-state
    anchor ``SIGMA_DIVERSIFIED`` (=0.18) — i.e. it is NOT self-consistent with
    the age-47 headline the deconcentration optimizer certifies at exactly that
    sigma. We therefore size FI as the MINIMUM weight (NVDA held fixed, the six
    other sleeves kept at their agreed ratios, FI split cash/short-IG bonds by
    ``CASH_FRAC_OF_FI``) at which the allocation's engine-blended sigma sits on
    the 0.18 anchor. That restores self-consistency: at the derived FI the
    typical-regime Monte-Carlo earliest-safe drawdown age is 47 with a solvency
    margin (P@95 ~= 91%), versus the panel-16 mix which slips to age 48
    (P@95 ~= 89.5%).

Model caveats carried in the rationale (NOT silently swallowed): the engine
blends class sigmas LINEARLY (no diversification/correlation credit), so the
0.18 target is conservative-leaning vs a covariance model; and the MC holds
mu_real constant regardless of the FI weight, so it sees FI's volatility benefit
but not its return drag. Both are documented so an adversarial reviewer can
reconcile the derived weight.
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
    map_glidepath_class_to_sigma_class,
    sigma_from_composition,
)
from argosy.services.target_allocation_doc import AllocationInstrument

# --- The two specially-handled weights (auditable, not magic). ---------------
# Ariel's sign-off, inside the optimizer's 10-13% band; cap is the canonical
# DEFAULT_NVDA_CAP_PCT (0.13). Held just below the cap for drift headroom.
NVDA_TARGET_PCT = 12.0
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
# holds the supplied sleeve as a fixed policy weight subtracted before the six
# equity sleeves are renormalised; FI remains the sigma-solver and absorbs the
# sleeve's SOURCED sigma to keep the blended sigma on the anchor.
_ALTERNATIVES_LABEL = "Alternatives"
_ALTERNATIVES_SIGMA_CLASS = "alternatives"
_ALTERNATIVES_SNAPSHOT_CATEGORY = "Alternative"


# --- Agreed equity/alts sleeves (the panel's mix, NVDA + FI handled above). --
# ``ratio`` is the panel's agreed RELATIVE weight among the non-NVDA, non-FI
# sleeves; absolute weights are filled by renormalisation once FI is derived.
@dataclass(frozen=True)
class _PanelSleeve:
    label: str            # engine-safe label (maps 1:1 onto a sigma-class)
    ratio: float          # agreed relative weight among the 6 equity/alts sleeves
    sigma_class: str
    snapshot_category: str  # portfolio-snapshot category for today's anchor
    agreement: str
    rationale: str
    dissent: str = ""
    instruments: tuple[AllocationInstrument, ...] = ()


_EQUITY_SLEEVES: tuple[_PanelSleeve, ...] = (
    _PanelSleeve(
        label="US broad-market core",
        ratio=28.0,
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
        ratio=19.0,
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
        ratio=12.0,
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
            "12 because ex-US developed equity hedges USD-CONCENTRATION but NOT the "
            "named shekel-appreciation risk (it is EUR/JPY/GBP, not NIS), and the "
            "engine models its sigma (0.20) above US equity (0.18)."
        ),
        dissent=(
            "Direction (lift from ~2%) is the strongest cross-lens agreement; "
            "magnitude 7-18 contested. NO lens's international weight hedges the "
            "named ILS risk — that lives in the FI sleeve's earmarked ILS tranche."
        ),
    ),
    _PanelSleeve(
        label="US growth tilt (ex-NVDA)",
        ratio=6.0,
        instruments=(
            AllocationInstrument(
                symbol="R1GR", role="primary", weight_within_class_pct=100.0, domicile="IE",
                rationale=(
                    "US large-cap growth via the Irish UCITS R1GR (iShares Russell 1000 "
                    "Growth, Acc, ~0.18% TER), NOT US-domiciled SCHG. Closest UCITS growth "
                    "twin; note it is NOT ex-NVDA (the index still holds NVDA), a small "
                    "overlap accepted given the sleeve's 6% size. UCITS domicile avoids "
                    "US-situs estate exposure; cite estate_tax_nonresidents.md."
                ),
            ),
        ),
        sigma_class="us_equity",
        snapshot_category="Growth",
        agreement="moderate",
        rationale=(
            "Lean SCHG-style sleeve preserving compounding upside, kept small "
            "because NVDA already supplies concentrated high-beta tech exposure at "
            "the 12% cap; stacking correlated tech beta re-adds the factor risk the "
            "deconcentration is meant to shed. Label deliberately avoids the "
            "'nvda' substring trap so it maps to us_equity, not the 0.45 single-stock."
        ),
        dissent="Magnitude 4-9; latent split on whether it is redundant with US-core.",
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
        ratio=1.0,
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
        f"Held at {NVDA_TARGET_PCT:.0f}% — Ariel's sign-off just below the "
        "optimizer's 13% cap (the MIN-of-four-constraints ceiling: sequence / "
        "tail-loss / risk-contribution / tax-liquidity). Retains essentially all "
        "optimizer-sanctioned conviction upside + low-basis CGT deferral while "
        "reserving ~1pp headroom below the hard cap so normal drift does not "
        "immediately breach the do-not-re-concentrate rule. Pair with a "
        "trim-on-breach band. NVDA's ~0.45 single-name sigma remains the dominant "
        "variance contributor even at 12% — the accepted residual idiosyncratic tail."
    ),
    dissent=(
        "13 (long-hold/Boglehead/risk) vs 10 (capital-preservation); Ariel chose 12. "
        "~NIS 87k of deployable book per point — conviction-upside vs single-name tail."
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
) -> float:
    """Holdings-weighted blended sigma. When an Alternatives sleeve is present its
    sigma is the SOURCED ``alt_sigma`` (pinned by label), not the fixed class
    constant — so a gold-only sleeve blends at 0.16 and an 80/20 gold/BTC sleeve
    at 0.268, exactly as the team sourced it."""
    if alt_label is None or alt_sigma is None:
        return sigma_from_composition(weights)
    total = sum(max(0.0, v) for v in weights.values())
    if total <= 0:
        return sigma_from_composition(weights)
    sigma = 0.0
    for label, pct in weights.items():
        if pct <= 0:
            continue
        weight = pct / total
        if label == alt_label:
            cls_sigma = alt_sigma
        else:
            cls_sigma = _SIGMA_BY_CLASS.get(
                map_glidepath_class_to_sigma_class(label), 0.20
            )
        sigma += weight * cls_sigma
    return round(sigma, 4)


def _renormalise(
    *, nvda_pct: float, fi_pct: float, alternatives_pct: float = 0.0
) -> dict[str, float]:
    """Hold NVDA + FI + Alternatives fixed; distribute the rest among the six
    equity sleeves at their agreed ratios; split FI into cash + short-IG bonds.

    The team-sourced Alternatives weight is subtracted off the book BEFORE the
    six equity sleeves are sized (it displaces the non-NVDA risky sleeves pro
    rata), so a larger sleeve shrinks equity and — via its sigma — indirectly
    forces more FI to hold the anchor. ``alternatives_pct=0`` adds no class."""
    other_total = 100.0 - nvda_pct - fi_pct - alternatives_pct
    ratio_sum = sum(s.ratio for s in _EQUITY_SLEEVES)
    weights: dict[str, float] = {
        s.label: s.ratio / ratio_sum * other_total for s in _EQUITY_SLEEVES
    }
    weights[_NVDA_SLEEVE.label] = nvda_pct
    if alternatives_pct > 0:
        weights[_ALTERNATIVES_LABEL] = alternatives_pct
    weights["Cash & T-bills (incl. ILS tranche)"] = fi_pct * CASH_FRAC_OF_FI
    weights["Short-duration IG bonds"] = fi_pct * (1.0 - CASH_FRAC_OF_FI)
    return weights


def derive_fi_weight(
    *,
    anchor_sigma: float = SIGMA_DIVERSIFIED,
    nvda_pct: float = NVDA_TARGET_PCT,
    alternatives_pct: float = 0.0,
    alternatives_sigma: float = 0.0,
    fi_step: float = 0.01,
    fi_lo: float = 8.0,
    fi_hi: float = 35.0,
) -> float:
    """Minimum FI weight (in ``fi_step`` increments) at which the allocation's
    engine-blended sigma sits at/under the steady-state anchor — the sigma the
    optimizer used to certify the earliest-safe age. Self-consistency, not a
    chosen constant. A team-sourced Alternatives sleeve is held at
    ``alternatives_pct`` and its SOURCED ``alternatives_sigma`` is what FI must
    offset (a higher sourced sigma forces more FI)."""
    alt_label = _ALTERNATIVES_LABEL if alternatives_pct > 0 else None
    alt_sigma = alternatives_sigma if alternatives_pct > 0 else None
    fi = fi_lo
    while fi <= fi_hi:
        weights = _renormalise(
            nvda_pct=nvda_pct, fi_pct=fi, alternatives_pct=alternatives_pct
        )
        if _blended_sigma_for(weights, alt_label=alt_label, alt_sigma=alt_sigma) <= (
            anchor_sigma + 1e-9
        ):
            return round(fi, 2)
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
        "FI was the panel's most-contested class (8/13/24/29). The reconciled 16% "
        "blends ABOVE the 0.18 anchor (P@95 at age 47 ~= 89.5% → age slips to 48); "
        "the weight is DERIVED up to the anchor instead so age 47 stays consistent."
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


def build_target_allocation(
    *,
    anchor_sigma: float = SIGMA_DIVERSIFIED,
    nvda_pct: float = NVDA_TARGET_PCT,
    alternatives_sleeve: AlternativesSleeveDecision | None = None,
    fi_step: float = 0.01,
    deployable_nis: float | None = None,
) -> TargetAllocation:
    """Assemble the canonical target allocation with the FI weight derived to the
    steady-state sigma anchor. Pure: no DB, no clock.

    ``alternatives_sleeve`` is the TEAM's verified, sized decision. When ``None``
    (or a 0% decision) there is NO alternatives class and the book is the
    six-equity + NVDA + FI baseline. When supplied, its ``target_pct`` is held as
    a fixed policy weight (subtracted before equity renorm) and its SOURCED
    ``sleeve_sigma`` flows into the FI solver."""
    alternatives_pct = (
        alternatives_sleeve.target_pct
        if (alternatives_sleeve and alternatives_sleeve.target_pct > 0)
        else 0.0
    )
    alternatives_sigma = (
        alternatives_sleeve.sleeve_sigma if alternatives_pct > 0 else 0.0
    )

    fi_pct = derive_fi_weight(
        anchor_sigma=anchor_sigma, nvda_pct=nvda_pct,
        alternatives_pct=alternatives_pct, alternatives_sigma=alternatives_sigma,
        fi_step=fi_step,
    )
    weights = _renormalise(
        nvda_pct=nvda_pct, fi_pct=fi_pct, alternatives_pct=alternatives_pct
    )

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

    blended = _blended_sigma_for(
        {c.label: c.target_pct for c in classes},
        alt_label=_ALTERNATIVES_LABEL if alternatives_pct > 0 else None,
        alt_sigma=alternatives_sigma if alternatives_pct > 0 else None,
    )
    alts_clause = (
        f"a {alternatives_pct:.1f}% team-sourced Alternatives sleeve (σ {alternatives_sigma:.3f}), "
        if alternatives_pct > 0
        else "no Alternatives sleeve (team sized it to 0%), "
    )
    overall = (
        f"Reconciled target for the deployable book at the end of the 2-year "
        f"deconcentration. Total equity ~{100 - fi_pct - alternatives_pct:.0f}% (return "
        f"engine + income/quality core + international + a min-vol damper), NVDA "
        f"{nvda_pct:.0f}% just under the 13% cap, {alts_clause}FI/cash {fi_pct:.1f}% "
        f"DERIVED to the {anchor_sigma} steady-state sigma anchor (blended sigma "
        f"{blended:.4f}). FI is derived rather than asserted "
        f"because the panel's contested 16% estimate blends above the anchor and would push "
        f"the earliest-safe age to 48; sized to the anchor it holds at 47."
    )
    residual = (
        "FI sizing — derived to the 0.18 anchor (NVDA fixed, 70/30 cash/short-IG). "
        "Caveats: the engine blends class sigmas linearly (no correlation credit → "
        "conservative-leaning) and holds mu_real constant regardless of FI (sees the "
        "volatility benefit, not the return drag). | Strategic-NVDA 10-13 band, Ariel "
        "chose 12. | FX hedge not fully neutralised at portfolio level — even with "
        "International 12 + the ILS cash tranche, most of the book stays USD-correlated. "
        "| Implementation: deploy NEW NVDA-proceeds cash into the target classes; do NOT "
        "force-sell appreciated non-NVDA sleeves (avoids fresh CGT)."
    )
    return TargetAllocation(
        classes=classes,
        blended_sigma=round(blended, 4),
        anchor_sigma=anchor_sigma,
        fi_pct=round(fi_pct, 2),
        nvda_pct=nvda_pct,
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
    "build_target_allocation",
    "derive_fi_weight",
    "to_synth_targets",
]
