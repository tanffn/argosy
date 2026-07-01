"""Plan-level gap detection: classes a diversified plan should carry that the
current plan is MISSING entirely (the gold/alternatives hole being the live
case). This is distinct from candidate-level plan-gaps (a buy implying an
absent class) — here we inspect the plan itself, independent of any candidate,
so the gap surfaces even when the deterministic engine never proposes the
missing class (it can't propose gold if the plan has no gold sleeve).

Owner decision: a missing class is raised as a plan CHANGE-REQUEST proposal for
the owner to approve; the funnel does NOT invent the sleeve weight. The
engine-derived weight + the publish-gated plan amendment are the remaining
Increment-3 work — this module produces the typed gap + proposal, not the edit.
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


__all__ = ["detect_missing_classes"]
