"""Stage-3 INPUTS — x10 / high-potential SLEEVE MANDATE for discovery candidates.

Time-machine backtest lesson (tmp/fleet_timemachine/, 2026-07): the long-hold
lens catches pre-momentum monsters WHEN it is handed the sleeve mandate
(bounded small position, accepted 100% per-name loss, cap-math asymmetry test,
mandatory exit discipline) — but production discovery adjudicated new names
with a generic stage-3 packet, so the fleet judged a moonshot candidate like a
core position (safety-first, initiation-sized) and killed exactly the names
the sleeve exists to hold.

This module closes that INPUTS gap for ``subject_type="discovery"`` stage-3
candidates, mirroring the ``estate_kb`` / ``position_context`` plumbing
pattern: deterministic inputs, never a judgment gate.

The mandate text is PLAN-OWNED, not hardcoded: it is read from the CURRENT
plan's high-growth/moonshot class (``sigma_class`` in the domicile-exempt
sleeve set — the same attribution key ``deep_decision._floor_scope`` uses),
whose ``rationale`` carries the binding x10-asymmetry mandate, and whose
instrument meta (weights = asymmetry rank, recorded ``exit_triggers``)
demonstrates the sleeve's conventions. Only when the plan carries no such
sleeve (or no rationale) does the standing ``X10_SLEEVE_MANDATE`` constant
travel as the fallback — the packet is never silently mandate-free.

The block also carries the sleeve's LIVE FUNDING GAP (current vs target, from
the same canonical ``build_allocation_breakdown`` attribution the /portfolio
card and the deployment-author packet use), so the fleet knows whether a new
qualifying name has sleeve budget to fill.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from argosy.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

    from argosy.services.target_allocation_doc import (
        AllocationClassDoc,
        TargetAllocationDoc,
    )

_log = get_logger("argosy.services.decision_funnel.sleeve_mandate")


def find_x10_sleeve_class(
    doc: TargetAllocationDoc | None,
) -> AllocationClassDoc | None:
    """The plan's x10 / high-potential sleeve class, keyed by ``sigma_class``
    membership in the domicile-exempt sleeve set (never by label or ticker),
    matching ``deep_decision._floor_scope`` attribution. ``None`` when the
    plan carries no such sleeve."""
    if doc is None:
        return None
    from argosy.services.target_allocation_doc import (
        _DOMICILE_EXEMPT_SIGMA_CLASSES,
    )

    for c in doc.classes:
        if c.sigma_class in _DOMICILE_EXEMPT_SIGMA_CLASSES:
            return c
    return None


def _load_current_plan_doc(
    session: Session, *, user_id: str
) -> TargetAllocationDoc | None:
    from sqlalchemy import select

    from argosy.services.target_allocation_doc import load_plan_target_allocation
    from argosy.state.models import PlanVersion

    pv = session.execute(
        select(PlanVersion).where(
            PlanVersion.user_id == user_id, PlanVersion.role == "current"
        )
    ).scalar_one_or_none()
    return load_plan_target_allocation(pv) if pv is not None else None


def _sizing_lines(cls: AllocationClassDoc) -> list[str]:
    """Per-name position bound DERIVED from the plan's own numbers (sleeve
    target x instrument weight-within-class), never a hardcoded band."""
    target = float(cls.target_pct or 0.0)
    weights = [
        float(i.weight_within_class_pct or 0.0)
        for i in cls.instruments
        if float(i.weight_within_class_pct or 0.0) > 0
    ]
    if target > 0 and weights:
        lo = target * min(weights) / 100.0
        hi = target * max(weights) / 100.0
        band = (
            f"~{lo:.1f}%-{hi:.1f}% of the tradeable book "
            f"(sleeve target {target:.1f}% x plan instrument weights "
            f"{min(weights):.0f}-{max(weights):.0f}% within the sleeve)"
        )
    elif target > 0:
        band = (
            f"a small bounded fraction of the {target:.1f}% sleeve target "
            "(the plan records no per-instrument weights yet)"
        )
    else:
        band = "a small bounded fraction of the sleeve"
    return [
        f"- POSITION IS BOUNDED: a single new sleeve name is {band}. "
        "Size it as a sleeve position — NEVER as a core initiation; the "
        "accepted per-name loss is 100% of that bounded slice, so "
        "defensibility/quality must not drive the verdict.",
    ]


def _exit_trigger_lines(cls: AllocationClassDoc) -> list[str]:
    n_with = sum(1 for i in cls.instruments if list(i.exit_triggers or []))
    return [
        "- EXIT TRIGGER REQUIRED: any green light (BUY) MUST state the "
        "concrete invalidation/exit trigger(s) for the thesis — the plan "
        "records ``exit_triggers`` per sleeve instrument "
        f"({n_with} of {len(cls.instruments)} current sleeve names carry "
        "recorded triggers); a BUY without an explicit exit trigger is "
        "INCOMPLETE and must not be issued.",
    ]


def _instrument_lines(cls: AllocationClassDoc) -> list[str]:
    if not cls.instruments:
        return []
    parts = []
    for i in cls.instruments:
        bits = f"{i.symbol} {float(i.weight_within_class_pct or 0.0):.0f}%"
        if list(i.exit_triggers or []):
            bits += " (exit triggers recorded)"
        parts.append(bits)
    return [
        "- CURRENT SLEEVE INSTRUMENTS (plan meta; weight = asymmetry rank): "
        + ", ".join(parts)
        + "."
    ]


def _funding_lines(
    session: Session,
    *,
    user_id: str,
    doc: TargetAllocationDoc,
    cls: AllocationClassDoc,
) -> list[str]:
    """The sleeve's live current-vs-target funding gap from the canonical
    breakdown (same attribution as /portfolio + the deployment-author packet).
    Degrades to an honest 'unavailable' line, never silently absent."""
    target = float(cls.target_pct or 0.0)
    try:
        from argosy.services.allocation_breakdown import build_allocation_breakdown
        from argosy.services.allocation_plan import normalize_sleeve_label
        from argosy.services.portfolio_snapshot_store import (
            get_latest_snapshot_row,
            row_to_snapshot,
        )

        snap_row = get_latest_snapshot_row(session, user_id)
        if snap_row is None:
            raise LookupError("no portfolio snapshot")
        rows = build_allocation_breakdown(row_to_snapshot(snap_row), doc)
        book_usd = sum(float(r.current_value_k or 0.0) for r in rows) * 1000.0
        _label = normalize_sleeve_label(cls.label)
        cur = next(
            (r for r in rows if normalize_sleeve_label(r.label) == _label), None
        )
        current_pct = float(cur.current_pct) if cur is not None else 0.0
        gap_pp = target - current_pct
        gap_usd = gap_pp / 100.0 * book_usd if book_usd > 0 else None
        gap_usd_str = f" (~${gap_usd:,.0f} of headroom)" if gap_usd else ""
        if gap_pp > 0.05:
            state = f"UNDER-FUNDED by ~{gap_pp:.1f}pp{gap_usd_str} — a new qualifying name is how this gap fills"
        elif gap_pp < -0.05:
            state = f"OVER target by ~{-gap_pp:.1f}pp — a new name must displace or wait for sleeve room"
        else:
            state = "at target — a new name must earn its slot vs current sleeve holdings"
        return [
            f"- SLEEVE FUNDING STATUS (latest snapshot vs plan target): "
            f"currently ~{current_pct:.1f}% of book vs {target:.1f}% target — "
            f"{state}.",
        ]
    except Exception:  # noqa: BLE001 — funding gap is enrichment, degrade honestly
        _log.exception("sleeve_mandate.funding_gap_failed", user_id=user_id)
        return [
            f"- SLEEVE FUNDING STATUS: live current-vs-target attribution "
            f"unavailable at packet time — treat the plan's {target:.1f}% "
            "sleeve target as the budget envelope.",
        ]


def build_sleeve_mandate_context(session: Session, *, user_id: str) -> str:
    """The full plan-owned x10 sleeve-mandate block for a DISCOVERY stage-3
    packet. Never raises; never returns an empty mandate (falls back to the
    standing ``X10_SLEEVE_MANDATE`` constant with the degradation stated)."""
    from argosy.services.high_potential_sleeve import X10_SLEEVE_MANDATE

    doc = None
    try:
        doc = _load_current_plan_doc(session, user_id=user_id)
    except Exception:  # noqa: BLE001 — plan lookup failure degrades to fallback
        _log.exception("sleeve_mandate.plan_load_failed", user_id=user_id)
    cls = find_x10_sleeve_class(doc)

    if cls is None or doc is None:
        return "\n".join([
            "X10 / HIGH-POTENTIAL SLEEVE MANDATE — DISCOVERY CANDIDATE. "
            "This candidate is adjudicated AGAINST the bounded moonshot "
            "sleeve, not the core allocation. (The current plan's sleeve "
            "definition was unavailable at packet time — the standing "
            "mandate below applies.)",
            "",
            X10_SLEEVE_MANDATE,
            "",
            "- POSITION IS BOUNDED: size as a small sleeve position with an "
            "accepted 100% per-name loss — never as a core initiation.",
            "- EXIT TRIGGER REQUIRED: any green light (BUY) MUST state the "
            "concrete invalidation/exit trigger(s); a BUY without one is "
            "INCOMPLETE and must not be issued.",
        ])

    mandate = (cls.rationale or "").strip() or X10_SLEEVE_MANDATE
    lines: list[str] = [
        f"X10 / HIGH-POTENTIAL SLEEVE MANDATE — DISCOVERY CANDIDATE. This "
        f"candidate is adjudicated AGAINST the plan's bounded sleeve class "
        f"'{cls.label}' (target {float(cls.target_pct or 0.0):.1f}% of the "
        f"tradeable book), NOT the core allocation. The binding mandate below "
        f"is the PLAN'S OWN sleeve definition:",
        "",
        mandate,
        "",
        "SLEEVE ADJUDICATION REQUIREMENTS (deterministic, from the plan's "
        "sleeve definition):",
        *_sizing_lines(cls),
        *_exit_trigger_lines(cls),
        *_funding_lines(session, user_id=user_id, doc=doc, cls=cls),
        *_instrument_lines(cls),
    ]
    return "\n".join(lines)


async def x10_sleeve_mandate_block(*, user_id: str) -> str:
    """Async wrapper for the stage-3 packet (mirrors ``position_context``)."""
    from argosy.state import db as db_mod

    async with db_mod.get_session() as session:
        return await session.run_sync(
            lambda s: build_sleeve_mandate_context(s, user_id=user_id)
        )


__all__ = [
    "build_sleeve_mandate_context",
    "find_x10_sleeve_class",
    "x10_sleeve_mandate_block",
]
