"""T4.1 — plan→proposal diff: the canonical plan vs current holdings.

The canonical ``TargetAllocationDoc`` is instrument-level (real tickers with a
``weight_within_class_pct`` inside each class, and a class ``target_pct`` of the
full book). Diffing those targets against actual holdings yields per-ticker
keep/trim/add deltas — the deterministic substrate the action-proposer turns
into "buy ~$X of VOO / trim ~$Y of NVDA". This module owns ONLY the money-math
diff; persistence to ``action_proposals`` + cap-checks are the T4.3/T4.4 wiring.

Basis: VALUE-based on the full tradeable book (sum of holdings). A symbol held
but absent from the plan targets to 0 (full exit). A symbol within
``keep_band_pct`` of its target is "keep" (no churn). Because the plan's class
targets sum to ~100 and instrument weights sum to ~100 within each class, the
per-symbol target values sum to the book total, so trims net against adds (a
closed book) — there is no fabricated cash creation.
"""
from __future__ import annotations

from dataclasses import dataclass

from argosy.services.target_allocation_doc import (
    TargetAllocationDoc,
    load_plan_target_allocation,
)


@dataclass(frozen=True)
class ProposalDelta:
    symbol: str
    action: str               # "add" | "trim" | "keep"
    current_value_usd: float
    target_value_usd: float
    delta_value_usd: float     # target - current (positive = buy, negative = trim)
    target_pct_of_book: float  # % of the full tradeable book this symbol targets
    rationale: str


def _target_values_by_symbol(
    doc: TargetAllocationDoc, total: float
) -> dict[str, tuple[float, float]]:
    """symbol -> (target_value_usd, target_pct_of_book). Aggregates a symbol
    that appears in more than one class."""
    out: dict[str, tuple[float, float]] = {}
    for c in doc.classes:
        for instr in c.instruments:
            pct_of_book = (c.target_pct / 100.0) * (instr.weight_within_class_pct / 100.0)
            value = pct_of_book * total
            prev_v, prev_pct = out.get(instr.symbol, (0.0, 0.0))
            out[instr.symbol] = (prev_v + value, prev_pct + pct_of_book * 100.0)
    return out


def diff_plan_vs_holdings(
    doc: TargetAllocationDoc,
    holdings: dict[str, float],
    *,
    keep_band_pct: float = 1.0,
) -> list[ProposalDelta]:
    """Per-ticker keep/trim/add deltas from the plan's targets vs ``holdings``.

    ``holdings`` maps symbol -> current USD value. ``keep_band_pct`` is the
    tolerance (in percentage points of the book) within which a position is
    left alone. Returns ``[]`` for an empty/zero book.
    """
    total = sum(float(v) for v in holdings.values())
    if total <= 0:
        return []

    targets = _target_values_by_symbol(doc, total)
    deltas: list[ProposalDelta] = []
    for sym in sorted(set(holdings) | set(targets)):
        current = float(holdings.get(sym, 0.0))
        target_value, target_pct = targets.get(sym, (0.0, 0.0))
        delta = target_value - current

        if sym not in targets:
            action = "trim"
            rationale = f"{sym} is not in the plan; exit the position (target 0%)."
        elif abs(delta) / total * 100.0 < keep_band_pct:
            action = "keep"
            rationale = (
                f"{sym} is within {keep_band_pct:.1f}pp of its {target_pct:.1f}% "
                f"plan target; hold."
            )
        elif delta > 0:
            action = "add"
            rationale = (
                f"Add ~${delta:,.0f} of {sym} to reach its {target_pct:.1f}% plan target."
            )
        else:
            action = "trim"
            rationale = (
                f"Trim ~${abs(delta):,.0f} of {sym} toward its {target_pct:.1f}% plan target."
            )

        deltas.append(ProposalDelta(
            symbol=sym,
            action=action,
            current_value_usd=round(current, 2),
            target_value_usd=round(target_value, 2),
            delta_value_usd=round(delta, 2),
            target_pct_of_book=round(target_pct, 4),
            rationale=rationale,
        ))
    return deltas


def plan_targets_by_symbol(doc: TargetAllocationDoc) -> dict[str, float]:
    """``{SYMBOL: target_pct_of_book}`` for the concentration cap-check.

    Same per-symbol percentage the diff uses, keyed UPPER because the execution
    preflight uppercases the proposal ticker (``risk_preflight.check_concentration_cap``).
    """
    out: dict[str, float] = {}
    for c in doc.classes:
        for instr in c.instruments:
            pct = c.target_pct * instr.weight_within_class_pct / 100.0
            key = instr.symbol.upper()
            out[key] = out.get(key, 0.0) + pct
    return out


def load_plan_targets(session, user_id: str) -> dict[str, float]:
    """Server-side ``{SYMBOL: target_pct}`` from the user's current canonical
    plan, or ``{}`` when no plan/doc is persisted.

    Makes the execution concentration cap-check AUTHORITATIVE (G21): the targets
    come from the canonical ``TargetAllocationDoc`` rather than being trusted
    from the request body. Never raises — a missing plan degrades to ``{}`` (the
    cap-check no-ops on an empty map) rather than breaking execution.
    """
    from argosy.state.queries import get_current_plan

    try:
        pv = get_current_plan(session, user_id)
    except Exception:  # noqa: BLE001 — defensive; absence degrades to {}
        return {}
    doc = load_plan_target_allocation(pv) if pv is not None else None
    return plan_targets_by_symbol(doc) if doc is not None else {}
