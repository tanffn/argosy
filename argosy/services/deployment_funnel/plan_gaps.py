"""Plan-level gap detection: missing diversifier classes + live sleeve gaps
for the Deploy Cash surface (target % vs current % vs $-to-close).
"""
from __future__ import annotations

from argosy.services.deployment_funnel.contracts import PlanGap

# Diversifier classes the deployment funnel treats as REQUIRED (a missing one is
# surfaced as a plan question). Deliberately EMPTY: the owner decided gold is
# intentionally excluded (the fleet's plan carries a Real assets (REIT/TIPS)
# sleeve and chose not to add gold — a defensible call, gold near ATH). We do
# NOT assert classes the fleet's own synthesis chose to omit. The mechanism
# below is retained as an extension point should a genuinely-required class be
# identified later — but nothing is hardcoded today.
_EXPECTED_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _plan_has(doc, keywords: tuple[str, ...]) -> bool:
    for c in doc.classes:
        label = (c.label or "").lower()
        if any(k in label for k in keywords):
            return True
    return False


def detect_missing_classes(doc) -> list[PlanGap]:
    """Return a typed PlanGap for each expected diversifier class the plan lacks.

    ``proposed_target_pct`` is left None on purpose — the weight is engine-
    derived from the diversification model when the sleeve is actually added
    (no magic number). ``blocked_amount_usd`` is 0.0 here because this gap is
    plan-structural, not tied to a specific blocked candidate; the deploy
    surface fills it from the tranche when it presents the proposal."""
    gaps: list[PlanGap] = []
    if doc is None:
        return gaps
    for asset_class, keywords in _EXPECTED_CLASSES:
        if not _plan_has(doc, keywords):
            gaps.append(
                PlanGap(
                    asset_class=asset_class,
                    current_target_pct=0.0,
                    proposed_target_pct=None,
                    reason_refs=(
                        f"the plan has no '{asset_class}' sleeve. This is a "
                        "QUESTION, not a verified gap: the fleet's plan carries a "
                        "Real assets (REIT/TIPS) sleeve and chose not to add gold "
                        "— which may be deliberate (e.g. gold near all-time highs). "
                        "Surfaced for the owner to decide; not auto-filled.",
                    ),
                    blocked_amount_usd=0.0,
                )
            )
    return gaps


def sleeve_gaps_for_deploy(
    *,
    doc,
    snapshot,
    cash_usd: float,
    classification_map=None,
) -> list[PlanGap]:
    """Per-sleeve underweight gaps for the entered cash amount.

    ``current_target_pct`` = live current % of book;
    ``proposed_target_pct`` = plan target %;
    ``blocked_amount_usd`` = $-to-close for ``cash_usd`` (scaled like
    ``allocation_engine``: full gap when total underweight ≤ cash, else
    proportional). Overweight / on-target sleeves are omitted.
    Unmapped bucket is never a deploy target (skipped).
    """
    from argosy.services.allocation_breakdown import build_allocation_breakdown
    from argosy.services.instrument_plan_class import UNMAPPED_LABEL

    if doc is None or snapshot is None or cash_usd <= 0:
        return []
    rows = build_allocation_breakdown(
        snapshot, doc, exclude_nvda=False, classification_map=classification_map,
    )
    book_usd = sum(float(r.current_value_k or 0.0) for r in rows) * 1000.0
    if book_usd <= 0:
        return []

    raw: list[tuple[str, float, float, float]] = []
    for r in rows:
        if r.label == UNMAPPED_LABEL:
            continue
        tgt = r.target_pct
        if tgt is None:
            continue
        cur = float(r.current_pct or 0.0)
        gap_pct = max(0.0, float(tgt) - cur)
        if gap_pct <= 1e-9:
            continue
        full_gap_usd = gap_pct / 100.0 * book_usd
        raw.append((r.label, cur, float(tgt), full_gap_usd))

    total_gap = sum(g for *_, g in raw)
    if total_gap <= 0:
        return []
    scale = 1.0 if total_gap <= cash_usd else cash_usd / total_gap
    out: list[PlanGap] = []
    remaining = round(float(cash_usd), 2)
    for label, cur, tgt, full_gap in sorted(raw, key=lambda t: (-t[3], t[0])):
        amount = min(round(full_gap * scale, 2), remaining)
        if amount <= 0:
            continue
        remaining = round(remaining - amount, 2)
        out.append(
            PlanGap(
                asset_class=label,
                current_target_pct=round(cur, 2),
                proposed_target_pct=round(tgt, 2),
                reason_refs=(
                    f"underweight vs plan: {cur:.1f}% current → {tgt:.1f}% target; "
                    f"${amount:,.0f} of this deploy closes the gap",
                ),
                blocked_amount_usd=amount,
            )
        )
    return out


__all__ = ["detect_missing_classes", "sleeve_gaps_for_deploy"]
