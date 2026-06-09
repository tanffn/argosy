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

from argosy.agents.plan_synthesizer_types import SynthTarget
from argosy.services.retirement.scenario_mc import (
    DEFAULT_NVDA_CAP_PCT,
    SIGMA_DIVERSIFIED,
)
from argosy.services.sigma_glidepath import sigma_from_composition
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
                symbol="VOO", role="primary", weight_within_class_pct=100.0,
                rationale="Total US-market core (VOO); VTI an equivalent low-cost substitute.",
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
                symbol="SCHD", role="primary", weight_within_class_pct=100.0,
                rationale="Dividend-quality payers (SCHD); VIG a comparable substitute.",
            ),
        ),
        sigma_class="us_equity",
        snapshot_category="Dividend",
        agreement="moderate",
        rationale=(
            "SCHD/quality-payer sleeve turning the book into a cash-flow machine "
            "that funds roughly half the net bridge draw, so less principal is sold "
            "into drawdowns — the cleanest structural defense against sequence risk "
            "in the 20-year no-pension window and a direct match to the household's "
            "long-hold/dividend style."
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
                symbol="VEA", role="primary", weight_within_class_pct=100.0,
                rationale="Developed ex-US equity (VEA); VXUS a broader substitute that also adds EM.",
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
                symbol="SCHG", role="primary", weight_within_class_pct=100.0,
                rationale="US large-cap growth ex-single-name (SCHG); deliberately excludes NVDA.",
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
                symbol="USMV", role="primary", weight_within_class_pct=100.0,
                rationale="Min-volatility / quality-defensive US equity (USMV).",
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
                symbol="VNQ", role="primary", weight_within_class_pct=100.0,
                rationale="US REIT proxy (VNQ) for the token real-assets sleeve; a TIPS fund (SCHP) is the inflation-hedge alternative.",
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
            symbol="NVDA", role="primary", weight_within_class_pct=100.0,
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


def _blended_sigma_for(weights: dict[str, float]) -> float:
    return sigma_from_composition(weights)


def _renormalise(*, nvda_pct: float, fi_pct: float) -> dict[str, float]:
    """Hold NVDA + FI fixed; distribute the rest among the six equity/alts
    sleeves at their agreed ratios; split FI into cash + short-IG bonds."""
    other_total = 100.0 - nvda_pct - fi_pct
    ratio_sum = sum(s.ratio for s in _EQUITY_SLEEVES)
    weights: dict[str, float] = {
        s.label: s.ratio / ratio_sum * other_total for s in _EQUITY_SLEEVES
    }
    weights[_NVDA_SLEEVE.label] = nvda_pct
    weights["Cash & T-bills (incl. ILS tranche)"] = fi_pct * CASH_FRAC_OF_FI
    weights["Short-duration IG bonds"] = fi_pct * (1.0 - CASH_FRAC_OF_FI)
    return weights


def derive_fi_weight(
    *,
    anchor_sigma: float = SIGMA_DIVERSIFIED,
    nvda_pct: float = NVDA_TARGET_PCT,
    fi_step: float = 0.01,
    fi_lo: float = 8.0,
    fi_hi: float = 35.0,
) -> float:
    """Minimum FI weight (in ``fi_step`` increments) at which the allocation's
    engine-blended sigma sits at/under the steady-state anchor — the sigma the
    optimizer used to certify the earliest-safe age. Self-consistency, not a
    chosen constant."""
    fi = fi_lo
    while fi <= fi_hi:
        if _blended_sigma_for(_renormalise(nvda_pct=nvda_pct, fi_pct=fi)) <= anchor_sigma + 1e-9:
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
            symbol="SGOV", role="primary", weight_within_class_pct=100.0,
            rationale="0-3mo US T-bills (SGOV); the earmarked ILS short-makam hedge tranche is held within this sleeve.",
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
            symbol="VGSH", role="primary", weight_within_class_pct=100.0,
            rationale="Short-duration US Treasuries / IG (VGSH); SGOV an alternative.",
        ),
    ),
)


def build_target_allocation(
    *,
    anchor_sigma: float = SIGMA_DIVERSIFIED,
    nvda_pct: float = NVDA_TARGET_PCT,
    fi_step: float = 0.01,
    deployable_nis: float | None = None,
) -> TargetAllocation:
    """Assemble the canonical target allocation with the FI weight derived to
    the steady-state sigma anchor. Pure: no DB, no clock."""
    fi_pct = derive_fi_weight(anchor_sigma=anchor_sigma, nvda_pct=nvda_pct, fi_step=fi_step)
    weights = _renormalise(nvda_pct=nvda_pct, fi_pct=fi_pct)

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

    blended = _blended_sigma_for({c.label: c.target_pct for c in classes})
    overall = (
        f"Reconciled target for the deployable book at the end of the 2-year "
        f"deconcentration. Total equity ~{100 - fi_pct - 1:.0f}% (return engine + "
        f"income/quality core + international + a min-vol damper), NVDA {nvda_pct:.0f}% "
        f"just under the 13% cap, FI/cash {fi_pct:.1f}% DERIVED to the {anchor_sigma} "
        f"steady-state sigma anchor (blended sigma {blended:.4f}), and a ~1% real-asset "
        f"toehold. FI is derived rather than asserted because the panel's contested 16% "
        f"estimate blends above the anchor and would push the earliest-safe age to 48; "
        f"sized to the anchor it holds at 47."
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
