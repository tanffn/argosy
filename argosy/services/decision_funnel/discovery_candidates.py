"""Stage 1 (discovery source) — feed the discovery funnel's conviction picks
into the decision funnel as new-name BUY candidates.

The discovery funnel (``high_potential_funnel``) surfaces NEW names: radar →
Sonnet estimator → Opus grader → ``FleetPick`` (conviction + BUY/WATCH/PASS
verdict + thesis + citations), persisted on ``ScanState.fleet_json``. This
module reads the persisted, still-active picks and turns the strongest ones
into ``RoutedCandidate``s (``subject_type="discovery"``) that flow through the
same Stage 2 triage → Stage 3 deep decision → propose-and-ask BUY as held
names — subject to the same shadow / IPS / north-star / estate guards.

Conservative by design (codex's escalation framing): a NEW name is a higher bar
than acting on something already held, so only HIGH-conviction BUY picks route,
and a name already in the book is skipped here (it routes via the normal
per-name path instead — no double review).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from argosy.services.decision_funnel.policy import DEFAULT_POLICY, RoutingPolicy
from argosy.services.decision_funnel.stage1_routing import RoutedCandidate

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

# Conviction ordering, so the policy's ``discovery_conviction_floor`` is a real
# FLOOR (admit this level AND stronger), not an exact-equality veto. With the
# default floor HIGH only HIGH routes (unchanged); lowering the floor to MEDIUM
# routes MEDIUM + HIGH — i.e. the owner/IPS decides how much of the grader's BUY
# conviction to deep-review, instead of a hardcoded HIGH-only drop of MED/LOW.
_CONVICTION_RANK: dict[str, int] = {"LOW": 1, "MED": 2, "MEDIUM": 2, "HIGH": 3}


def _active_fleet_picks(session: Session, user_id: str):
    """Yield the persisted, still-``active`` FleetPicks for the user. Best-effort:
    a malformed row is skipped, never fatal."""
    from sqlalchemy import select

    from argosy.services.high_potential_funnel import _pick_from_json
    from argosy.state.models import ScanState

    rows = (
        session.execute(
            select(ScanState).where(
                ScanState.user_id == user_id, ScanState.status == "active"
            )
        )
        .scalars()
        .all()
    )
    for r in rows:
        if not r.fleet_json:
            continue
        try:
            yield _pick_from_json(r.fleet_json)
        except (ValueError, KeyError):
            continue


def load_discovery_candidates(
    session: Session,
    *,
    user_id: str,
    held_tickers: set[str],
    policy: RoutingPolicy = DEFAULT_POLICY,
) -> list[RoutedCandidate]:
    """Return discovery picks that clear the conviction bar as routed candidates.

    Skips names already held (those route via the per-name path) and de-dupes by
    ticker (the strongest pick wins if a ticker somehow appears twice). Each
    candidate carries its conviction + grader citations in ``extra`` so the
    Stage-1 trace row is fully sourced (radar → proposal)."""
    if not policy.route_discovery_picks:
        return []
    floor = (policy.discovery_conviction_floor or "HIGH").upper()
    floor_rank = _CONVICTION_RANK.get(floor, 3)  # unknown floor -> HIGH (strictest)
    held = {t.upper() for t in held_tickers}
    seen: set[str] = set()
    out: list[RoutedCandidate] = []
    for pick in _active_fleet_picks(session, user_id):
        tk = (pick.ticker or "").upper()
        if not tk or tk in held or tk in seen:
            continue
        conviction = (pick.conviction or "").upper()
        verdict = (pick.verdict or "").upper()
        # A BUY at or above the policy's conviction FLOOR earns a new-name deep
        # review. The floor is IPS/policy-owned: HIGH (default) routes only HIGH;
        # MEDIUM routes MEDIUM+HIGH; the grader's picks are not silently vetoed.
        if verdict != "BUY" or _CONVICTION_RANK.get(conviction, 0) < floor_rank:
            continue
        seen.add(tk)
        out.append(
            RoutedCandidate(
                subject=tk,
                subject_type="discovery",
                triggers=["discovery_pick"],
                primary_signal="discovery_pick",
                reason=(
                    f"discovery pick — {conviction} conviction BUY from the "
                    f"high-potential funnel"
                ),
                extra={
                    "conviction": conviction,
                    "verdict": verdict,
                    "grader_cites": list(getattr(pick, "cites", []) or [])[:8],
                },
            )
        )
    return out


__all__ = ["load_discovery_candidates"]
