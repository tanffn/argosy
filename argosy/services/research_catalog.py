"""Shared research ledger and idempotent, source-independent routing."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from argosy.state.research_models import ResearchSource, ResearchItem, ResearchClaim, ResearchReviewRequest, ResearchEvidenceUse

KINDS = {"rss", "sec13d", "sec13f", "youtube", "manual", "existing"}


def index_existing_events(*, user_id):
    """Reuse successful adapter pulls; no second network/fleet pass."""
    from argosy.state.models import InvestorEvent
    count = 0
    with research_session() as session:
        events = session.scalars(select(InvestorEvent).where(InvestorEvent.user_id == user_id,
            InvestorEvent.ingested_at >= datetime.now(UTC) - timedelta(days=90)).order_by(InvestorEvent.ingested_at.desc()).limit(3000)).all()
        for event in events:
            if not event.ticker or not event.headline:
                continue
            source = upsert_source(session, user_id=user_id, name=event.source.replace("_", " ").title(),
                kind="existing", reference=event.source)
            raw = json.loads(event.payload_json or "{}")
            if not isinstance(raw, dict):
                raw = {}
            item, created = enqueue(session, source, external_id="event:" + str(event.id), title=event.headline,
                url=raw.get("url") or raw.get("source_url") or raw.get("document_url") or raw.get("filing_url") or "",
                body=event.headline + "\n" + event.payload_json, published_at=event.occurred_at, now=utc(event.ingested_at))
            complete_item(session, item, {"claims": {"named_tickers": [event.ticker], "claims": [
                {"statement": event.headline, "named_entities": [event.ticker], "claim_type": "reported_event"}]},
                "synthesis": {}, "original_event": raw}, now=utc(event.ingested_at))
            count += int(created)
        session.commit()
    return count


def digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def symbol(value) -> str | None:
    value = str(value or "").strip().lstrip("$")
    return value if re.fullmatch(r"[A-Z]{1,5}(?:[./-][A-Z]{1,3})?", value) else None


def utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


@contextmanager
def research_session(database_url=None):
    from argosy.config import get_settings
    from argosy.state.db import create_sync_engine
    engine = create_sync_engine(str(database_url or get_settings().database_url).replace("+aiosqlite", ""))
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        engine.dispose()


def upsert_source(session, *, user_id: str, name: str, kind: str, reference: str,
                  enabled: bool = True, cadence_hours: int = 24, priority: int = 50):
    if kind not in KINDS:
        raise ValueError("Unknown research source kind")
    if not 1 <= cadence_hours <= 24 * 90 or not 0 <= priority <= 100:
        raise ValueError("Invalid cadence or priority")
    reference = reference.strip()
    if kind == "rss" or (kind == "sec13d" and reference != "all"):
        from argosy.services.research_connectors import validate_public_url
        validate_public_url(reference)
        if kind == "sec13d":
            from urllib.parse import urlsplit
            host = urlsplit(reference).hostname or ""
            if host != "sec.gov" and not host.endswith(".sec.gov"):
                raise ValueError("13D feeds must be hosted by SEC.gov")
    elif kind == "sec13f" and not re.fullmatch(r"\d{1,10}", reference):
        raise ValueError("13F sources require the manager's numeric CIK")
    elif kind == "sec13d" and reference != "all":
        raise ValueError("13D reference must be 'all' or an SEC feed URL")
    row = session.scalar(select(ResearchSource).where(
        ResearchSource.user_id == user_id, ResearchSource.kind == kind,
        ResearchSource.reference == reference))
    if row is None:
        row = ResearchSource(user_id=user_id, name=name.strip(), kind=kind,
                             reference=reference, enabled=enabled,
                             cadence_hours=cadence_hours, priority=priority)
        session.add(row)
        session.flush()
    return row


def enqueue(session, source, *, external_id: str, title: str, url: str, body: str,
            published_at: datetime | None = None, author: str = "", now=None):
    now = now or datetime.now(UTC)
    body = body.strip()
    content_hash = digest(body)
    item_id = digest(source.user_id, source.id, external_id, content_hash)
    item = session.get(ResearchItem, item_id)
    if item is not None:
        return item, False
    item = ResearchItem(id=item_id, user_id=source.user_id, source_id=source.id,
                        external_id=external_id, title=title, url=url, body=body,
                        author=author or source.name, published_at=published_at,
                        observed_at=now, content_hash=content_hash, status="queued")
    session.add(item)
    session.flush()
    return item, True


def complete_item(session, item, payload: dict, *, now=None):
    """Keep source claims and fleet judgments distinct; preserve older versions."""
    now = now or datetime.now(UTC)
    if item.status == "analyzed":
        return
    claims = payload.get("claims") or {}
    document_body = payload.pop("_document_body", None)
    if document_body:
        item.body = document_body
        item.content_hash = digest(document_body)
    elif not item.body:
        item.body = json.dumps(claims, sort_keys=True, ensure_ascii=False)
        item.content_hash = digest(item.body)
    synthesis = payload.get("synthesis") or {}
    speaker_calls = claims.get("speaker_calls") or []
    # Legacy extractors sometimes put company names in named_tickers. A mixed-case
    # name is not a security identifier; keep its claim without inventing a ticker.
    declared = {str(t).strip() for t in claims.get("named_tickers", [])
                if re.fullmatch(r"[A-Z]{1,5}(?:[./-][A-Z]{1,3})?", str(t).strip())}
    all_claims = [(c, "ticker") for c in claims.get("claims", [])]
    all_claims += [(c, "market") for c in claims.get("market_outlook_claims", [])]
    # Some extractors emit calls separately from their claim list. Retain them.
    statements = {c.get("statement") for c, _ in all_claims}
    all_claims += [(c, "ticker") for c in speaker_calls if c.get("statement") not in statements]
    for claim, scope in all_claims:
        statement = str(claim.get("statement") or "").strip()
        if not statement:
            continue
        call = next((c for c in speaker_calls if c.get("statement") == statement), {})
        tickers = {symbol(t) for t in claim.get("named_entities", [])}
        tickers |= {symbol(t) for t in claim.get("tickers", [])}
        explicit = symbol(call.get("ticker") or claim.get("ticker"))
        # Entities like CEO/USA are not ticker associations unless the extractor
        # also declared them as named tickers. Explicit speaker calls are retained.
        tickers &= declared
        if explicit:
            tickers.add(explicit)
        tickers.discard(None)
        horizon = call.get("horizon_days") or claim.get("horizon_days")
        horizon = int(horizon) if isinstance(horizon, (int, float)) and 0 < horizon <= 3650 else None
        for ticker in sorted(tickers) or [None]:
            claim_id = digest(item.id, statement, ticker, scope)
            if session.get(ResearchClaim, claim_id):
                continue
            session.add(ResearchClaim(
                id=claim_id, user_id=item.user_id, item_id=item.id, ticker=ticker,
                scope=scope, statement=statement,
                payload_json=json.dumps({**claim, "speaker_call": call}, ensure_ascii=False),
                direction=call.get("direction") or claim.get("direction"), horizon_days=horizon,
                due_at=(utc(item.published_at or item.observed_at) + timedelta(days=horizon)) if horizon else None,
            ))
    item.analysis_json = json.dumps(payload, ensure_ascii=False, default=str)
    item.status = "analyzed"
    item.analyzed_at = now
    item.lease_until = None
    item.error = None
    item.cost_usd = float(payload.get("cost_usd") or 0)
    session.flush()
    # Queue meaningful synthesized questions, not every mentioned ticker.
    reasons = {}
    for rec in synthesis.get("ticker_recommendations", []):
        ticker = symbol(rec.get("ticker"))
        if ticker and rec.get("disposition", "").lower() in {"watch", "buy"}:
            reasons[ticker] = str(rec.get("rationale") or rec.get("next_step") or "New sourced investment thesis")
    portfolio = payload.get("portfolio") or {}
    if synthesis.get("decision_status") == "existing_thesis_recheck" or portfolio.get("relevance") == "high":
        for implication in portfolio.get("implications", []):
            ticker = symbol(implication.get("subject"))
            if ticker and implication.get("relationship") == "held":
                reasons[ticker] = str(implication.get("implication") or implication.get("next_step"))
    for ticker, reason in reasons.items():
        # Cross-posting identical content does not produce a second review request.
        key = digest(item.user_id, ticker, item.content_hash)
        if session.get(ResearchReviewRequest, key) is None:
            session.add(ResearchReviewRequest(id=key, user_id=item.user_id, item_id=item.id,
                                              ticker=ticker, reason=reason[:4000],
                                              created_at=now, updated_at=now))


def list_sources(session, user_id: str) -> list[dict]:
    result = []
    for source in session.scalars(select(ResearchSource).where(ResearchSource.user_id == user_id).order_by(ResearchSource.priority.desc(), ResearchSource.id)):
        items = session.scalars(select(ResearchItem).where(ResearchItem.source_id == source.id)).all()
        ids = [i.id for i in items]
        claims = session.scalars(select(ResearchClaim).where(ResearchClaim.item_id.in_(ids))).all() if ids else []
        uses = session.scalars(select(ResearchEvidenceUse).where(ResearchEvidenceUse.item_id.in_(ids))).all() if ids else []
        requests = session.scalars(select(ResearchReviewRequest).where(ResearchReviewRequest.item_id.in_(ids))).all() if ids else []
        scored = [json.loads(c.outcome_json) for c in claims if c.outcome_json and json.loads(c.outcome_json).get("verdict") in {"correct", "incorrect"}]
        result.append({
            "id": source.id, "name": source.name, "kind": source.kind, "reference": source.reference,
            "enabled": source.enabled, "cadence_hours": source.cadence_hours, "priority": source.priority,
            "last_polled_at": source.last_polled_at, "last_error": source.last_error,
            "stats": {"items_collected": len(items), "items_analyzed": sum(i.status == "analyzed" for i in items),
                      "queued": sum(i.status in {"queued", "processing"} for i in items),
                      "failed": sum(i.status == "failed" for i in items),
                      "tickers": sorted({c.ticker for c in claims if c.ticker}),
                      "claims": len(claims), "evaluated": len(scored),
                      "accuracy": (sum(s["verdict"] == "correct" for s in scored) / len(scored)) if scored else None,
                      "reviews_requested": len(requests), "reviews_triaged": sum(r.state != "pending" for r in requests),
                      "reviews_completed": sum(r.state == "reviewed" for r in requests),
                      "verdict_changes_on_review": sum(bool(json.loads(r.result_json).get("changed_on_review")) for r in requests),
                      "reports_supplied": len(uses), "reports_citing": sum(u.cited for u in uses),
                      "cost_usd": round(sum(i.cost_usd for i in items), 4)},
        })
    return result


def pending_candidates(session, *, user_id, held_tickers, now, limit=5):
    from argosy.services.decision_funnel.stage1_routing import RoutedCandidate
    requests = session.scalars(select(ResearchReviewRequest).where(
        ResearchReviewRequest.user_id == user_id,
        or_(ResearchReviewRequest.state == "pending", (ResearchReviewRequest.state == "ready") & (ResearchReviewRequest.updated_at < now - timedelta(hours=24))),
        ResearchReviewRequest.created_at <= now,
        ResearchReviewRequest.created_at >= now - timedelta(days=30),
    ).order_by(ResearchReviewRequest.created_at).limit(100)).all()
    grouped = {}
    for request in requests:
        item = session.get(ResearchItem, request.item_id)
        if item is None or utc(item.observed_at) > utc(now):
            continue
        if request.ticker not in grouped and len(grouped) >= limit:
            continue
        grouped.setdefault(request.ticker, []).append((request, item))
    return [RoutedCandidate(
        subject=ticker, subject_type="holding" if ticker in held_tickers else "research",
        triggers=["new_research_evidence"], primary_signal="new_research_evidence",
        reason="; ".join(r.reason for r, _ in pairs)[:6000],
        extra={"research_request_ids": [r.id for r, _ in pairs],
               "research_sources": [{"id": "research:" + i.id, "url": i.url, "reason": r.reason} for r, i in pairs],
               "mandate": "General portfolio fit; do not impose the x10 sleeve unless explicitly assigned."},
    ) for ticker, pairs in grouped.items()]


def record_triage(session, candidate, outcome, *, user_id, now):
    for key in candidate.extra.get("research_request_ids", []):
        row = session.get(ResearchReviewRequest, key)
        if row and row.user_id == user_id:
            row.state = "ready" if outcome.warrants_decision else "triaged"
            row.updated_at = now
            row.result_json = json.dumps({"warrants_decision": outcome.warrants_decision, "rationale": outcome.rationale})


def record_review_result(session, candidate, result, *, user_id, now):
    for key in candidate.extra.get("research_request_ids", []):
        row = session.get(ResearchReviewRequest, key)
        if row and row.user_id == user_id:
            payload = json.loads(row.result_json)
            from sqlalchemy import inspect
            from argosy.state.models import DecisionSnapshot
            if inspect(session.connection()).has_table("decision_snapshots"):
                prior = session.scalar(select(DecisionSnapshot).where(DecisionSnapshot.user_id == user_id,
                    DecisionSnapshot.ticker == row.ticker, DecisionSnapshot.created_at < now)
                    .order_by(DecisionSnapshot.created_at.desc()).limit(1))
                if prior:
                    previous_action = json.loads(prior.decision_json).get("action")
                    payload["previous_action"] = previous_action
                    payload["changed_on_review"] = bool(previous_action and result.action and previous_action.lower() != result.action.lower())
            payload.update({"decision_run_id": result.decision_run_id, "proposal_id": result.proposal_id,
                            "action": result.action, "status": result.status, "reason": result.blocked_reason})
            row.result_json = json.dumps(payload)
            row.state = "reviewed"
            row.updated_at = now


def index_youtube(payload, *, user_id, database_url=None):
    """Mirror the existing fleet's artifact without calling the fleet again."""
    from argosy.services.youtube_intelligence import _parse_iso
    video = payload.get("video") or {}
    with research_session(database_url) as session:
        from sqlalchemy import inspect
        if not inspect(session.connection()).has_table("research_sources"):
            return  # upgrade/backfill handles artifacts created by older installs
        from argosy.state.models import YouTubeChannel, YouTubeVideo
        legacy = session.scalar(select(YouTubeVideo).where(YouTubeVideo.user_id == user_id, YouTubeVideo.youtube_video_id == str(video.get("video_id"))))
        channel = session.get(YouTubeChannel, legacy.channel_id) if legacy and legacy.channel_id else None
        source = upsert_source(session, user_id=user_id, name=channel.channel_name if channel else str(video.get("channel") or "YouTube"),
                               kind="youtube", reference=channel.youtube_channel_id if channel else str(video.get("channel") or "unknown"), enabled=bool(channel and channel.enabled))
        # Body contains original extracted excerpts; transcripts stay in file catalog.
        body = json.dumps(payload.get("claims") or {}, sort_keys=True, ensure_ascii=False)
        pending = session.scalar(select(ResearchItem).where(ResearchItem.user_id == user_id,
            ResearchItem.source_id == source.id, ResearchItem.external_id == str(video.get("video_id")),
            ResearchItem.status.in_(["queued", "processing", "failed"])))
        item = pending
        if item is None:
            item, _ = enqueue(session, source, external_id=str(video.get("video_id")),
                          title=str(video.get("title") or "YouTube research"), url=str(video.get("url") or ""),
                          body=body, published_at=legacy.published_at if legacy else _parse_iso(video.get("published_at")),
                          now=utc(legacy.analyzed_at) if legacy else None)
        complete_item(session, item, payload, now=utc(legacy.analyzed_at) if legacy else None)
        session.commit()
