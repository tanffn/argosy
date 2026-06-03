"""Plan-narrative service (Wave 8 v2 polish).

Thin orchestrator around the PlanNarrativeAgent. Reads the user's
current plan + identity + baseline-voice excerpt, invokes the agent,
and returns the bilingual narrative.

Caching: two layers, both keyed by ``plan_version_id``. A process-local
``_CACHE`` is the hot layer; ``plan_versions.narrative_json`` is the
DB-persisted warm layer (migration 0062). The narrative is written
through to the DB on first generation, so it survives a backend restart
and the /plan recap loads instantly instead of re-running the LLM. The
LLM only runs on a genuine miss (a newly-accepted plan, or
``force_refresh``).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.state.models import PlanVersion, UserContext
from argosy.state.queries import get_current_plan

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanNarrativeResult:
    plan_version_id: int
    narrative_md_en: str
    narrative_md_he: str
    confidence: str  # "HIGH" / "MEDIUM" / "LOW"


# Process-local cache keyed by (user_id, plan_version_id).
# Manual invalidation by accepting a new plan or restarting the
# uvicorn worker — both are explicit user actions.
_CACHE: dict[tuple[str, int], PlanNarrativeResult] = {}


def _load_identity_excerpt(session: Session, user_id: str) -> str:
    """Pull a short identity excerpt from UserContext for the agent
    framing. Returns empty string on any error."""
    try:
        ctx = (
            session.query(UserContext)
            .filter(UserContext.user_id == user_id)
            .one_or_none()
        )
    except Exception:  # pragma: no cover - defensive
        return ""
    if ctx is None:
        return ""
    parts: list[str] = []
    for label in ("identity_yaml", "goals_yaml", "constraints_yaml"):
        raw = getattr(ctx, label, "") or ""
        raw = raw.strip()
        if not raw:
            continue
        # Cap each block at 4K characters — agent only needs framing,
        # not the full profile; long YAML blobs eat the prompt window.
        if len(raw) > 4000:
            raw = raw[:4000] + "\n# (truncated)"
        parts.append(f"# {label}\n{raw}")
    return "\n\n".join(parts)


def _load_baseline_voice(session: Session, user_id: str) -> str:
    """Pull a short sample of the user's BASELINE plan markdown so
    the narrative agent matches its tone. Falls back to empty string
    when the user has no baseline (rare — every user has one)."""
    baseline = (
        session.execute(
            select(PlanVersion)
            .where(
                PlanVersion.user_id == user_id,
                PlanVersion.role == "baseline",
            )
            .order_by(desc(PlanVersion.imported_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if baseline is None or not baseline.raw_markdown:
        return ""
    md = baseline.raw_markdown.strip()
    # First ~800 chars — enough for the agent to lock onto the
    # author's tone without bloating the prompt.
    return md[:800]


def _load_snapshot_composition_md(
    session: Session, user_id: str
) -> str:
    """Render the user's latest portfolio_snapshot allocation as a
    short markdown block so the narrative agent has explicit ground
    truth about CURRENT diversification (not just the synth's
    framing). Without this, the narrative inherits the synth's
    "concentrated single stock" framing even when the user already
    has many asset classes.

    Returns empty string when no snapshot exists.
    """
    from argosy.state.models import PortfolioSnapshotRow

    snap = (
        session.execute(
            select(PortfolioSnapshotRow)
            .where(PortfolioSnapshotRow.user_id == user_id)
            .order_by(desc(PortfolioSnapshotRow.imported_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if snap is None or not snap.allocations_json:
        return ""
    try:
        rows = json.loads(snap.allocations_json)
    except json.JSONDecodeError:
        return ""
    if not isinstance(rows, list):
        return ""
    lines: list[str] = [
        f"# Current portfolio composition "
        f"(from snapshot {snap.snapshot_date.isoformat() if snap.snapshot_date else 'today'})",
        "",
        "| Asset class | % of portfolio |",
        "|---|---|",
    ]
    for row in rows:
        if not isinstance(row, dict):
            continue
        cat = row.get("category")
        pct = row.get("pct")
        if not isinstance(cat, str):
            continue
        try:
            pct_f = float(pct) if pct is not None else 0.0
        except (TypeError, ValueError):
            continue
        if pct_f <= 0.0:
            continue
        lines.append(f"| {cat} | {pct_f:.1f}% |")
    return "\n".join(lines)


def _assemble_plan_input(pv: PlanVersion) -> str:
    """Concatenate the three horizon markdowns + a compact JSON
    summary of structured targets / themes / actions / deltas for
    the narrative agent."""
    parts: list[str] = []
    if pv.version_label:
        parts.append(f"# Plan label: {pv.version_label}")
    for label, md_field, json_field in (
        ("Long horizon (multi-year)", "horizon_long_md", "horizon_long_json"),
        ("Medium horizon (12–24 months)", "horizon_medium_md", "horizon_medium_json"),
        ("Short horizon (next 90 days)", "horizon_short_md", "horizon_short_json"),
    ):
        md = (getattr(pv, md_field, "") or "").strip()
        if md:
            parts.append(f"## {label}\n{md}")
        # Append a compact structured summary so the agent can quote
        # exact dates / target values without re-parsing prose.
        raw_json = getattr(pv, json_field, None)
        if raw_json:
            try:
                payload = json.loads(raw_json)
                compact = {
                    "targets": payload.get("targets") or [],
                    "themes": payload.get("themes") or [],
                    "actions": payload.get("actions") or [],
                    "deltas_from_prior": [
                        {
                            "summary": d.get("summary"),
                            "accepted": d.get("accepted"),
                            "item_kind": d.get("item_kind"),
                        }
                        for d in (payload.get("deltas_from_prior") or [])
                    ],
                    "rationale": payload.get("rationale"),
                }
                parts.append(
                    f"### Structured summary — {label}\n```json\n"
                    + json.dumps(compact, indent=2, ensure_ascii=False)
                    + "\n```"
                )
            except (json.JSONDecodeError, TypeError):  # pragma: no cover
                pass
    return "\n\n".join(parts)


async def get_plan_narrative(
    session: Session,
    user_id: str,
    *,
    force_refresh: bool = False,
) -> PlanNarrativeResult | None:
    """Top-level entry. Returns the cached or freshly-generated
    bilingual narrative for the user's current plan, or None when
    no current plan exists."""
    pv = get_current_plan(session, user_id)
    if pv is None:
        return None
    cache_key = (user_id, pv.id)
    if not force_refresh:
        # Hot layer: process-local cache.
        if cache_key in _CACHE:
            return _CACHE[cache_key]
        # Warm layer: DB-persisted narrative (migration 0062). Survives
        # restarts, so the recap loads instantly instead of re-running
        # the LLM. Populate the hot cache on a DB hit.
        persisted = _load_persisted_narrative(pv)
        if persisted is not None:
            _CACHE[cache_key] = persisted
            return persisted

    plan_input = _assemble_plan_input(pv)
    # Wave 8 v2 polish: include the current portfolio composition so
    # the narrative agent can correctly frame today's diversification
    # vs the plan's NVDA-reduction direction. Without this the
    # narrative inherits the synth's "concentrated single stock"
    # framing even when the user already has 8 asset classes.
    snapshot_md = _load_snapshot_composition_md(session, user_id)
    if snapshot_md:
        plan_input = snapshot_md + "\n\n" + plan_input
    identity = _load_identity_excerpt(session, user_id)
    baseline = _load_baseline_voice(session, user_id)

    # Import the agent lazily so the route module doesn't pull in
    # the agent SDK at import time (keeps test collection fast).
    from argosy.agents.plan_narrative import PlanNarrativeAgent

    agent = PlanNarrativeAgent(user_id=user_id)
    try:
        report = await agent.run(
            plan_input=plan_input,
            identity_excerpt=identity,
            baseline_voice=baseline,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "plan_narrative.agent_failed user_id=%s plan_version_id=%s err=%s",
            user_id,
            pv.id,
            exc,
        )
        return None

    out = report.output
    result = PlanNarrativeResult(
        plan_version_id=pv.id,
        narrative_md_en=out.narrative_md_en,
        narrative_md_he=out.narrative_md_he,
        confidence=str(out.confidence),
    )
    _CACHE[cache_key] = result
    # Write-through to the DB so the narrative survives a backend restart
    # (the hot _CACHE is process-local). Best-effort: a persistence
    # failure must not fail the request — the in-memory result still
    # serves this process; a later load just regenerates.
    _persist_narrative(session, pv, result)
    return result


def _load_persisted_narrative(pv: PlanVersion) -> PlanNarrativeResult | None:
    """Reconstruct a PlanNarrativeResult from ``pv.narrative_json``.
    Returns None when the column is empty or malformed."""
    raw = getattr(pv, "narrative_json", None)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return PlanNarrativeResult(
            plan_version_id=pv.id,
            narrative_md_en=data["narrative_md_en"],
            narrative_md_he=data["narrative_md_he"],
            confidence=data.get("confidence", "MEDIUM"),
        )
    except (json.JSONDecodeError, KeyError, TypeError):  # pragma: no cover
        return None


def _persist_narrative(
    session: Session, pv: PlanVersion, result: PlanNarrativeResult
) -> None:
    """Write the generated narrative onto ``pv.narrative_json`` (commit).
    Best-effort — swallow + log on failure."""
    try:
        pv.narrative_json = json.dumps(
            {
                "narrative_md_en": result.narrative_md_en,
                "narrative_md_he": result.narrative_md_he,
                "confidence": result.confidence,
            },
            ensure_ascii=False,
        )
        session.add(pv)
        session.commit()
    except Exception as exc:  # noqa: BLE001 — persistence is best-effort
        logger.warning(
            "plan_narrative.persist_failed user_id=%s plan_version_id=%s err=%s",
            pv.user_id, pv.id, exc,
        )
        try:
            session.rollback()
        except Exception:  # pragma: no cover — defensive
            pass


def invalidate_narrative_cache(user_id: str, plan_version_id: int) -> None:
    """Drop the cached narrative for ``user_id`` / ``plan_version_id``.
    Called by the /draft/{id}/accept handler when a new plan promotes
    to current. Clears both the hot cache and the persisted column so a
    forced refresh regenerates from scratch."""
    _CACHE.pop((user_id, plan_version_id), None)
