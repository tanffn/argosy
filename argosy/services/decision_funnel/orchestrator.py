"""Decision-funnel orchestrator — the conductor (P1).

Runs the escalating tiers and records EVERY step through the trace recorder so
the run is fully replayable:

  Stage 0  market review (cheap, off already-ingested data)
  Stage 1  deterministic relevance routing onto the book (default NO-OP)
  Stage 2  cheap Sonnet triage of the routed candidates (kill the no-ops)
  Stage 3  full Opus deep-decision fleet for survivors (propose-and-ask)

Conservative escalation, not a daily recommender:
- Master kill switch gates whether the funnel runs at all (checked by the loop).
- SHADOW mode (default) records proposals + the full trace but surfaces NOTHING.
- STAGE 3 is gated separately: when off, Stage 0-2 run + are traced, but no
  expensive deep decision fires (the trace shows what WOULD escalate).
- Discretionary proposals are always propose-and-ask (tier T2, human review).

The deterministic + trace work uses a sync session (the cadence-loop pattern);
the LLM stages (triage, deep decision) are awaited. The triage / deep-decision
callables are injectable so the whole flow is testable without live LLMs.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from sqlalchemy import func, select

from argosy.config import get_settings
from argosy.logging import get_logger
from argosy.services.decision_funnel.book import load_book
from argosy.services.decision_funnel.deep_decision import run_deep_decision
from argosy.services.decision_funnel.policy import DEFAULT_POLICY, RoutingPolicy
from argosy.services.decision_funnel.stage0_market import build_market_read
from argosy.services.decision_funnel.stage1_routing import (
    PerNameSignal,
    RoutedCandidate,
    _cap_for,
    route,
)
from argosy.services.decision_funnel.triage import triage_candidate
from argosy.services.funnel_trace import (
    close_run,
    open_run,
    record_snapshot,
    record_stage_row,
)
from argosy.services.ips import build_ips
from argosy.services.proposal_expiry import default_expiry, expire_stale_proposals

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

_log = get_logger("argosy.services.decision_funnel.orchestrator")

# The fleet's headline model (per accuracy-over-cost: T2 deep decisions run the
# Opus fleet). The granular per-agent model/prompt identities live in
# agent_reports under the snapshot's decision_run_id.
_FLEET_MODEL = "claude-opus-4-8"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_session_factory():
    from argosy.orchestrator.loops.state_observer import _build_default_session_factory

    return _build_default_session_factory()


def _last_review_map(session: "Session", user_id: str) -> dict[str, datetime]:
    """Per-ticker last deep-decision time, from the immutable snapshots — the
    cooldown source of truth."""
    from argosy.state.models import DecisionSnapshot

    rows = session.execute(
        select(DecisionSnapshot.ticker, func.max(DecisionSnapshot.created_at))
        .where(DecisionSnapshot.user_id == user_id)
        .group_by(DecisionSnapshot.ticker)
    ).all()
    out: dict[str, datetime] = {}
    for ticker, ts in rows:
        if ticker and ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            out[ticker.upper()] = ts
    return out


def _build_signals(
    session: "Session", user_id: str, book, market
) -> dict[str, PerNameSignal]:
    """Assemble per-name signals from ALREADY-INGESTED data: active thesis
    monitor flags + high-materiality news. Price/earnings triggers stay None
    when that data isn't already ingested (honest — the audit sample is the
    false-drop safety net)."""
    from argosy.state.models import MonitorFlag

    held = {h.ticker.upper() for h in book}

    # Thesis flags per ticker.
    thesis: dict[str, tuple[str | None, str | None]] = {}
    try:
        flags = session.execute(
            select(MonitorFlag).where(
                MonitorFlag.user_id == user_id, MonitorFlag.status == "active"
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001
        flags = []
    import json as _json

    for f in flags:
        if not (f.kind or "").startswith("thesis_monitor_"):
            continue
        try:
            payload = _json.loads(f.payload or "{}")
        except (ValueError, TypeError):
            payload = {}
        tk = str(payload.get("ticker") or "").upper()
        if not tk:
            continue
        thesis[tk] = (payload.get("thesis_status"), f.severity)

    # High-materiality news per ticker (from the Stage-0 read).
    news_by: dict[str, str | None] = {}
    for hit in market.high_materiality_news:
        news_by.setdefault(hit.ticker.upper(), hit.sentiment)

    signals: dict[str, PerNameSignal] = {}
    for tk in held:
        t_status, t_sev = thesis.get(tk, (None, None))
        signals[tk] = PerNameSignal(
            ticker=tk,
            thesis_status=t_status,
            thesis_severity=t_sev,
            high_materiality_news=tk in news_by,
            news_sentiment=news_by.get(tk),
        )
    return signals


async def run_funnel(
    user_id: str = "ariel",
    *,
    now: datetime | None = None,
    trigger: str = "scheduler",
    session_factory: Callable[[], "Session"] | None = None,
    policy: RoutingPolicy = DEFAULT_POLICY,
    triage_fn: Callable[..., Any] = triage_candidate,
    deep_decision_fn: Callable[..., Any] = run_deep_decision,
    settings: Any = None,
) -> dict[str, Any]:
    """Run one full funnel pass. Returns a totals summary (for the loop's
    job_runs output_summary)."""
    settings = settings or get_settings()
    shadow = bool(getattr(settings, "decision_funnel_shadow", True))
    stage3_enabled = bool(getattr(settings, "decision_funnel_stage3", False))
    now = now or _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    day = now.date().isoformat()
    sf = session_factory or _default_session_factory()

    totals: dict[str, Any] = {
        "shadow": shadow, "stage3_enabled": stage3_enabled,
        "stage1_routed": 0, "stage1_dropped": 0, "stage1_audit": 0,
        "stage2_go": 0, "stage2_stop": 0,
        "stage3_proposed": 0, "stage3_blocked": 0, "stage3_skipped": 0,
        "surfaced": 0,
    }

    # ---- Phase A: deterministic (sync session) ----
    s = sf()
    try:
        try:
            expire_stale_proposals(s, user_id=user_id, now=now)
        except Exception as exc:  # noqa: BLE001
            _log.warning("decision_funnel.expiry_failed", error=str(exc)[:200])

        book = load_book(s, user_id=user_id)
        market = build_market_read(s, user_id=user_id, now=now)
        ips = build_ips(s, user_id=user_id)
        last_review = _last_review_map(s, user_id)
        signals = _build_signals(s, user_id, book, market)

        run = open_run(
            s, user_id=user_id, day=day, trigger=trigger, shadow=shadow,
            policy_version=policy.version,
            ips_version=(ips.ips_version if ips else None),
            plan_version_id=(ips.plan_version_id if ips else None),
            started_at=now,
        )
        run_id = run.id

        record_stage_row(
            s, run_id=run_id, stage="stage0", subject="MARKET", subject_type="market",
            decision=("risk_off" if market.risk_off else "neutral"),
            reason=market.summary, inputs=market.to_dict(), commit=False,
        )

        routing = route(
            book=book, market_read=market, ips=ips, signals=signals,
            last_review_by_ticker=last_review, policy=policy, day=day, now=now,
        )
        for cand in routing.routed:
            record_stage_row(
                s, run_id=run_id, stage="stage1", subject=cand.subject,
                subject_type=cand.subject_type, decision="routed",
                reason=cand.reason, signal_or_rule=cand.primary_signal,
                inputs={"triggers": cand.triggers, "is_audit": cand.is_audit},
                commit=False,
            )
            if cand.is_audit:
                totals["stage1_audit"] += 1
        for drop in routing.dropped:
            record_stage_row(
                s, run_id=run_id, stage="stage1", subject=drop.subject,
                subject_type=drop.subject_type, decision="dropped",
                reason=drop.reason, signal_or_rule=drop.signal, commit=False,
            )
        s.commit()
        totals["stage1_routed"] = len(routing.routed)
        totals["stage1_dropped"] = len(routing.dropped)

        weight_by = {h.ticker.upper(): h.weight_pct for h in book}
        cap_by = {
            h.ticker.upper(): _cap_for(h.ticker, ips, policy)[0] for h in book
        }
        market_dict = market.to_dict()
    finally:
        s.close()

    # ---- Phase B: Stage 2 triage (async LLM) ----
    survivors: list[tuple[RoutedCandidate, Any]] = []
    for cand in routing.routed:
        subj = cand.subject.upper()
        try:
            outcome = await asyncio.to_thread(
                triage_fn, cand, market=market,
                weight_pct=weight_by.get(subj), cap_pct=cap_by.get(subj),
                user_id=user_id,
            )
        except Exception as exc:  # noqa: BLE001
            with sf() as s2:
                record_stage_row(
                    s2, run_id=run_id, stage="stage2", subject=cand.subject,
                    subject_type=cand.subject_type, decision="triage_error",
                    reason=str(exc)[:300], signal_or_rule=cand.primary_signal,
                )
            continue
        with sf() as s2:
            record_stage_row(
                s2, run_id=run_id, stage="stage2", subject=cand.subject,
                subject_type=cand.subject_type,
                decision=("triage_go" if outcome.warrants_decision else "triage_stop"),
                reason=outcome.rationale, signal_or_rule=cand.primary_signal,
                model=outcome.model, prompt_hash=outcome.prompt_hash,
                tokens_in=outcome.tokens_in, tokens_out=outcome.tokens_out,
                cost_usd=outcome.cost_usd,
                inputs={"urgency": outcome.urgency},
            )
        if outcome.warrants_decision:
            survivors.append((cand, outcome))
            totals["stage2_go"] += 1
        else:
            totals["stage2_stop"] += 1

    # ---- Phase C: Stage 3 deep decision (async LLM, gated) ----
    for cand, _t in survivors:
        if cand.subject_type != "holding":
            with sf() as s3:
                record_stage_row(
                    s3, run_id=run_id, stage="stage3", subject=cand.subject,
                    subject_type=cand.subject_type, decision="sleeve_deferred",
                    reason="sleeve-level review deferred to plan refresh (P3)",
                    signal_or_rule=cand.primary_signal,
                )
            continue
        if not stage3_enabled:
            with sf() as s3:
                record_stage_row(
                    s3, run_id=run_id, stage="stage3", subject=cand.subject,
                    subject_type="holding", decision="stage3_skipped",
                    reason="Stage 3 disabled (shadow calibration) — would escalate",
                    signal_or_rule=cand.primary_signal,
                )
            totals["stage3_skipped"] += 1
            continue

        dd = await deep_decision_fn(
            user_id=user_id, ticker=cand.subject, account_class="main",
        )
        subj = cand.subject.upper()
        with sf() as s3:
            decision_payload = {
                "action": dd.action,
                "status": dd.status,
                "blocked_reason": dd.blocked_reason,
                "blocked_by": dd.blocked_by,
                "triggers": cand.triggers,
                "router_reason": cand.reason,
            }
            snap = record_snapshot(
                s3, run_id=run_id, user_id=user_id, ticker=cand.subject, day=day,
                decision=decision_payload,
                portfolio_snapshot={"weight_pct": weight_by.get(subj),
                                    "cap_pct": cap_by.get(subj),
                                    "book": weight_by},
                market_snapshot=market_dict,
                policy_version=policy.version, policy=policy.to_dict(),
                model_name=_FLEET_MODEL,
                prompt_template_hash=(
                    f"fleet:T2:{policy.version}:{day}:{subj}"
                ),
                model_inputs={"decision_run_id": dd.decision_run_id, "fleet_tier": "T2"},
                source_refs=market.source_refs,
                why_not_act=(dd.blocked_reason if dd.status != "approved" else None),
                decision_run_id=dd.decision_run_id,
                proposal_id=dd.proposal_id,
            )
            # Stamp the proposal lifecycle columns (source/shadow/expiry/run).
            if dd.proposal_id:
                from argosy.state.models import Proposal

                p = s3.get(Proposal, dd.proposal_id)
                if p is not None:
                    p.source = "decision_funnel"
                    p.shadow = 1 if shadow else 0
                    p.funnel_run_id = run_id
                    p.expires_at = default_expiry(now)
                    s3.commit()

            proposed = dd.status == "approved"
            record_stage_row(
                s3, run_id=run_id, stage="stage3", subject=cand.subject,
                subject_type="holding",
                decision=("proposed" if proposed else "blocked"),
                reason=(f"action={dd.action}" if proposed else (dd.blocked_reason or "blocked")),
                signal_or_rule=cand.primary_signal,
                inputs={"action": dd.action, "status": dd.status},
                snapshot_id=snap.id, proposal_id=dd.proposal_id,
            )
            # Surface routing: only an approved, non-shadow proposal reaches the
            # client's "needs me now" surface; everything else is recorded but
            # hidden (shadow calibration / blocked).
            surfaced = proposed and not shadow
            record_stage_row(
                s3, run_id=run_id, stage="surface", subject=cand.subject,
                subject_type="holding",
                decision=("surfaced" if surfaced else "hidden"),
                reason=(
                    "client needs a decision" if surfaced
                    else ("shadow mode — recorded, not surfaced" if shadow else "no actionable proposal")
                ),
                proposal_id=dd.proposal_id,
            )
            if proposed:
                totals["stage3_proposed"] += 1
            else:
                totals["stage3_blocked"] += 1
            if surfaced:
                totals["surfaced"] += 1

    # ---- close ----
    with sf() as sc:
        close_run(
            sc, run_id=run_id, status="ok", totals=totals,
            macro_read=market_dict, finished_at=_utcnow(),
        )
    _log.info("decision_funnel.run_done", user_id=user_id, run_id=run_id, **{
        k: v for k, v in totals.items() if isinstance(v, int)
    })
    return {"run_id": run_id, **totals}


__all__ = ["run_funnel"]
