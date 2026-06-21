"""Overview — plain-language plan-explainer assembler.

Pure, testable builder for the new ``/api/overview`` surface. It turns the
canonical plan (resolver + fact_registry) into a 7-chapter plain-language story.

Design doctrine (see ``docs/superpowers/specs/2026-06-21-overview-plan-explainer-design.md``):

  * **One source of numbers.** Every magnitude in a chapter headline is a
    ``{{fact:KEY}}`` token rendered centrally by :mod:`argosy.quality.fact_registry`
    against :func:`argosy.services.plan_numeric_resolver.resolve_plan_numbers`.
    No hand-typed financial magnitudes ever live in a template — the static
    template constants below all pass ``find_unauthorized_numbers(...) == []``.
  * **Degrade, don't crash.** ``render_placeholders(strict=False)`` leaves a
    pending fact's token visible and the chapter is flagged ``degraded`` rather
    than throwing. Missing viz inputs likewise degrade a chapter, never raise.
  * **Explains; does not execute.** ``your_move`` chips deep-link to /proposals;
    the action checklist lives there.
  * **Does NOT reopen B1.** Chapter 5 (forward RSU income) is a READ-ONLY
    display of the deterministic vest projection — never wired into
    fi_crossing / savings. Scalar FI path only.

This module has no FastAPI / Pydantic dependency; it returns plain dataclasses
so it is unit-testable in isolation. The route wraps these into the response
models.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from argosy.quality.fact_registry import (
    FACT_DISPLAY,
    find_unauthorized_numbers,
    format_fact,
    render_fact,
    render_placeholders,
)

logger = logging.getLogger(__name__)

# Capital-track retention applied to forward RSU vest income (per the B1
# handover: Section-102 capital track nets ~0.68 after the blended rate). This
# is a DISPLAY-ONLY factor for chapter 5; it is NOT a financial magnitude in any
# prose template, so it is not policed by the fact gate.
_RSU_CAPITAL_TRACK_RETENTION = 0.68

# FI forward-series buffer: project a couple of years past the crossing so the
# hero chart shows the line clearing the target.
_FI_SERIES_BUFFER_YEARS = 2
_FI_SERIES_MIN_POINTS = 6


# ---------------------------------------------------------------------------
# Plain dataclasses (route maps these onto the Pydantic response models).
# ---------------------------------------------------------------------------
@dataclass
class FactRefData:
    key: str
    value: float | None
    unit: str
    status: str
    display: str | None
    source_locator: str
    confidence: str | None


@dataclass
class VizPayloadData:
    kind: str
    data: dict


@dataclass
class YourMoveData:
    label: str
    href: str


@dataclass
class OverviewChapterData:
    id: str
    title: str
    eyebrow: str
    headline: str
    degraded: bool
    facts: list[FactRefData]
    viz: VizPayloadData
    drill_label: str
    drill_href: str
    your_move: YourMoveData | None = None


@dataclass
class OverviewActionsBannerData:
    open_count: int
    href: str


@dataclass
class OverviewModel:
    available: bool
    reason: str | None
    plan_version_id: int | None
    decision_run_id: int | None
    as_of: str | None
    chapters: list[OverviewChapterData]
    actions_banner: OverviewActionsBannerData


# ---------------------------------------------------------------------------
# Fact helpers — everything flows through fact_registry so policy stays central.
# ---------------------------------------------------------------------------
def _fact_ref(resolved, key: str) -> FactRefData:
    """Build a :class:`FactRefData` for one resolver key, with the rendered
    display (or ``None`` if pending / unregistered)."""
    rv = resolved.get(key)
    display: str | None
    try:
        display = render_fact(key, resolved)
    except Exception:  # PlaceholderError or unregistered key
        display = None
    return FactRefData(
        key=key,
        value=getattr(rv, "value", None),
        unit=getattr(rv, "unit", ""),
        status=getattr(rv, "status", "pending"),
        display=display,
        source_locator=getattr(rv, "source_locator", key),
        confidence=getattr(rv, "confidence", None),
    )


def _is_resolved(resolved, key: str) -> bool:
    rv = resolved.get(key)
    return (
        getattr(rv, "status", None) == "resolved"
        and getattr(rv, "value", None) is not None
    )


def _value(resolved, key: str) -> float | None:
    rv = resolved.get(key)
    if getattr(rv, "status", None) == "resolved":
        return getattr(rv, "value", None)
    return None


def render_signed_abs(key: str, resolved) -> str:
    """Render the ABSOLUTE magnitude of a signed resolver value, keeping all
    rendering through ``fact_registry.format_fact`` so display policy stays
    central. The sign/direction word ("short"/"ahead") is supplied by the
    Python-selected template variant, never by this rendered magnitude.

    Raises (caught by the caller) when the value is pending — so a pending
    margin degrades the chapter rather than fabricating a magnitude.
    """
    rv = resolved.get(key)
    if (
        getattr(rv, "status", None) != "resolved"
        or getattr(rv, "value", None) is None
    ):
        raise ValueError(f"signed fact {key!r} not resolved")
    display = FACT_DISPLAY.get(key)
    if display is None:
        raise ValueError(f"signed fact {key!r} not in registry")
    return format_fact(abs(float(rv.value)), getattr(rv, "unit", ""), display=display)


def _render_headline(template: str, resolved, *, leak_check_template: str | None = None) -> tuple[str, bool]:
    """Render a template's ``{{fact:}}`` tokens (degrade, don't crash).

    Returns ``(rendered, ok)`` where ``ok`` is False if any token was left
    unresolved (pending) OR if a hand-typed magnitude leaked into the template.

    The leak guard runs on ``leak_check_template`` (default: ``template``) —
    which MUST be the ORIGINAL token-bearing template, NOT one with
    fact_registry-rendered magnitudes already spliced into ``{SHORT_AMOUNT}`` /
    ``{FIRST_YEAR_NET}`` markers. Splicing rendered ₪-strings in and then leak-
    checking would false-positive on our own central-render output. The marker
    templates are validated clean at import via ``_assert_clean_templates``.
    """
    rendered = render_placeholders(template, resolved, strict=False)
    pending = "{{fact:" in rendered
    leaked = bool(find_unauthorized_numbers(leak_check_template or template))
    if leaked:
        logger.warning(
            "overview template carries a hand-typed magnitude: %r",
            leak_check_template or template,
        )
    return rendered, (not pending and not leaked)


# ---------------------------------------------------------------------------
# Headline templates — every magnitude is a {{fact:}} token (no literals).
# Verified at import time below via _assert_clean_templates().
# ---------------------------------------------------------------------------
_T_FI_REACHED = (
    "You've reached it. You have {{fact:portfolio.liquid_net_worth_nis}} you "
    "can actually live on versus the {{fact:retirement.fi_total_capital_nis}} "
    "you need to never work again — you're past the line."
)
_T_FI_SHORT = (
    "Almost. You need {{fact:retirement.fi_total_capital_nis}} to live off "
    "forever without working; you have {{fact:portfolio.liquid_net_worth_nis}} "
    "you can actually spend — so you're {SHORT_AMOUNT} short, and normal growth "
    "should close that by {{fact:retirement.fi_crossing_year}}."
)
_T_LIQUIDITY = (
    "You're worth {{fact:portfolio.total_net_worth_incl_residence_nis}} all in, "
    "but only the {{fact:portfolio.liquid_net_worth_nis}} that's liquid counts "
    "toward retiring — your home equity is real wealth you can't live off."
)
_T_ALLOCATION = (
    "Here's how your money is split today versus the target mix the plan sets — "
    "and why."
)
_T_NVDA = (
    "NVDA is {{fact:concentration.nvda_current_pct}} of your money; the plan "
    "trims it toward {{fact:concentration.nvda_target_pct}}. Only about "
    "{{fact:concentration.nvda_eligible_now_sh}} shares are sellable at the low "
    "tax rate right now — the rest is worth waiting for."
)
_T_RSU = (
    "Your NVDA grants keep vesting — about {FIRST_YEAR_NET} worth this year "
    "(held to the capital-gains track), tapering toward {LAST_YEAR_NET} by "
    "{LAST_YEAR} as older grants run off."
)
_T_RSU_DEGRADED = (
    "Your NVDA grants keep paying out over the next few years as older grants "
    "vest and newer ones ramp."
)
_T_PHASES = (
    "Your spending isn't flat — kids, a wedding, a car every few years. Here's "
    "the road of what life costs over time."
)
_T_DUAL_TRACK = (
    "Retire and spend normally at about {{fact:retirement.earliest_safe_age}}, "
    "or keep every cent of principal safe and it's about "
    "{{fact:retirement.preservation_age}}. Same plan — it's your call on the risk."
)

# Templates whose ONLY magnitudes are {{fact:}} tokens (the runtime-substituted
# {SHORT_AMOUNT}/{FIRST_YEAR_NET}/... markers are themselves rendered via
# fact_registry helpers before substitution, so they aren't hand-typed either).
_STATIC_TEMPLATES = [
    _T_FI_REACHED,
    _T_LIQUIDITY,
    _T_ALLOCATION,
    _T_NVDA,
    _T_RSU_DEGRADED,
    _T_PHASES,
    _T_DUAL_TRACK,
]


def _assert_clean_templates() -> None:
    """Import-time guard: no static template carries a hand-typed magnitude.
    The runtime-marker templates (_T_FI_SHORT, _T_RSU) are validated separately
    because their non-fact markers ({SHORT_AMOUNT}/...) are placeholders that get
    filled with fact_registry-rendered strings, not literals."""
    for t in _STATIC_TEMPLATES:
        v = find_unauthorized_numbers(t)
        assert not v, f"template carries hand-typed magnitude {v!r}: {t!r}"
    # For the marker templates, blank the markers then assert clean.
    for t in (_T_FI_SHORT, _T_RSU):
        bare = (
            t.replace("{SHORT_AMOUNT}", "")
            .replace("{FIRST_YEAR_NET}", "")
            .replace("{LAST_YEAR_NET}", "")
            .replace("{LAST_YEAR}", "")
        )
        v = find_unauthorized_numbers(bare)
        assert not v, f"marker template carries hand-typed magnitude {v!r}: {t!r}"


_assert_clean_templates()


# ---------------------------------------------------------------------------
# Chapter builders.
# ---------------------------------------------------------------------------
def _chapter_fi(resolved, *, base_year: int | None = None) -> OverviewChapterData:
    keys = [
        "retirement.fi_total_capital_nis",
        "portfolio.liquid_net_worth_nis",
        "retirement.fi_margin_signed_nis",
        "retirement.fi_crossing_year",
    ]
    facts = [_fact_ref(resolved, k) for k in keys]

    margin = _value(resolved, "retirement.fi_margin_signed_nis")
    degraded = False

    if margin is not None and margin >= 0:
        headline, ok = _render_headline(_T_FI_REACHED, resolved)
        degraded = not ok
    else:
        # "short" variant — render the absolute magnitude via fact_registry.
        try:
            short_amount = render_signed_abs(
                "retirement.fi_margin_signed_nis", resolved
            )
        except Exception:
            short_amount = "{{fact:retirement.fi_margin_signed_nis}}"
            degraded = True
        template = _T_FI_SHORT.replace("{SHORT_AMOUNT}", short_amount)
        leak_tmpl = _T_FI_SHORT.replace("{SHORT_AMOUNT}", "")
        headline, ok = _render_headline(template, resolved, leak_check_template=leak_tmpl)
        degraded = degraded or not ok

    viz_data, viz_ok = _build_fi_series(resolved, base_year=base_year)
    if not viz_ok:
        degraded = True

    return OverviewChapterData(
        id="fi",
        title="Can you stop working yet?",
        eyebrow="CAN YOU STOP WORKING YET?",
        headline=headline,
        degraded=degraded,
        facts=facts,
        viz=VizPayloadData(kind="fi_crossing", data=viz_data),
        drill_label="See the full retirement detail",
        drill_href="/retirement",
        your_move=None,
    )


def _build_fi_series(resolved, *, base_year: int | None = None) -> tuple[dict, bool]:
    """Deterministic forward projection of liquid wealth (scalar path only — B1
    untouched). Returns ``(data, ok)``; ``ok`` is False if any input is pending.

    ``base_year`` anchors the series x-axis to the plan/snapshot year (so the
    chart can't visually contradict the resolver-derived crossing year); falls
    back to the wall-clock year when not supplied."""
    from argosy.services.fi_crossing import _future_value

    liquid = _value(resolved, "portfolio.liquid_net_worth_nis")
    fi_total = _value(resolved, "retirement.fi_total_capital_nis")
    ret = _value(resolved, "retirement.return_assumption_pct")
    savings = _value(resolved, "savings.annual_net_nis")
    crossing_year = _value(resolved, "retirement.fi_crossing_year")

    data: dict = {
        "progress_pct": None,
        "target_nis": fi_total,
        "series": [],
        "crossing_year": int(crossing_year) if crossing_year is not None else None,
    }

    if any(v is None for v in (liquid, fi_total, ret, savings)):
        return data, False
    if not (-1.0 < ret < 1.0):
        logger.warning("overview FI series: return %r not a fraction", ret)
        return data, False

    current_year = base_year or datetime.now().year
    if crossing_year is not None:
        span = max(int(crossing_year) - current_year + _FI_SERIES_BUFFER_YEARS, 0)
    else:
        span = 0
    n_points = max(span + 1, _FI_SERIES_MIN_POINTS)

    series = []
    for n in range(n_points):
        projected = _future_value(liquid, ret, savings, n)
        series.append(
            {"year": current_year + n, "projected_liquid_nis": projected}
        )
    data["series"] = series
    data["progress_pct"] = (liquid / fi_total * 100.0) if fi_total else None
    return data, True


def _chapter_liquidity(resolved, *, illiquid_nis: float | None) -> OverviewChapterData:
    keys = [
        "portfolio.total_net_worth_incl_residence_nis",
        "portfolio.liquid_net_worth_nis",
    ]
    facts = [_fact_ref(resolved, k) for k in keys]
    headline, ok = _render_headline(_T_LIQUIDITY, resolved)
    degraded = not ok

    total = _value(resolved, "portfolio.total_net_worth_incl_residence_nis")
    liquid = _value(resolved, "portfolio.liquid_net_worth_nis")
    if illiquid_nis is None and total is not None and liquid is not None:
        illiquid_nis = max(total - liquid, 0.0)
    viz_data = {
        "liquid_nis": liquid,
        "illiquid_nis": illiquid_nis,
        "total_nis": total,
    }
    if liquid is None or total is None:
        degraded = True

    return OverviewChapterData(
        id="liquidity",
        title="What's actually spendable",
        eyebrow="WHAT'S ACTUALLY SPENDABLE",
        headline=headline,
        degraded=degraded,
        facts=facts,
        viz=VizPayloadData(kind="liquid_split", data=viz_data),
        drill_label="See your full portfolio",
        drill_href="/portfolio",
        your_move=None,
    )


def _chapter_allocation(alloc_rows: list[dict], *, source_locator: str) -> OverviewChapterData:
    # Allocation prose carries NO magnitudes (per-class target keys are not in
    # the fact registry); numbers live only in viz `data`. No fact rendering.
    headline, ok = _render_headline(_T_ALLOCATION, resolved=_EmptyResolved())
    degraded = (not ok) or (not alloc_rows)
    return OverviewChapterData(
        id="allocation",
        title="Where your money sits vs the plan",
        eyebrow="WHERE YOUR MONEY SITS VS THE PLAN",
        headline=headline,
        degraded=degraded,
        facts=[],
        viz=VizPayloadData(kind="alloc_vs_target", data={"rows": alloc_rows}),
        drill_label="See the full plan",
        drill_href="/plan",
        your_move=None,
    )


def _chapter_nvda(resolved, *, held_sh: float | None = None) -> OverviewChapterData:
    keys = [
        "concentration.nvda_current_pct",
        "concentration.nvda_target_pct",
        "concentration.nvda_cap_pct",
        "concentration.nvda_eligible_now_sh",
        "concentration.nvda_sell_sh",
        "concentration.nvda_target_sh",
    ]
    facts = [_fact_ref(resolved, k) for k in keys]
    headline, ok = _render_headline(_T_NVDA, resolved)
    degraded = not ok

    eligible = _value(resolved, "concentration.nvda_eligible_now_sh")
    sell = _value(resolved, "concentration.nvda_sell_sh")
    target_sh = _value(resolved, "concentration.nvda_target_sh")
    cur_pct = _value(resolved, "concentration.nvda_current_pct")
    # held_sh = TOTAL NVDA shares held (from the snapshot), so the viz can split
    # the holding into "sellable now at the capital-track rate" (eligible) vs the
    # rest still inside the 2-year holding period — the real "worth waiting" story
    # (not the meaningless sell_sh − eligible remainder).

    viz_data = {
        "current_pct": cur_pct,
        "target_pct": _value(resolved, "concentration.nvda_target_pct"),
        "cap_pct": _value(resolved, "concentration.nvda_cap_pct"),
        "eligible_now_sh": eligible,
        "sell_sh": sell,
        "target_sh": target_sh,
        "held_sh": held_sh,
    }

    # your_move: sell amount capped to what's eligible now.
    your_move: YourMoveData | None = None
    if sell is not None and eligible is not None:
        capped = min(sell, eligible)
        if capped > 0:
            label_amount = format_fact(capped, "sh", display="sh")
            your_move = YourMoveData(
                label=f"Sell ~{label_amount} shares now", href="/proposals"
            )
    elif eligible is not None and eligible > 0:
        label_amount = format_fact(eligible, "sh", display="sh")
        your_move = YourMoveData(
            label=f"Sell ~{label_amount} shares now", href="/proposals"
        )

    return OverviewChapterData(
        id="nvda",
        title="Winding down your NVDA bet",
        eyebrow="WINDING DOWN YOUR NVDA BET",
        headline=headline,
        degraded=degraded,
        facts=facts,
        viz=VizPayloadData(kind="nvda_winddown", data=viz_data),
        drill_label="See your full portfolio",
        drill_href="/portfolio",
        your_move=your_move,
    )


def _chapter_rsu(years: list[dict], *, degraded_reason: str | None) -> OverviewChapterData:
    """READ-ONLY forward RSU income chapter. ``years`` = [{year, net_nis}, ...].
    Never wired into any calc. Magnitudes rendered via fact_registry.format_fact."""
    if degraded_reason or not years:
        return OverviewChapterData(
            id="rsu_income",
            title="The income still coming in",
            eyebrow="THE INCOME STILL COMING IN",
            headline=render_placeholders(_T_RSU_DEGRADED, _EmptyResolved(), strict=False),
            degraded=True,
            facts=[],
            viz=VizPayloadData(kind="rsu_forward", data={"years": years}),
            drill_label="See your full portfolio",
            drill_href="/portfolio",
            your_move=None,
        )

    first = years[0]
    last = years[-1]
    first_net = format_fact(first["net_nis"], "nis", display="nis")
    last_net = format_fact(last["net_nis"], "nis", display="nis")
    last_year = format_fact(last["year"], "year", display="year")
    template = (
        _T_RSU.replace("{FIRST_YEAR_NET}", first_net)
        .replace("{LAST_YEAR_NET}", last_net)
        .replace("{LAST_YEAR}", last_year)
    )
    leak_tmpl = (
        _T_RSU.replace("{FIRST_YEAR_NET}", "")
        .replace("{LAST_YEAR_NET}", "")
        .replace("{LAST_YEAR}", "")
    )
    headline, ok = _render_headline(template, _EmptyResolved(), leak_check_template=leak_tmpl)

    # RSU facts are display-only synthetic refs (source: project_quarterly_vests).
    facts = [
        FactRefData(
            key=f"rsu.forward_net_nis.{y['year']}",
            value=y["net_nis"],
            unit="nis",
            status="resolved",
            display=format_fact(y["net_nis"], "nis", display="nis"),
            source_locator="rsu_savings.project_quarterly_vests (display only)",
            confidence=None,
        )
        for y in years
    ]

    return OverviewChapterData(
        id="rsu_income",
        title="The income still coming in",
        eyebrow="THE INCOME STILL COMING IN",
        headline=headline,
        degraded=not ok,
        facts=facts,
        viz=VizPayloadData(kind="rsu_forward", data={"years": years}),
        drill_label="See your full portfolio",
        drill_href="/portfolio",
        your_move=None,
    )


def _chapter_phases(phase_rows: list[dict]) -> OverviewChapterData:
    headline, ok = _render_headline(_T_PHASES, _EmptyResolved())
    return OverviewChapterData(
        id="phases",
        title="Life phases ahead",
        eyebrow="LIFE PHASES AHEAD",
        headline=headline,
        degraded=(not ok) or (not phase_rows),
        facts=[],
        viz=VizPayloadData(kind="phase_timeline", data={"phases": phase_rows}),
        drill_label="See the full retirement detail",
        drill_href="/retirement",
        your_move=None,
    )


def _chapter_dual_track(resolved) -> OverviewChapterData:
    keys = ["retirement.earliest_safe_age", "retirement.preservation_age"]
    facts = [_fact_ref(resolved, k) for k in keys]
    headline, ok = _render_headline(_T_DUAL_TRACK, resolved)
    earliest = _value(resolved, "retirement.earliest_safe_age")
    preservation = _value(resolved, "retirement.preservation_age")
    viz_data = {
        "earliest_safe_age": earliest,
        "preservation_age": preservation,
    }
    degraded = (not ok) or earliest is None or preservation is None
    return OverviewChapterData(
        id="dual_track",
        title="When can you retire — two honest answers",
        eyebrow="WHEN CAN YOU RETIRE",
        headline=headline,
        degraded=degraded,
        facts=facts,
        viz=VizPayloadData(kind="dual_track_age", data=viz_data),
        drill_label="See the full retirement detail",
        drill_href="/retirement",
        your_move=None,
    )


class _EmptyResolved:
    """Tiny stand-in for ResolvedPlanNumbers for templates that carry no facts —
    every key is "pending" so any stray {{fact:}} would be left visible (and the
    template would be flagged), but our no-fact templates have none."""

    def get(self, key: str):
        return None


# ---------------------------------------------------------------------------
# Data-source helpers (snapshot / RSU / phases / actions).
# ---------------------------------------------------------------------------
def _norm_pct(v) -> float | None:
    """Return a FRACTION (0-1). A value > 1.5 is treated as percent-points
    (snapshot rows may carry either convention); else assumed already a fraction."""
    f = _as_float(v)
    if f is None:
        return None
    return f / 100.0 if f > 1.5 else f


def _alloc_rows(snapshot, plan) -> tuple[list[dict], str]:
    """Build alloc current-vs-target rows from the SAME canonical computation the
    /portfolio surface uses (``build_allocation_breakdown``: live holdings grouped
    into the plan's class taxonomy, with each class's canonical
    ``TargetAllocationDoc`` target). This guarantees the Overview's allocation is
    byte-identical to /portfolio — one source, no drift (codex blocker #1). Falls
    back to the snapshot's own current/target rows only if the canonical path is
    unavailable. ``current_pct``/``target_pct`` are passed through unchanged so
    the two surfaces render the same numbers.
    """
    # Canonical path — mirror argosy/api/routes/portfolio.py::allocation-breakdown.
    try:
        from argosy.services.allocation_breakdown import build_allocation_breakdown
        from argosy.services.target_allocation_doc import load_plan_target_allocation
        from argosy.services.portfolio_snapshot_store import row_to_snapshot

        if snapshot is not None and plan is not None:
            doc = load_plan_target_allocation(plan)
            snap = row_to_snapshot(snapshot)
            cats = build_allocation_breakdown(snap, doc, exclude_nvda=False)
            rows = [
                {
                    "label": c.label,
                    "current_pct": c.current_pct,
                    "target_pct": c.target_pct,
                }
                for c in cats
            ]
            if rows:
                return rows, "allocation_breakdown (canonical plan target + live holdings)"
    except Exception as exc:  # degrade to the snapshot rows below, never throw
        logger.warning("overview allocation breakdown unavailable, falling back: %s", exc)

    # Fallback: the snapshot's own current/target pair (internally consistent).
    import json

    if snapshot is None:
        return [], "no portfolio snapshot and no plan target allocation"
    try:
        raw = json.loads(snapshot.allocations_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return [], "snapshot allocations_json unparseable"
    rows = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        label = r.get("category") or r.get("label")
        if not label:
            continue
        rows.append(
            {
                "label": str(label),
                "current_pct": _norm_pct(r.get("current_pct")),
                "target_pct": _norm_pct(r.get("target_pct")),
            }
        )
    return rows, "portfolio_snapshots.allocations_json (fallback — no canonical breakdown)"


def _as_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _nvda_held_shares(snapshot) -> float | None:
    """Total NVDA shares held, from the latest snapshot's positions. None if
    unavailable — keeps the NVDA 'worth waiting' split honest rather than faking
    a denominator."""
    import json

    if snapshot is None:
        return None
    try:
        positions = json.loads(snapshot.positions_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    total = 0.0
    found = False
    for p in positions:
        if not isinstance(p, dict):
            continue
        if (p.get("symbol") or "").upper() == "NVDA":
            sh = _as_float(p.get("shares"))
            if sh is not None:
                total += sh
                found = True
    return total if found else None


def _event_year(ev) -> int | None:
    """Extract a calendar year from a vest event's date — accepts date/datetime
    objects and ISO-ish strings; returns None for anything unparseable."""
    d = ev.get("date") if isinstance(ev, dict) else None
    if d is None:
        return None
    yr = getattr(d, "year", None)  # date / datetime
    if isinstance(yr, int):
        return yr
    s = str(d)
    if len(s) >= 4 and s[:4].isdigit():
        return int(s[:4])
    return None


def _rsu_forward_years(
    session, user_id: str, resolved, snapshot, *, base_year: int | None = None
) -> tuple[list[dict], str | None]:
    """READ-ONLY: aggregate the deterministic vest projection to per-year net
    NIS for the next ~5 years. Returns ``(years, degraded_reason)``. Never raises
    — any load/coercion failure degrades to ``([], reason)``.

    net_nis_per_year = year_shares * nvda_price_usd * fx_usd_nis * 0.68.
    NEVER wired into fi_crossing / savings.
    """
    import json

    try:
        from argosy.services.wealth_dashboard import _load_user_context_yaml
        from argosy.services.rsu_savings import project_quarterly_vests
    except Exception as exc:  # pragma: no cover - import guard
        return [], f"rsu modules unavailable: {exc}"

    # Whole load/coercion path guarded — degrade, never throw (codex blocker #2).
    try:
        user_ctx = _load_user_context_yaml(session, user_id)
    except Exception as exc:
        return [], f"user context load failed: {exc}"
    if not isinstance(user_ctx, dict):
        return [], "user context unavailable"
    sched = user_ctx.get("rsu_vest_schedule")
    if not isinstance(sched, dict):
        return [], "no rsu_vest_schedule in user context"
    active_grants = sched.get("active_grants")
    portal_vests = sched.get("quarterly_vests")
    if not active_grants:
        return [], "no active_grants in rsu_vest_schedule"

    current_year = base_year or datetime.now().year
    try:
        events = project_quarterly_vests(
            active_grants,
            portal_vests,
            horizon_start_year=current_year,
            horizon_years=5,
        )
    except Exception as exc:
        return [], f"vest projection failed: {exc}"

    # NVDA price from snapshot positions; fx from resolver then snapshot.
    nvda_price_usd: float | None = None
    if snapshot is not None:
        try:
            positions = json.loads(snapshot.positions_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            positions = []
        for p in positions:
            if isinstance(p, dict) and (p.get("symbol") or "").upper() == "NVDA":
                nvda_price_usd = _as_float(p.get("current_price"))
                if nvda_price_usd:
                    break

    fx = _value(resolved, "fx.usd_nis")
    if fx is None and snapshot is not None:
        fx = _as_float(getattr(snapshot, "fx_usd_nis", None))

    if not nvda_price_usd or not fx:
        return [], "missing NVDA price or USD/NIS fx for RSU valuation"

    by_year: dict[int, float] = {}
    for ev in events:
        yr = _event_year(ev)
        if yr is None:
            continue
        shares = _as_float(ev.get("shares")) or 0.0
        by_year[yr] = by_year.get(yr, 0.0) + shares

    years: list[dict] = []
    for yr in sorted(by_year):
        net_nis = by_year[yr] * nvda_price_usd * fx * _RSU_CAPITAL_TRACK_RETENTION
        years.append({"year": yr, "net_nis": net_nis})

    if not years:
        return [], "no forward vests within horizon"
    return years, None


def _phase_rows(resolved, session, user_id: str) -> list[dict]:
    """Build phase_timeline rows from the canonical phase-expense curve.

    annual_nis = spend.annual_t12_nis * monthly_multiplier. If the baseline spend
    is pending, annual_nis is left None (chapter still shows the phase shape).
    """
    try:
        from argosy.services.retirement.phase_expenses import build_phase_expense_curve
    except Exception:  # pragma: no cover
        return []
    try:
        phases = build_phase_expense_curve()
    except Exception:
        return []
    baseline = _value(resolved, "spend.annual_t12_nis")
    rows: list[dict] = []
    for ph in phases:
        mult = getattr(getattr(ph, "monthly_multiplier", None), "value", None)
        annual = (
            baseline * float(mult)
            if (baseline is not None and mult is not None)
            else None
        )
        rows.append(
            {
                "label": ph.label,
                "start": ph.start_age,
                "end": ph.end_age,
                "start_age": ph.start_age,
                "end_age": ph.end_age,
                "annual_nis": annual,
            }
        )
    return rows


def _open_actions_count(session, user_id: str) -> int:
    """Count of open user-facing actions (overdue + today + upcoming) via the
    existing /api/plan action-items aggregation. Degrades to 0 on any error."""
    try:
        from argosy.api.routes.plan import get_action_items

        resp = get_action_items(user_id=user_id, window_days=14, db=session)
        return int(
            (resp.overdue_count or 0)
            + (resp.today_count or 0)
            + (resp.upcoming_count or 0)
        )
    except Exception as exc:  # pragma: no cover - degrade, never throw
        logger.warning("overview actions banner degraded: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Top-level entry point.
# ---------------------------------------------------------------------------
def build_overview(session, *, user_id: str) -> OverviewModel:
    """Assemble the Overview model for ``user_id``. Pure-ish (reads DB, no writes),
    never throws on missing/pending data — degrades each chapter instead."""
    from argosy.state.queries import get_current_plan

    plan = get_current_plan(session, user_id)
    if plan is None:
        return _unavailable("No current plan — accept a plan to see the Overview.")
    if plan.decision_run_id is None:
        return _unavailable(
            "Current plan has no decision run — numbers can't be resolved yet.",
            plan_version_id=plan.id,
        )

    from argosy.services.plan_numeric_resolver import resolve_plan_numbers

    resolved = resolve_plan_numbers(
        session,
        user_id=user_id,
        decision_run_id=plan.decision_run_id,
        include_canonical_ages=True,
    )

    # Snapshot (chapters 2,3,4,5).
    snapshot = None
    illiquid_nis = None
    try:
        from argosy.services.wealth_dashboard import _latest_snapshot

        snapshot = _latest_snapshot(session, user_id)
    except Exception as exc:  # pragma: no cover
        logger.warning("overview snapshot load failed: %s", exc)

    # Anchor year for forward projections: the snapshot's year (so the FI series
    # x-axis and the RSU horizon align with the plan's as-of), else wall-clock.
    base_year: int | None = None
    snap_date = getattr(snapshot, "snapshot_date", None) if snapshot is not None else None
    snap_year = getattr(snap_date, "year", None)
    if isinstance(snap_year, int):
        base_year = snap_year
    elif snap_date:
        s = str(snap_date)
        if len(s) >= 4 and s[:4].isdigit():
            base_year = int(s[:4])

    alloc_rows, alloc_src = _alloc_rows(snapshot, plan)
    rsu_years, rsu_reason = _rsu_forward_years(
        session, user_id, resolved, snapshot, base_year=base_year
    )
    phase_rows = _phase_rows(resolved, session, user_id)

    chapters = [
        _chapter_fi(resolved, base_year=base_year),
        _chapter_liquidity(resolved, illiquid_nis=illiquid_nis),
        _chapter_allocation(alloc_rows, source_locator=alloc_src),
        _chapter_nvda(resolved, held_sh=_nvda_held_shares(snapshot)),
        _chapter_rsu(rsu_years, degraded_reason=rsu_reason),
        _chapter_phases(phase_rows),
        _chapter_dual_track(resolved),
    ]

    open_count = _open_actions_count(session, user_id)

    # as_of: prefer snapshot date, else plan accepted/imported timestamp.
    as_of: str | None = None
    if snapshot is not None and getattr(snapshot, "snapshot_date", None):
        as_of = str(snapshot.snapshot_date)
    elif getattr(plan, "accepted_at", None):
        as_of = plan.accepted_at.isoformat()
    elif getattr(plan, "imported_at", None):
        as_of = plan.imported_at.isoformat()

    return OverviewModel(
        available=True,
        reason=None,
        plan_version_id=plan.id,
        decision_run_id=plan.decision_run_id,
        as_of=as_of,
        chapters=chapters,
        actions_banner=OverviewActionsBannerData(open_count=open_count, href="/proposals"),
    )


def _unavailable(reason: str, *, plan_version_id: int | None = None) -> OverviewModel:
    return OverviewModel(
        available=False,
        reason=reason,
        plan_version_id=plan_version_id,
        decision_run_id=None,
        as_of=None,
        chapters=[],
        actions_banner=OverviewActionsBannerData(open_count=0, href="/proposals"),
    )
