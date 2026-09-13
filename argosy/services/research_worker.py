"""Bounded shared ingest queue: poll cheaply, then run the research fleet."""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import json
import re

from sqlalchemy import func, or_, select, text

from argosy.services.research_catalog import research_session, enqueue, complete_item, utc
from argosy.state.research_models import ResearchSource, ResearchItem

DAILY_FLEET_LIMIT = 3


async def queue_youtube(*, user_id):
    from argosy.services.youtube_intelligence import list_youtube_sources, _fetch_feed, _unseen, _update_poll, _parse_iso, _video_ingested
    from argosy.services.research_catalog import upsert_source
    result = {"sources_checked": 0, "queued": 0, "failures": []}
    for source in list_youtube_sources(user_id=user_id)["sources"]:
        with research_session() as session:
            mirror = upsert_source(session, user_id=user_id, name=source["channel_name"], kind="youtube",
                                   reference=source["youtube_channel_id"], enabled=source["enabled"])
            mirror.enabled = source["enabled"]
            session.commit()
        if not source["enabled"]:
            continue
        result["sources_checked"] += 1
        try:
            feed = await asyncio.to_thread(_fetch_feed, source["youtube_channel_id"])
            unseen = _unseen(feed, source.get("last_seen_video_id"))
            with research_session() as session:
                for item in unseen:
                    if _video_ingested(user_id, item["video_id"]):
                        continue
                    _, created = enqueue(session, mirror, external_id=item["video_id"], title=item.get("title") or item["video_id"],
                                         url="https://www.youtube.com/watch?v=" + item["video_id"], body="",
                                         published_at=_parse_iso(item.get("published_at")))
                    result["queued"] += int(created)
                session.commit()
            # Cursor advances only after every discovered upload is durable.
            _update_poll(source["id"], user_id, feed[0] if feed else None, None)
        except Exception as exc:
            _update_poll(source["id"], user_id, None, str(exc)[:1000])
            result["failures"].append({"source": source["channel_name"], "error": str(exc)[:300]})
    return result


def rank_item(item, source, *, now):
    title = item.title.lower()
    score = source.priority
    if re.search(r"earnings|guidance|thesis|undervalued|stock|shares|13d|13f|position|margin|cash flow", title):
        score += 20
    if re.search(r"macro|outlook|tariff|inflation|rates|recession|liquidity", title):
        score += 15
    if re.search(r"lecture|course|lesson|beginners|tutorial", title):
        score -= 25
    # Aging prevents lower-priority sources from starving indefinitely.
    score += min(30, max(0, (now - utc(item.observed_at)).days))
    return score


def claim_next(*, user_id, now=None):
    now = now or datetime.now(UTC)
    from zoneinfo import ZoneInfo
    start = now.astimezone(ZoneInfo("Asia/Jerusalem")).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    with research_session() as session:
        # Serialize selection and budget charging across manual/scheduled callers.
        session.execute(text("BEGIN IMMEDIATE"))
        attempts = session.scalars(select(ResearchItem).where(
            ResearchItem.user_id == user_id, ResearchItem.attempted_at >= start)).all()
        if len(attempts) >= DAILY_FLEET_LIMIT:
            return None
        sources_used = Counter(i.source_id for i in attempts)
        rows = session.execute(select(ResearchItem, ResearchSource).join(ResearchSource).where(
            ResearchItem.user_id == user_id, ResearchSource.enabled.is_(True),
            ResearchItem.attempts < 3,
            or_(ResearchItem.status.in_(["queued", "failed"]),
                (ResearchItem.status == "processing") & (ResearchItem.lease_until < now)),
            or_(ResearchItem.next_attempt_at.is_(None), ResearchItem.next_attempt_at <= now),
        )).all()
        eligible = [(i, s) for i, s in rows if sources_used[s.id] == 0]
        if not eligible:
            return None
        item, source = max(eligible, key=lambda pair: (rank_item(*pair, now=now), -pair[1].id))
        item.status = "processing"
        item.attempts += 1
        item.attempted_at = now
        item.lease_until = now + timedelta(hours=3)
        session.commit()
        return item.id, source.kind


async def analyze_document(item, *, user_id):
    from argosy.agents.youtube_analysis import YouTubeClaimsAgent, YouTubeSkepticAgent, YouTubePortfolioAgent, YouTubeSynthesisAgent, model_json
    from argosy.services.youtube_analysis import _portfolio_context
    from argosy.services.agent_report_persistence import persist_agent_report_async
    from argosy.services.research_connectors import expand_document
    body = item.body if item.url == "" or item.external_id.startswith(("manual:", "13f:")) else await asyncio.to_thread(expand_document, item)
    source_id = "research:" + item.id
    body = ("This is a written research document, not necessarily a video. Use timestamp 00:00:00 when no timestamps exist. "
            "Distinguish reported facts, author claims, and verified evidence. Preserve uncertainties. "
            "Verify security identity before assigning ticker symbols; leave ambiguous names unresolved.\n" + body)
    context = await asyncio.to_thread(_portfolio_context, user_id)
    run_id = "research-analysis:" + item.id
    readers = [YouTubeClaimsAgent(user_id=user_id), YouTubeSkepticAgent(user_id=user_id), YouTubePortfolioAgent(user_id=user_id)]
    reports = []
    # Persist each completed reader even if a later reader fails.
    results = await asyncio.gather(
        readers[0].run(transcript=body, source_id=source_id, decision_id=run_id),
        readers[1].run(transcript=body, source_id=source_id, decision_id=run_id),
        readers[2].run(transcript=body, transcript_source_id=source_id, portfolio_context=context,
                       portfolio_source_id=f"argosy:{user_id}:portfolio", decision_id=run_id),
        return_exceptions=True)
    for result in results:
        if not isinstance(result, BaseException):
            await persist_agent_report_async(result, decision_id=run_id)
            reports.append(result)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    claims, skeptic, portfolio = results
    synthesis = await YouTubeSynthesisAgent(user_id=user_id).run(
        transcript_source_id=source_id, transcript=body,
        claims_source_id=run_id + ":claims", claims_json=model_json(claims.output),
        skeptic_source_id=run_id + ":skeptic", skeptic_json=model_json(skeptic.output),
        portfolio_source_id=run_id + ":portfolio", portfolio_json=model_json(portfolio.output), decision_id=run_id)
    await persist_agent_report_async(synthesis, decision_id=run_id)
    reports.append(synthesis)
    return {"_document_body": body, "claims": claims.output.model_dump(mode="json"), "skeptic": skeptic.output.model_dump(mode="json"),
            "portfolio": portfolio.output.model_dump(mode="json"), "synthesis": synthesis.output.model_dump(mode="json"),
            "cost_usd": sum(float(r.cost_usd) for r in reports)}


def _finish(item_id, payload, *, user_id):
    with research_session() as session:
        item = session.get(ResearchItem, item_id)
        if item is None or item.user_id != user_id:
            raise ValueError("Research item not found")
        complete_item(session, item, payload)
        session.commit()
        if session.get(ResearchSource, item.source_id).kind == "youtube":
            return  # analyze_youtube already performs the established routing.
        # Use the established WATCH/BUY router; it only creates reviewable notes.
        from argosy.services.ingest_recommendation_router import route_ingest_recommendations
        recommendations = (payload.get("synthesis") or {}).get("ticker_recommendations") or []
        route_ingest_recommendations(session, user_id=user_id, source_kind="research",
                                    source_id=item.id, source_url=item.url, recommendations=recommendations)
        item.error = None
        session.commit()


async def process_queue(*, user_id, analyzer=analyze_document):
    from argosy.orchestrator.cost_guard import get_cost_guard
    results = {"analyzed": 0, "videos_ingested": 0, "failures": []}
    for _ in range(DAILY_FLEET_LIMIT):
        if await get_cost_guard(user_id=user_id).should_pause_non_routine(loop_name="research_inputs"):
            results["paused"] = "cost_cap"
            break
        claimed = await asyncio.to_thread(claim_next, user_id=user_id)
        if claimed is None:
            break
        item_id, kind = claimed
        try:
            with research_session() as session:
                item = session.get(ResearchItem, item_id)
                session.expunge(item)
            if kind == "youtube":
                from argosy.services.youtube_analysis import analyze_youtube, result_to_dict
                payload = result_to_dict(await analyze_youtube(item.external_id, user_id=user_id, save=True))
            else:
                payload = await analyzer(item, user_id=user_id)
            await asyncio.to_thread(_finish, item_id, payload, user_id=user_id)
            results["analyzed"] += 1
            results["videos_ingested"] += int(kind == "youtube")
        except Exception as exc:
            with research_session() as session:
                item = session.get(ResearchItem, item_id)
                # Preserve completed analysis if only downstream routing failed.
                item.status = "analyzed" if item.analyzed_at else "failed"
                item.error = str(exc)[:2000]
                item.lease_until = None
                item.next_attempt_at = datetime.now(UTC) + timedelta(hours=24)
                session.commit()
            results["failures"].append({"item_id": item_id, "error": str(exc)[:300]})
    return results


async def poll_sources(*, user_id):
    from argosy.services.research_connectors import collect_source
    now = datetime.now(UTC)
    result = {"sources_checked": 0, "collected": 0, "failures": []}
    with research_session() as session:
        sources = session.scalars(select(ResearchSource).where(
            ResearchSource.user_id == user_id, ResearchSource.enabled.is_(True),
            ResearchSource.kind.in_(["rss", "sec13d", "sec13f"]))).all()
        for source in sources:
            session.expunge(source)
    for source in sources:
        if source.last_polled_at and now - utc(source.last_polled_at) < timedelta(hours=source.cadence_hours):
            continue
        result["sources_checked"] += 1
        try:
            entries = await collect_source(source)
            with research_session() as session:
                for entry in entries:
                    published = utc(entry.get("published_at"))
                    if published and not now - timedelta(days=90) <= published <= now:
                        continue
                    _, created = enqueue(session, source, **entry, now=now)
                    result["collected"] += int(created)
                live = session.get(ResearchSource, source.id)
                live.last_polled_at = now
                live.last_error = None
                session.commit()
        except Exception as exc:
            with research_session() as session:
                live = session.get(ResearchSource, source.id)
                live.last_error = str(exc)[:1000]
                session.commit()
            result["failures"].append({"source": source.name, "error": str(exc)[:300]})
    return result


async def sync_research(*, user_id="ariel"):
    from argosy.services.research_catalog import index_existing_events
    await asyncio.to_thread(index_existing_events, user_id=user_id)
    # Retry action delivery from saved analysis without spending on another fleet.
    with research_session() as session:
        repairs = [(i.id, json.loads(i.analysis_json)) for i in session.scalars(select(ResearchItem).where(
            ResearchItem.user_id == user_id, ResearchItem.status == "analyzed", ResearchItem.error.is_not(None)).limit(20))]
    for item_id, payload in repairs:
        await asyncio.to_thread(_finish, item_id, payload, user_id=user_id)
    polled = await poll_sources(user_id=user_id)
    processed = await process_queue(user_id=user_id)
    from argosy.services.research_impact import index_evidence_uses, evaluate_due_claims
    uses = await asyncio.to_thread(index_evidence_uses, user_id=user_id)
    outcomes = await asyncio.to_thread(evaluate_due_claims, user_id=user_id)
    return {**polled, **processed, "failures": polled["failures"] + processed["failures"],
            "usage_receipts": uses, "claims_evaluated": outcomes}
