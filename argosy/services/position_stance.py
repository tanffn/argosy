"""Per-position STANCE REGISTRY (Ariel-directed, 2026-07-10).

ONE canonical record per held position that every surface projects — kills
the three-voices defect where /portfolio said HOLD while the fleet review
said SELL and the inbox held a proposal for the same ticker (SPCX).

Three stance sources, fixed precedence:

    open proposal (awaiting_human/approved/cooling)
        >  verified review (holding_reviews outcome 'proposed'/'hold')
        >  plan stance (derive_position_theses)

A ``held_unverified`` review (fleet said act, blind gate diverged,
fail-closed) NEVER changes the stance: it sets ``divergence=True`` and a
nothing-hidden note so the disagreement stays visible without being acted on.

The registry is a rebuildable projection: ``rebuild_stances`` derives the plan
layer, overlays reviews + open proposals, and persists rows (delete+insert per
user, one transaction). ``get_stances`` serves stored rows, rebuilding first
when they're older than ``max_age_seconds`` or any source row (proposal /
review / plan version / snapshot) is newer than ``built_at``.

The semantics of the proposal/review overlay were lifted VERBATIM from
``argosy/api/routes/positions.py::_overlay_open_proposals`` (now deleted) —
including the 0.95 full-vs-partial sell rule and the exact note wording.
Cache-safety carries over too: the plan-thesis cache stores immutable
dataclass cards; overlays are applied on copies at rebuild time, so closing a
proposal restores the plan stance on the next rebuild.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from argosy.services.per_position_thesis import (
    PositionThesis,
    derive_position_theses,
    emit_thesis_predictions,
)
from argosy.state.models import (
    AgentReport,
    HoldingReview,
    PlanVersion,
    PositionStance,
    Proposal,
)
from argosy.state.queries import get_current_plan, get_pending_draft

logger = logging.getLogger(__name__)

# Sentinel: "caller did not preload this" (None is a VALID snapshot value).
_UNSET: Any = object()


# Proposal statuses that count as a LIVE fleet decision — fresher than the
# plan's stance until the human decides (same set the old overlay used).
OPEN_PROPOSAL_STATUSES = ("awaiting_human", "approved", "cooling")

# Full-vs-partial sell threshold: a sell of >= 95% of the position is a SELL
# (full exit); anything smaller is a TRIM.
_FULL_SELL_RATIO = 0.95

# Serving TTL for stored stance rows — get_stances trusts rows younger than
# this (after the cheap source-freshness checks pass).
DEFAULT_MAX_AGE_SECONDS = 300

# In-process plan-thesis cache (perf) — moved here from the /positions/thesis
# route. Derivation is deterministic in (plan_version, snapshot), so we
# recompute only when one of those changes; the reliability-ledger emit
# happens only on a cache MISS, once per plan/snapshot. Bounded; a handful of
# entries in practice. Cleared on server restart.
_THESIS_CACHE: dict[tuple, list[PositionThesis]] = {}
_THESIS_CACHE_MAX = 32


def _snapshot_cache_key(snapshot: Any) -> tuple:
    return (
        getattr(snapshot, "snapshot_date", None),
        len(getattr(snapshot, "positions", []) or []),
        round(float(getattr(snapshot, "total_usd_value_k", 0.0) or 0.0), 2),
    )


def _snapshot_key_str(snapshot: Any) -> str:
    d, n, v = _snapshot_cache_key(snapshot)
    return f"{d}|{n}|{v}"


def _load_portfolio_snapshot(user_id: str, db: Session | None = None) -> Any:
    """Return the freshest portfolio snapshot or None.

    DB-FIRST (matching /api/portfolio/snapshot) so the per-position cards
    agree with the allocation chart AND load fast; falls back to the TSV walk
    only when the DB has nothing. Moved verbatim from the /positions route.
    """
    try:
        from argosy.services.portfolio_snapshot_store import (
            get_latest_snapshot_row,
            row_to_snapshot,
        )

        if db is not None:
            row = get_latest_snapshot_row(db, user_id)
            if row is not None:
                return row_to_snapshot(row)
    except Exception:  # noqa: BLE001 - fall through to the filesystem path
        logger.warning("portfolio snapshot DB lookup failed", exc_info=True)
    try:
        from argosy.api.routes.portfolio import _find_latest_tsv
        from argosy.ingest.tsv import parse_portfolio_tsv

        tsv = _find_latest_tsv()
        if tsv is None:
            return None
        return parse_portfolio_tsv(tsv)
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("portfolio snapshot lookup failed", exc_info=True)
        return None


def _load_plan_version(db: Session, user_id: str) -> PlanVersion | None:
    """Pending draft first (cards reflect the PROPOSED plan), else current."""
    pv: PlanVersion | None = get_pending_draft(db, user_id)
    if pv is None:
        pv = get_current_plan(db, user_id)
    return pv


def _plan_theses(
    db: Session, user_id: str, pv: PlanVersion, snapshot: Any
) -> list[PositionThesis]:
    """Derive (or serve cached) plan-layer thesis cards for one plan/snapshot.

    Cache MISS also fans out the reliability-ledger predictions (idempotent on
    (plan_version_id, ticker)), exactly as the route used to.
    """
    cache_key = (user_id, pv.id, _snapshot_cache_key(snapshot))
    cached = _THESIS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    reports: list[AgentReport] = []
    if pv.decision_run_id is not None:
        decision_id_str = f"plan-synth-{pv.decision_run_id}"
        reports = list(
            db.execute(
                select(AgentReport).where(
                    AgentReport.user_id == user_id,
                    AgentReport.decision_id == decision_id_str,
                )
            )
            .scalars()
            .all()
        )

    theses = derive_position_theses(
        plan_version=pv,
        portfolio_snapshot=snapshot,
        agent_reports=reports,
        session=db,
        user_id=user_id,
    )

    emit_thesis_predictions(
        db,
        user_id,
        plan_version_id=pv.id,
        theses=theses,
        provenance_weights_applied=(pv.decision_run_id is not None),
    )

    if len(_THESIS_CACHE) >= _THESIS_CACHE_MAX:
        _THESIS_CACHE.clear()  # simple bound — keys are plan/snapshot scoped
    _THESIS_CACHE[cache_key] = theses
    return theses


def _norm_conviction(value: str | None) -> str:
    """Normalize to the stance registry's HIGH | MED | LOW enum (unknown→LOW)."""
    v = (value or "").upper()
    if v == "HIGH":
        return "HIGH"
    if v in ("MED", "MEDIUM"):
        return "MED"
    return "LOW"


def _latest_reviews(db: Session, user_id: str) -> dict[str, HoldingReview]:
    """Latest fleet review per symbol — the second voice.

    Ordered by id asc so the LAST row per symbol wins (same as the old
    overlay). Never fatal: reviews are an annotation.
    """
    reviews: dict[str, HoldingReview] = {}
    try:
        for hr in (
            db.execute(
                select(HoldingReview)
                .where(HoldingReview.user_id == user_id)
                .order_by(HoldingReview.id.asc())
            )
            .scalars()
            .all()
        ):
            reviews[(hr.symbol or "").upper()] = hr  # latest wins
    except Exception:  # noqa: BLE001 — reviews are an annotation, never fatal
        logger.warning("holding_reviews overlay failed", exc_info=True)
    return reviews


def _open_proposals(db: Session, user_id: str) -> dict[str, Proposal]:
    rows = (
        db.execute(
            select(Proposal).where(
                Proposal.user_id == user_id,
                Proposal.status.in_(OPEN_PROPOSAL_STATUSES),
            )
        )
        .scalars()
        .all()
    )
    return {(r.ticker or "").upper(): r for r in rows}


def _proposal_stance(r: Proposal, card: PositionThesis) -> str:
    """SELL vs TRIM vs ADD vs BUY for an open proposal — the 0.95 rule.

    Lifted verbatim from ``_overlay_open_proposals``: a sell covering >= 95%
    of the current position (shares or USD) is a full exit (SELL); smaller is
    a TRIM. A buy is ADD when we already hold shares, else BUY.
    """
    if r.action == "sell":
        size = float(r.size_shares_or_currency or 0.0)
        if (r.size_units or "") == "shares":
            full = bool(card.current_shares) and size >= _FULL_SELL_RATIO * (
                card.current_shares or 0.0
            )
        else:
            full = bool(card.current_usd_value) and size >= _FULL_SELL_RATIO * (
                card.current_usd_value or 0.0
            )
        return "SELL" if full else "TRIM"
    return "ADD" if (card.current_shares or 0) > 0 else "BUY"


def rebuild_stances(
    db: Session,
    user_id: str,
    *,
    plan_version: PlanVersion | None | Any = _UNSET,
    snapshot: Any = _UNSET,
) -> list[PositionStance]:
    """Rebuild the stance registry for one user (delete+insert, one txn).

    Derives the plan layer, overlays the latest holding review per symbol and
    open proposals with precedence proposal > verified review > plan, and
    persists one row per thesis card (held positions first, plan "should add"
    cards included so the projection covers the full /positions list).

    ``plan_version`` / ``snapshot`` may be preloaded by the caller (the route
    loads both anyway); when omitted they're loaded here.
    """
    pv = _load_plan_version(db, user_id) if plan_version is _UNSET else plan_version
    built_at = datetime.now(timezone.utc)

    if pv is None:
        # No plan at all — an empty registry is the valid projection.
        db.query(PositionStance).filter(PositionStance.user_id == user_id).delete(
            synchronize_session="fetch"
        )
        db.commit()
        return []

    if snapshot is _UNSET:
        snapshot = _load_portfolio_snapshot(user_id, db)
    theses = _plan_theses(db, user_id, pv, snapshot)
    reviews = _latest_reviews(db, user_id)
    proposals = _open_proposals(db, user_id)
    snapshot_key = _snapshot_key_str(snapshot)

    rows: list[PositionStance] = []
    for card in theses:
        sym = (card.ticker or "").upper()
        plan_verdict = card.verdict
        stance = plan_verdict
        stance_source = "plan"
        conviction = _norm_conviction(card.conviction)
        divergence = False
        pending_proposal_id: int | None = None
        review_verdict: str | None = None
        review_outcome: str | None = None
        notes: list[str] = []

        hr = reviews.get(sym)
        if hr is not None:
            review_verdict = hr.verdict
            review_outcome = hr.outcome or ""
            if review_outcome in ("proposed", "hold"):
                # Verified review — beats the plan stance, with one carve-out:
                # a HOLD verdict (outcome 'hold') answers "should we act on
                # this HOLDING?" with "no action"; it does NOT adjudicate the
                # plan's deployment-schedule BUY/ADD (underweight vs target),
                # so it never downgrades those (SPMV would otherwise lose its
                # funded-by-schedule BUY to a routine thesis-intact review).
                rv = (hr.verdict or "").upper()
                if rv == "BUY" and (card.current_shares or 0) > 0:
                    rv = "ADD"
                overrides = rv in ("BUY", "ADD", "TRIM", "SELL") or (
                    rv == "HOLD" and plan_verdict == "HOLD"
                )
                if overrides:
                    stance = rv
                    stance_source = "review"
                    if hr.confidence:
                        conviction = _norm_conviction(hr.confidence)
            elif review_outcome == "held_unverified":
                # Fleet said act but the blind gate failed — fail-closed:
                # stance unchanged, divergence flagged, nothing hidden.
                divergence = True
                notes.append(
                    f"**Fleet review ({str(hr.reviewed_at)[:10]}):** suggested "
                    f"{hr.verdict} ({hr.confidence} confidence) but the blind "
                    f"verification diverged, so no action was filed "
                    f"(fail-closed). The stance shown is the plan's.\n\n"
                )

        r = proposals.get(sym)
        if r is not None:
            # Open proposal — the freshest voice; overrides everything and
            # (matching the old overlay exactly) its note REPLACES the other
            # notes rather than stacking on them.
            stance = _proposal_stance(r, card)
            stance_source = "proposal"
            pending_proposal_id = r.id
            if r.confidence:
                conviction = _norm_conviction(r.confidence)
            when = (
                f" (resurfaces {str(r.cooling_off_until)[:10]})"
                if r.status == "cooling" and r.cooling_off_until
                else ""
            )
            notes = [
                f"**Pending decision{when}:** a {r.action} proposal for "
                f"{sym} awaits you in the inbox — it overrides the "
                f"plan-derived stance below until decided.\n\n"
            ]
        elif stance in ("BUY", "ADD"):
            notes.append(
                "**Underweight vs plan target** — funded by the "
                "deployment schedule (proceeds route to the biggest-gap "
                "sleeve first), not by an action needed from you now.\n\n"
            )

        rows.append(
            PositionStance(
                user_id=user_id,
                symbol=sym,
                stance=stance,
                stance_source=stance_source,
                conviction=conviction,
                plan_verdict=plan_verdict,
                review_verdict=review_verdict,
                review_outcome=review_outcome,
                pending_proposal_id=pending_proposal_id,
                divergence=divergence,
                falsifiers_json=None,  # populated once the verdict registry
                # (handover 07-10 §4.2) records falsifiers per verdict.
                reasoning_md="".join(notes) + (card.reasoning_md or ""),
                plan_version_id=pv.id,
                snapshot_key=snapshot_key,
                built_at=built_at,
            )
        )

    # Persist: delete+insert per user in ONE transaction so readers never see
    # a half-built registry. synchronize_session="fetch" so the session's
    # identity map drops the deleted rows — SQLite (no AUTOINCREMENT) reuses
    # their ids for the fresh inserts, which otherwise collides in the map.
    db.query(PositionStance).filter(PositionStance.user_id == user_id).delete(
        synchronize_session="fetch"
    )
    db.add_all(rows)
    db.commit()
    return rows


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sources_newer_than(
    db: Session,
    user_id: str,
    built_at: datetime,
    stored_plan_version_id: int | None,
    stored_snapshot_key: str | None,
    *,
    plan_version: PlanVersion | None | Any = _UNSET,
    snapshot: Any = _UNSET,
) -> bool:
    """True when ANY stance source moved after the registry was built.

    Sources: latest proposals.updated_at (ALL statuses — closing a proposal
    must trigger a rebuild too), latest holding_reviews.reviewed_at, the
    current plan-version id, and the latest snapshot identity.
    """
    built = _as_utc(built_at)

    latest_prop = db.execute(
        select(func.max(Proposal.updated_at)).where(Proposal.user_id == user_id)
    ).scalar()
    if latest_prop is not None and _as_utc(latest_prop) > built:
        return True

    latest_review = db.execute(
        select(func.max(HoldingReview.reviewed_at)).where(
            HoldingReview.user_id == user_id
        )
    ).scalar()
    if latest_review is not None and _as_utc(latest_review) > built:
        return True

    pv = _load_plan_version(db, user_id) if plan_version is _UNSET else plan_version
    if (pv.id if pv is not None else None) != stored_plan_version_id:
        return True

    if snapshot is _UNSET:
        snapshot = _load_portfolio_snapshot(user_id, db)
    if _snapshot_key_str(snapshot) != (stored_snapshot_key or ""):
        return True

    return False


def get_stances(
    db: Session,
    user_id: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    *,
    plan_version: PlanVersion | None | Any = _UNSET,
    snapshot: Any = _UNSET,
) -> list[PositionStance]:
    """Serve the stored registry, rebuilding first when it's stale.

    Stale = no rows for a user who has a plan, rows older than
    ``max_age_seconds``, or any source row (proposal / review / plan version /
    snapshot) newer than ``built_at``.
    """
    rows = (
        db.execute(
            select(PositionStance).where(PositionStance.user_id == user_id)
        )
        .scalars()
        .all()
    )
    if not rows:
        return rebuild_stances(
            db, user_id, plan_version=plan_version, snapshot=snapshot
        )

    built_at = min((_as_utc(r.built_at) or datetime.min.replace(tzinfo=timezone.utc)) for r in rows)
    now = datetime.now(timezone.utc)
    if now - built_at > timedelta(seconds=max_age_seconds) or _sources_newer_than(
        db,
        user_id,
        built_at,
        rows[0].plan_version_id,
        rows[0].snapshot_key,
        plan_version=plan_version,
        snapshot=snapshot,
    ):
        return rebuild_stances(
            db, user_id, plan_version=plan_version, snapshot=snapshot
        )

    return list(rows)


def project_thesis_dtos(
    db: Session,
    user_id: str,
    *,
    plan_version: PlanVersion | None | Any = _UNSET,
    snapshot: Any = _UNSET,
) -> list[dict[str, Any]]:
    """The /api/positions/thesis projection: plan-layer cards with the
    stance registry's verdict + layered reasoning applied per ticker.

    Returns plain dicts in the exact PositionThesisDTO wire shape so the
    route (and therefore the UI) needs no changes. Cards are copied via
    ``to_dict()`` — the cached dataclasses are never mutated.
    """
    pv = _load_plan_version(db, user_id) if plan_version is _UNSET else plan_version
    if pv is None:
        return []

    if snapshot is _UNSET:
        snapshot = _load_portfolio_snapshot(user_id, db)
    stances = {
        s.symbol: s
        for s in get_stances(db, user_id, plan_version=pv, snapshot=snapshot)
    }
    theses = _plan_theses(db, user_id, pv, snapshot)

    from argosy.services.verdict_registry import provenance_for_subjects

    prov_map = provenance_for_subjects(
        db,
        user_id=user_id,
        subjects=[(card.ticker or "") for card in theses],
    )

    out: list[dict[str, Any]] = []
    for card in theses:
        d = card.to_dict()
        d.pop("reliability_annotations", None)  # not part of the wire DTO
        s = stances.get((card.ticker or "").upper())
        if s is not None:
            d["verdict"] = s.stance
            d["reasoning_md"] = s.reasoning_md
            if s.conviction:
                d["conviction"] = s.conviction
        prov = prov_map.get((card.ticker or "").upper())
        if prov is not None:
            d["falsifier_state"] = prov.falsifier_state
            d["falsifiers"] = list(prov.falsifiers)
            d["next_validation"] = prov.next_validation
            d["last_fleet_check_at"] = prov.last_fleet_check_at
        else:
            d["falsifier_state"] = "none_recorded"
            d["falsifiers"] = []
            d["next_validation"] = None
            d["last_fleet_check_at"] = None
        out.append(d)
    return out


def clear_thesis_cache() -> None:
    """Drop the in-process plan-thesis cache (e.g. after a code-gate change)."""
    _THESIS_CACHE.clear()


__all__ = [
    "DEFAULT_MAX_AGE_SECONDS",
    "OPEN_PROPOSAL_STATUSES",
    "clear_thesis_cache",
    "get_stances",
    "project_thesis_dtos",
    "rebuild_stances",
]
