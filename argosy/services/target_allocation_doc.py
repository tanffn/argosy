"""The canonical, instrument-level, time-varying target-allocation document.

This is the single structured object every surface reads. It is authored by the
deterministic ``allocation_plan`` engine (not an LLM), persisted on the plan
version, and projected — never recomputed — by ``/plan``, ``/portfolio`` and
``/retirement``. Three properties make it the source of truth:

- **instrument-level** — each class names its tickers (``instruments``),
- **canonical** — engine-authored with the panel's agreement/dissent recorded,
- **time-varying** — a quarterly ``glide`` from today's book to the target.

See ``docs/design/SDD.md`` section 20 (the allocation model) and the realignment
roadmap. T1.1 defines the schema; ``build_target_allocation_doc`` (T1.3) fills it.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session


class AllocationInstrument(BaseModel):
    """A named holding within an asset class (e.g. ``VOO`` inside the core sleeve)."""

    symbol: str
    role: Literal["primary", "alt", "hold", "exit"]
    weight_within_class_pct: float  # sums to 100 within its class
    rationale: str = ""


class AllocationClassDoc(BaseModel):
    """One asset class: its target weight, its instruments, and the panel's notes."""

    label: str  # "US broad-market core"
    snapshot_category: str  # "Core Equity" — the exact snapshot-anchor key
    sigma_class: str
    target_pct: float  # % of the FULL tradeable book (classes sum to ~100)
    instruments: list[AllocationInstrument]
    agreement: str = ""
    rationale: str = ""
    dissent: str = ""


class GlideWaypoint(BaseModel):
    """The target composition at one quarter on the transition path."""

    quarter: int
    date: date
    composition_pct_by_class: dict[str, float]  # sums to 100 each quarter


class TargetAllocationDoc(BaseModel):
    """The canonical plan-level allocation: classes + their instruments + the glide."""

    schema_version: int = 1
    basis: str = "full tradeable book"
    anchor_sigma: float
    blended_sigma: float
    nvda_cap_pct: float  # the 13% ceiling
    fi_pct: float  # derived
    provenance: str
    classes: list[AllocationClassDoc]
    glide: list[GlideWaypoint]  # today -> target over N quarters


# --- Deriving TODAY's full-tradeable-book composition (the glide's t0) --------
# Snapshot category (normalized/lowercased, as _categories_from_snapshot emits)
# -> engine class label. 'defensive' and 'individual stocks' are special-cased
# in derive_full_book_today_composition (split / redeploy band).
_SNAPSHOT_CAT_TO_LABEL: dict[str, str] = {
    "core equity": "US broad-market core",
    "dividend": "Dividend-quality income",
    "international": "International developed (ex-US)",
    "growth": "US growth tilt (ex-NVDA)",
    "cash": "Cash & T-bills (incl. ILS tranche)",
    "alternative": "Real assets (REIT/TIPS)",
}

# Today's non-NVDA single stocks have no target sleeve (the engine holds only
# NVDA as a single name). They are an honest, distinct band that the glide
# redeploys to 0 — NOT mislabeled into the growth sleeve. The per-ticker
# keep/trim decision (e.g. keep GOOG) is an instrument-level transition concern.
OTHER_SINGLES_LABEL = "Individual Stocks (non-NVDA, to redeploy)"

_NVDA_LABEL = "Strategic single-stock (NVDA)"


def derive_full_book_today_composition(
    *,
    nvda_tradeable_pct: float,
    ex_nvda_categories: dict[str, float],
    low_vol_target: float,
    bonds_target: float,
) -> dict[str, float]:
    """Today's composition on the FULL tradeable book basis, keyed by engine label.

    The settled basis (codex danger-full-access verified against the live DB):
    NVDA's weight is ``nvda_tradeable_pct`` (from the concentration report, NOT the
    snapshot's 'Individual Stocks' row, which is the OTHER singles). The ex-NVDA
    snapshot categories (each a % of the ex-NVDA book, summing to ~100) are scaled
    by ``(100 - nvda_tradeable_pct)/100`` so the whole book sums to ~100.

    Special cases:
      * ``defensive`` splits between US low-vol + short IG bonds proportional to
        their engine target weights (the glidepath's shared-category rule);
      * ``individual stocks`` (the non-NVDA singles) becomes the redeploy band
        ``OTHER_SINGLES_LABEL`` (glides to 0 — no target sleeve);
      * unknown categories are kept under their raw key so the sum is preserved.
    """
    mult = (100.0 - nvda_tradeable_pct) / 100.0
    comp: dict[str, float] = {_NVDA_LABEL: nvda_tradeable_pct}
    for cat, pct in ex_nvda_categories.items():
        scaled = pct * mult
        if cat == "defensive":
            denom = low_vol_target + bonds_target
            if denom <= 0:
                comp["US low-volatility equity"] = scaled
                continue
            comp["US low-volatility equity"] = scaled * low_vol_target / denom
            comp["Short-duration IG bonds"] = scaled * bonds_target / denom
        elif cat == "individual stocks":
            comp[OTHER_SINGLES_LABEL] = comp.get(OTHER_SINGLES_LABEL, 0.0) + scaled
        else:
            label = _SNAPSHOT_CAT_TO_LABEL.get(cat, cat)
            comp[label] = comp.get(label, 0.0) + scaled
    return comp


def build_target_allocation_doc(
    *,
    today: date,
    today_composition: dict[str, float],
    quarters: int = 8,
    anchor_sigma: float | None = None,
) -> TargetAllocationDoc:
    """Assemble the canonical doc from the deterministic ``allocation_plan`` engine.

    The engine OWNS the numbers: this maps its instrument-level classes into the
    doc and builds the quarterly ``glide`` from ``build_redistribution_schedule``.
    ``today_composition`` is the current FULL tradeable book (incl. NVDA, summing
    to ~100) — passed in rather than read here so this builder is pure and the
    basis-sensitive snapshot derivation lives in (and is verified by) the wiring
    layer. Imports of ``allocation_plan`` are local to break the import cycle
    (``allocation_plan`` imports ``AllocationInstrument`` from this module).
    """
    from argosy.services.allocation_plan import (
        build_redistribution_schedule,
        build_target_allocation,
    )
    from argosy.services.retirement.scenario_mc import (
        DEFAULT_NVDA_CAP_PCT,
        SIGMA_DIVERSIFIED,
    )

    anchor = SIGMA_DIVERSIFIED if anchor_sigma is None else anchor_sigma
    alloc = build_target_allocation(anchor_sigma=anchor)

    classes = [
        AllocationClassDoc(
            label=c.label,
            snapshot_category=c.snapshot_category,
            sigma_class=c.sigma_class,
            target_pct=c.target_pct,
            instruments=list(c.instruments),
            agreement=c.agreement,
            rationale=c.rationale,
            dissent=c.dissent,
        )
        for c in alloc.classes
    ]

    schedule = build_redistribution_schedule(
        today_composition=today_composition,
        target=alloc,
        start=today,
        quarters=quarters,
    )
    # q0 = TODAY's actual composition (the chart's left anchor) so the glidepath
    # opens on the same reality /portfolio's current pie shows, then q1..qN are
    # the staged transformation. Without q0 the chart would start already-moved.
    glide: list[GlideWaypoint] = [
        GlideWaypoint(
            quarter=0,
            date=today,
            composition_pct_by_class=dict(today_composition),
        )
    ]
    for q in range(1, schedule.quarters + 1):
        wps = [w for w in schedule.waypoints if w.quarter == q]
        glide.append(
            GlideWaypoint(
                quarter=q,
                date=wps[0].target_date,
                composition_pct_by_class={w.label: w.pct for w in wps},
            )
        )

    return TargetAllocationDoc(
        anchor_sigma=alloc.anchor_sigma,
        blended_sigma=alloc.blended_sigma,
        nvda_cap_pct=DEFAULT_NVDA_CAP_PCT * 100.0,
        fi_pct=alloc.fi_pct,
        provenance=alloc.provenance,
        classes=classes,
        glide=glide,
    )


def load_full_book_today_composition(
    db: "Session", user_id: str, decision_run_id: int
) -> dict[str, float] | None:
    """Resolve TODAY's full-tradeable-book composition from the DB, or ``None``.

    NVDA's weight comes from the plan's concentration report
    (``concentration.nvda_current_pct`` — the SAME canonical source the NVDA
    projection uses, NOT the snapshot's other-singles row), and the ex-NVDA
    categories from the latest portfolio snapshot. ``None`` when either is
    missing (the doc is additive — the caller skips writing it rather than
    persisting a guess). ``include_canonical_ages`` is left at its default
    False (the concentration keys must not enter the dual-track re-entrant hop).
    """
    from argosy.services.allocation_glidepath import (
        _categories_from_snapshot,
        _latest_portfolio_snapshot,
    )
    from argosy.services.allocation_plan import build_target_allocation
    from argosy.services.plan_numeric_resolver import resolve_plan_numbers

    nums = resolve_plan_numbers(db, user_id=user_id, decision_run_id=decision_run_id)
    cur_rv = nums.get("concentration.nvda_current_pct")
    if cur_rv is None or getattr(cur_rv, "status", None) != "resolved":
        return None
    if cur_rv.value is None or float(cur_rv.value) <= 0:
        return None
    nvda_pct = float(cur_rv.value) * 100.0

    ex_nvda = _categories_from_snapshot(_latest_portfolio_snapshot(db, user_id))
    if not ex_nvda:
        return None

    by_label = {c.label: c.target_pct for c in build_target_allocation().classes}
    return derive_full_book_today_composition(
        nvda_tradeable_pct=nvda_pct,
        ex_nvda_categories=ex_nvda,
        low_vol_target=by_label.get("US low-volatility equity", 0.0),
        bonds_target=by_label.get("Short-duration IG bonds", 0.0),
    )


def _deconcentration_quarters(
    db: "Session", user_id: str, today: date, *, default_quarters: int = 8
) -> int:
    """The doc's deconcentration glide tapers over the OPTIMIZER-chosen horizon
    (T4.2): ``optimize_deconcentration`` sweeps H∈{1..5}y and picks the H that
    minimizes the typical-regime drawdown age (tie-break: lower total CGT) — the
    SAME horizon its σ-glide uses for the MC. The displayed transition then spans
    that horizon: ``quarters = H × 4``. Best-effort: the optimizer is a heavy MC
    sweep, so any failure / no-feasible-horizon falls back to ``default_quarters``
    (never blocks the doc build, never fabricates a horizon)."""
    try:
        from argosy.services.retirement.deconcentration_optimizer import (
            optimize_deconcentration,
        )

        plan = optimize_deconcentration(session=db, user_id=user_id, today=today)
        h = plan.chosen_horizon_years
        if h and int(h) > 0:
            return int(h) * 4
    except Exception:  # noqa: BLE001 — never block the doc build on the optimizer
        pass
    return default_quarters


def build_plan_target_allocation_doc(
    db: "Session", user_id: str, decision_run_id: int, today: date
) -> TargetAllocationDoc | None:
    """The DB-aware entry T1.5/backfill call: derive today's composition then
    build the canonical doc, or ``None`` when the composition can't be derived.

    The deconcentration glide spans the optimizer-chosen sell-down horizon
    (T4.2, :func:`_deconcentration_quarters`) instead of a fixed 2-year taper."""
    comp = load_full_book_today_composition(db, user_id, decision_run_id)
    if comp is None:
        return None
    quarters = _deconcentration_quarters(db, user_id, today)
    return build_target_allocation_doc(
        today=today, today_composition=comp, quarters=quarters
    )


def doc_equity_bond_cash(doc: TargetAllocationDoc) -> tuple[float, float, float]:
    """Aggregate the doc's class targets into (equity, bond, cash) percentages
    by sigma_class. The retirement /glide-path projects THIS — the plan's actual
    target allocation (equity-heavy by design; the deconcentration transition is
    the /plan glidepath, and σ-de-risking for solvency is the MC's job) — rather
    than a textbook age-decline curve. Everything that isn't bonds/cash is a risk
    asset → equity."""
    equity = bond = cash = 0.0
    for c in doc.classes:
        if c.sigma_class == "bonds":
            bond += c.target_pct
        elif c.sigma_class == "cash":
            cash += c.target_pct
        else:
            equity += c.target_pct
    return equity, bond, cash


def load_plan_target_allocation(pv: object) -> TargetAllocationDoc | None:
    """Read the canonical doc off a plan version, or ``None`` — never raises.

    Surfaces call this to project the plan; a missing/empty/corrupt column must
    degrade to "no canonical doc" (fall back to the legacy path) rather than
    break the surface. ``pv`` is any object with a ``target_allocation_json``
    attribute (the ``PlanVersion`` row)."""
    raw = getattr(pv, "target_allocation_json", None)
    if not raw:
        return None
    try:
        return TargetAllocationDoc.model_validate_json(raw)
    except (ValueError, TypeError):
        return None
