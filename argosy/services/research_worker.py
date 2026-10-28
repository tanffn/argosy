"""Bounded shared ingest queue: poll cheaply, then run the research fleet."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, text

from argosy.services.research_catalog import complete_item, enqueue, research_session, utc
from argosy.state.research_models import ResearchItem, ResearchSource

DAILY_FLEET_LIMIT = 3


def reserved_slots(source):
    """Explicit source capacity, in addition to the shared discovery budget."""
    value = json.loads(source.config_json or "{}").get("reserved_daily_slots", 0)
    return min(1, max(0, value)) if type(value) is int else 0


def daily_fleet_limit(session, *, user_id):
    from argosy.services.research_selection import VIDEO_LIMIT
    return DAILY_FLEET_LIMIT + sum((VIDEO_LIMIT if s.kind == "youtube" else reserved_slots(s)) for s in session.scalars(
        select(ResearchSource).where(ResearchSource.user_id == user_id, ResearchSource.enabled.is_(True))))


async def queue_youtube(*, user_id):
    from argosy.services.youtube_refresh import refresh_channels
    receipts = await refresh_channels(user_id=user_id, channels=["*"])
    return {"sources_checked": sum(r["status"] in {"checked_online", "failed"} for r in receipts),
            "queued": sum(r.get("queued", 0) for r in receipts),
            "failures": [r for r in receipts if r["status"] == "failed"], "receipts": receipts}


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
        from argosy.services.research_selection import day_key
        source_kinds = dict(session.execute(select(ResearchSource.id, ResearchSource.kind).where(ResearchSource.user_id == user_id)).all())
        general_attempts = [i for i in attempts if source_kinds.get(i.source_id) != "youtube"]
        sources_used = Counter(i.source_id for i in general_attempts)
        rows = session.execute(select(ResearchItem, ResearchSource).join(ResearchSource).where(
            ResearchItem.user_id == user_id, ResearchSource.enabled.is_(True),
            ResearchItem.attempts < 3,
            or_(ResearchItem.status.in_(["queued", "failed"]),
                ((ResearchItem.status == "processing") & (ResearchItem.lease_until < now)
                 & func.json_extract(ResearchItem.analysis_json, "$._chat_claim").is_(None))),
            or_(ResearchItem.next_attempt_at.is_(None), ResearchItem.next_attempt_at <= now),
        )).all()
        from argosy.services.research_catalog import automatic_owner_alive
        rows = [(i, s) for i, s in rows if i.status != "processing" or (
            json.loads(i.analysis_json or "{}").get("_auto_owner")
            and not automatic_owner_alive(json.loads(i.analysis_json or "{}")))]
        active_videos = set(session.scalars(select(ResearchItem.external_id).join(ResearchSource).where(
            ResearchItem.user_id == user_id, ResearchSource.user_id == user_id,
            ResearchSource.kind == "youtube", ResearchItem.status == "processing")))
        from argosy.services.research_catalog import video_retry_states
        from argosy.state.models import YouTubeVideo
        completed_videos = set(session.scalars(select(YouTubeVideo.youtube_video_id).where(
            YouTubeVideo.user_id == user_id)))
        completed_videos.update(session.scalars(select(ResearchItem.external_id).join(ResearchSource).where(
            ResearchItem.user_id == user_id, ResearchSource.user_id == user_id,
            ResearchSource.kind == "youtube", ResearchItem.analyzed_at.is_not(None))))
        retries = video_retry_states(session, user_id=user_id)
        for item, source in rows:
            if source.kind == "youtube" and item.external_id in completed_videos and item.status in {"queued", "failed"}:
                item.status = "archived"
                item.error = "Duplicate queue entry: this video already has saved analysis; no repeat fleet run."
        eligible = [(i, s) for i, s in rows if (s.kind == "youtube" or sources_used[s.id] == 0)
                    and i.status != "archived"
                    and (s.kind != "youtube" or (
                        json.loads(i.analysis_json or "{}").get("daily_selection", {}).get("day") == day_key(now)
                        and json.loads(i.analysis_json or "{}").get("daily_selection", {}).get("selected")
                        and utc(i.published_at or i.observed_at) >= now - timedelta(hours=24)))
                    and not (s.kind == "youtube" and (
                        i.external_id in active_videos or i.external_id in completed_videos
                        or retries[i.external_id][0] >= 3
                        or (retries[i.external_id][1] and retries[i.external_id][1] > now)))]
        # Reserved sources cannot consume another source's general slots once
        # the general budget is exhausted. A source still has at most one/day.
        enabled_sources = session.scalars(select(ResearchSource).where(
            ResearchSource.user_id == user_id, ResearchSource.enabled.is_(True))).all()
        reserved_used = sum(min(sources_used[s.id], reserved_slots(s)) for s in enabled_sources)
        if len(general_attempts) - reserved_used >= DAILY_FLEET_LIMIT:
            eligible = [(i, s) for i, s in eligible if s.kind == "youtube" or reserved_slots(s)]
        if not eligible:
            session.commit()  # Retain any archived duplicate queue entries.
            return None
        item, source = max(eligible, key=lambda pair: (rank_item(*pair, now=now), -pair[1].id,
                                                     utc(pair[0].observed_at)))
        item.status = "processing"
        import os

        import psutil
        payload = json.loads(item.analysis_json or "{}")
        token = uuid.uuid4().hex
        payload["_auto_owner"] = {"token": token, "pid": os.getpid(),
                                  "created_at": psutil.Process().create_time()}
        item.analysis_json = json.dumps(payload)
        item.attempts += 1
        item.attempted_at = now
        item.lease_until = now + timedelta(hours=3)
        session.commit()
        return item.id, source.kind, token


async def analyze_document(item, *, user_id):
    from argosy.services.research_images import captured_images
    with captured_images(item) as (images, manifest):
        return await _analyze_document_packet(item, user_id=user_id, images=images, manifest=manifest)


async def _analyze_document_packet(item, *, user_id, images, manifest):
    from argosy.agents.youtube_analysis import (
        YouTubeClaimsAgent,
        YouTubePortfolioAgent,
        YouTubeSkepticAgent,
        YouTubeSynthesisAgent,
        model_json,
    )
    from argosy.services.agent_report_persistence import persist_agent_report_async
    from argosy.services.research_connectors import expand_document
    from argosy.services.youtube_analysis import _portfolio_context
    body = item.body if item.url == "" or item.external_id.startswith(("manual:", "13f:", "browser:")) else await asyncio.to_thread(expand_document, item)
    if item.external_id.startswith("browser:"):
        from argosy.services.alpha_capture import document_context
        body = await asyncio.to_thread(document_context, item, images_supplied=bool(images))
    source_id = "research:" + item.id
    body = ("This is a written research document, not necessarily a video. Use timestamp 00:00:00 when no timestamps exist. "
            "Distinguish reported facts, author claims, and verified evidence. Preserve uncertainties. "
            "Verify security identity before assigning ticker symbols; leave ambiguous names unresolved.\n" + body)
    context = await asyncio.to_thread(_portfolio_context, user_id)
    run_id = "research-analysis:" + item.id
    readers = [YouTubeClaimsAgent(user_id=user_id), YouTubeSkepticAgent(user_id=user_id), YouTubePortfolioAgent(user_id=user_id)]
    reports = []
    assessment_id = uuid.uuid4().hex
    # Persist each completed reader even if a later reader fails.
    results = await asyncio.gather(
        readers[0].run(transcript=body, source_id=source_id, decision_id=run_id, image_attachments=images),
        readers[1].run(transcript=body, source_id=source_id, decision_id=run_id, image_attachments=images),
        readers[2].run(transcript=body, transcript_source_id=source_id, portfolio_context=context,
                       portfolio_source_id=f"argosy:{user_id}:portfolio", decision_id=run_id,
                       image_attachments=images),
        return_exceptions=True)
    save_errors = []
    for result in results:
        if not isinstance(result, BaseException):
            try:
                saved_id = await persist_agent_report_async(result, decision_id=run_id)
                if saved_id is None:
                    raise RuntimeError("Research reader report was not durably saved")
                reports.append(result)
            except Exception as exc:
                save_errors.append(exc)
    if save_errors:
        raise ExceptionGroup("Reader persistence failed; every completed reader was attempted", save_errors)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    claims, skeptic, portfolio = results
    from argosy.services.evidence_review import reviewed_authoring
    async def persist(report):
        saved_id = await persist_agent_report_async(report, decision_id=run_id)
        if saved_id is None:
            raise RuntimeError("Evidence review report was not durably saved")
        reports.append(report)
    async def author(feedback):
        return await YouTubeSynthesisAgent(user_id=user_id).run(
            transcript_source_id=source_id, transcript=body,
            claims_source_id=run_id + ":claims", claims_json=model_json(claims.output),
            skeptic_source_id=run_id + ":skeptic", skeptic_json=model_json(skeptic.output),
            portfolio_source_id=run_id + ":portfolio", portfolio_json=model_json(portfolio.output),
            portfolio_context_source_id=f"argosy:{user_id}:portfolio", portfolio_context=context,
            decision_id=run_id, review_context=feedback, image_attachments=images)
    synthesis, review = await reviewed_authoring(user_id=user_id,
        sources=[(source_id, body), (f"argosy:{user_id}:portfolio", context)],
        context="Research dispositions only, not orders. Cover newly discovered names as well as holdings.",
        author=author, persist=persist, image_attachments=images, decision_id=run_id,
        draft_payload=lambda draft: {
            "claims": (draft.output.reviewed_claims or claims.output).model_dump(mode="json"),
            "portfolio": (draft.output.reviewed_portfolio or portfolio.output).model_dump(mode="json"),
            "synthesis": draft.output.model_dump(mode="json")})
    return {"_document_body": item.body if item.external_id.startswith("browser:") else body,
            "assessment_id": assessment_id,
            "claims": (synthesis.output.reviewed_claims or claims.output).model_dump(mode="json"),
            "skeptic": skeptic.output.model_dump(mode="json"),
            "portfolio": (synthesis.output.reviewed_portfolio or portfolio.output).model_dump(mode="json"),
            "synthesis": synthesis.output.model_dump(mode="json"),
            "evidence_review": review, "image_evidence": manifest,
            "cost_usd": sum(float(r.cost_usd) for r in reports)}


def _finish(item_id, payload, *, user_id, token=None):
    with research_session() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        item = session.get(ResearchItem, item_id)
        if item is None or item.user_id != user_id:
            raise ValueError("Research item not found")
        if token and json.loads(item.analysis_json or "{}").get("_auto_owner", {}).get("token") != token:
            raise ValueError("Automatic research ownership changed; completion was not written")
        state = json.loads(item.analysis_json or '{}')
        stamp = state.get('_auto_completed_at')
        complete_item(session, item, payload, now=datetime.fromisoformat(stamp) if stamp else None)
        state_after = json.loads(item.analysis_json or '{}')
        state_after['_delivery_pending'] = True
        if state.get('_auto_owner'):
            state_after['_auto_owner'] = state['_auto_owner']
        item.analysis_json = json.dumps(state_after)
        session.commit()
        if payload.get('_youtube_delivery_pending'):
            from argosy.services.youtube_analysis import deliver_youtube_result
            deliver_youtube_result(payload, user_id=user_id)
        # Use the established WATCH/BUY router; it only creates reviewable notes.
        from argosy.services.ingest_recommendation_router import route_ingest_recommendations
        recommendations = (payload.get("synthesis") or {}).get("ticker_recommendations") or []
        if session.get(ResearchSource, item.source_id).kind != "youtube":
            route_ingest_recommendations(session, user_id=user_id, source_kind="research",
                                        source_id=item.id, source_url=item.url, recommendations=recommendations,
                                        assessment_id=payload.get("assessment_id"), now=item.analyzed_at,
                                        complete_assessment=bool(payload.get("assessment_id")))
        delivered = json.loads(item.analysis_json or '{}')
        delivered.pop('_delivery_pending', None)
        delivered.pop('_auto_owner', None)
        delivered.pop('_youtube_delivery_pending', None)
        item.analysis_json = json.dumps(delivered)
        item.error = None
        session.commit()


def _stage_result(item_id, token, payload, user_id):
    with research_session() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        item = db.get(ResearchItem, item_id)
        state = json.loads(item.analysis_json or '{}')
        if item.user_id != user_id or state.get('_auto_owner', {}).get('token') != token:
            raise ValueError('Automatic research ownership changed; result not staged')
        state.update(_completed_payload=payload, _auto_completed_at=datetime.now(UTC).isoformat())
        item.analysis_json = json.dumps(state, default=str)
        db.commit()


def _claim_pending_delivery(item_id, *, assessment_id, user_id):
    """CAS claim: a stale recovery snapshot must never replace a newer review."""
    import os
    import psutil
    from argosy.services.research_catalog import automatic_owner_alive
    with research_session() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        item = db.get(ResearchItem, item_id)
        if item is None or item.user_id != user_id:
            return None
        current = json.loads(item.analysis_json or '{}')
        staged = current.get('_completed_payload') or {}
        pending_id = current.get('assessment_id') if current.get('_delivery_pending') else staged.get('assessment_id')
        reclaim_failed = bool(staged and not current.get('_delivery_pending') and item.status in {'failed', 'archived'})
        if (not (current.get('_delivery_pending') or staged) or pending_id != assessment_id
                or (automatic_owner_alive(current) and not reclaim_failed)):
            return None
        token = uuid.uuid4().hex
        current['_auto_owner'] = {'token': token, 'pid': os.getpid(),
                                  'created_at': psutil.Process().create_time()}
        item.analysis_json = json.dumps(current)
        if not current.get('_delivery_pending'):
            item.status = 'processing'
        db.commit()
        return token


def reconcile_completed(*, user_id, details=False):
    from argosy.services.research_catalog import automatic_owner_alive
    with research_session() as db:
        repairs = []
        for item in db.scalars(select(ResearchItem).where(ResearchItem.user_id == user_id,
                or_(ResearchItem.status.in_(['processing', 'failed', 'archived']),
                    func.json_extract(ResearchItem.analysis_json, '$._delivery_pending') == 1))):
            state = json.loads(item.analysis_json or '{}')
            owner = state.get('_auto_owner') or {}
            if item.next_attempt_at and utc(item.next_attempt_at) > datetime.now(UTC):
                continue
            if state.get('_delivery_pending') and (not owner or not automatic_owner_alive(state)):
                repairs.append((item.id, owner.get('token'), state))
            elif owner and state.get('_completed_payload') and (
                    item.status in {'failed', 'archived'} or not automatic_owner_alive(state)):
                repairs.append((item.id, owner['token'], state['_completed_payload']))
    completed, failures = 0, []
    for item_id, token, payload in repairs:
        try:
            token = _claim_pending_delivery(item_id, assessment_id=payload.get('assessment_id'), user_id=user_id)
            if token is None:
                continue
            _finish(item_id, payload, user_id=user_id, token=token)
            completed += 1
        except Exception as exc:
            failures.append({'item_id': item_id, 'error': str(exc)[:300]})
            with research_session() as db:
                db.execute(text('BEGIN IMMEDIATE'))
                item = db.get(ResearchItem, item_id)
                current = json.loads(item.analysis_json or '{}')
                if current.get('_auto_owner', {}).get('token') == token:
                    if current.get('_delivery_pending'):
                        current.pop('_auto_owner', None)
                    else:
                        item.status = 'failed'
                    item.analysis_json = json.dumps(current)
                    item.error = str(exc)[:2000]
                    item.next_attempt_at = datetime.now(UTC) + timedelta(hours=1)
                    db.commit()
    return {'completed': completed, 'failures': failures} if details else completed


def _record_claim_failure(item_id, token, error):
    with research_session() as session:
        session.execute(text('BEGIN IMMEDIATE'))
        item = session.get(ResearchItem, item_id)
        owner = json.loads(item.analysis_json or '{}').get('_auto_owner', {})
        if owner.get('token') != token:
            return {'item_id': item_id, 'error': 'Automatic research ownership changed'}
        if not item.analyzed_at:
            item.status = 'failed'
        elif item.status == 'processing':
            capture = json.loads(item.analysis_json or '{}').get('capture') or {}
            item.status = ('analyzed_partial' if capture.get('images') and
                           capture.get('image_analysis') != 'reviewed' else 'analyzed')
        item.error = error[:2000]
        failed_state = json.loads(item.analysis_json or '{}')
        if failed_state.get('_delivery_pending'):
            failed_state.pop('_auto_owner', None)
            item.analysis_json = json.dumps(failed_state)
        item.lease_until = None
        item.next_attempt_at = datetime.now(UTC) + timedelta(hours=24)
        session.commit()
    return {'item_id': item_id, 'error': error[:300]}


async def _process_claim(claimed, *, user_id, analyzer):
    item_id, kind, token = claimed
    try:
        with research_session() as session:
            item = session.get(ResearchItem, item_id)
            session.expunge(item)
        async def stage(payload):
            await asyncio.to_thread(_stage_result, item_id, token, payload, user_id)
        if kind == 'youtube':
            from argosy.services.youtube_analysis import analyze_youtube, result_to_dict
            payload = result_to_dict(await analyze_youtube(item.external_id, user_id=user_id,
                                                         save=True, on_completed=stage))
        else:
            payload = await analyzer(item, user_id=user_id)
            await stage(payload)
        await asyncio.to_thread(_finish, item_id, payload, user_id=user_id, token=token)
        return None
    except Exception as exc:
        return await asyncio.to_thread(_record_claim_failure, item_id, token, str(exc))


async def await_owned(work):
    """Cancellation stops subsequent work, not in-flight provider/persistence ownership."""
    task = asyncio.create_task(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        raise


async def _process_next(*, user_id, analyzer):
    # The claim thread itself must be owned: cancellation can arrive after its commit.
    claimed = await asyncio.to_thread(claim_next, user_id=user_id)
    if claimed is None:
        return None, None
    return claimed, await _process_claim(claimed, user_id=user_id, analyzer=analyzer)


async def process_queue(*, user_id, analyzer=analyze_document):
    from argosy.orchestrator.cost_guard import get_cost_guard
    results = {"analyzed": 0, "videos_ingested": 0, "failures": []}
    with research_session() as session:
        batch_limit = daily_fleet_limit(session, user_id=user_id)
    for _ in range(batch_limit):
        if await get_cost_guard(user_id=user_id).should_pause_non_routine(loop_name="research_inputs"):
            results["paused"] = "cost_cap"
            break
        claimed, failure = await await_owned(_process_next(user_id=user_id, analyzer=analyzer))
        if claimed is None:
            break
        # Keep the owner alive until outstanding fetch/persistence threads finish.
        # Cancellation stops the batch, never abandons a running video in-process.
        if failure is None:
            results["analyzed"] += 1
            results['videos_ingested'] += int(claimed[1] == 'youtube')
        else:
            results['failures'].append(failure)
    return results


def source_due(source, now):
    if source.last_polled_at is None:
        return True
    previous = utc(source.last_polled_at)
    if source.cadence_hours % 24 == 0:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("Asia/Jerusalem")
        return (now.astimezone(zone).date() - previous.astimezone(zone).date()).days >= source.cadence_hours // 24
    return now - previous >= timedelta(hours=source.cadence_hours)


def _persist_polled_source(source, entries, now):
    # All writer waits belong to a worker thread: blocking the asyncio loop
    # can prevent the current async writer from reaching its own commit.
    collected = 0
    with research_session() as session:
        for entry in entries:
            published = utc(entry.get("published_at"))
            if published and not now - timedelta(days=90) <= published <= now:
                continue
            _, created = enqueue(session, source, **entry, now=now)
            collected += int(created)
        live = session.get(ResearchSource, source.id)
        live.last_polled_at = now
        live.last_error = None
        session.commit()
    return collected


def _record_poll_failure(source_id, error):
    with research_session() as session:
        live = session.get(ResearchSource, source_id)
        live.last_error = error[:1000]
        session.commit()


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
        if not source_due(source, now):
            continue
        result["sources_checked"] += 1
        try:
            entries = await collect_source(source)
            result["collected"] += await await_owned(asyncio.to_thread(
                _persist_polled_source, source, entries, now,
            ))
        except Exception as exc:
            await await_owned(asyncio.to_thread(_record_poll_failure, source.id, str(exc)))
            result["failures"].append({"source": source.name, "error": str(exc)[:300]})
    return result


async def sync_research(*, user_id="ariel"):
    delivery = await asyncio.to_thread(reconcile_completed, user_id=user_id, details=True)
    from argosy.services.research_catalog import index_existing_events
    await asyncio.to_thread(index_existing_events, user_id=user_id)
    polled = await poll_sources(user_id=user_id)
    from argosy.services.research_selection import select_daily_videos
    selection = await select_daily_videos(user_id=user_id)
    processed = await process_queue(user_id=user_id)
    from argosy.services.research_impact import evaluate_due_claims, index_evidence_uses
    uses = await asyncio.to_thread(index_evidence_uses, user_id=user_id)
    outcomes = await asyncio.to_thread(evaluate_due_claims, user_id=user_id)
    return {**polled, **processed, "delivery_recovery": delivery, "video_selection": selection, "failures": delivery['failures'] + polled["failures"] + selection["failures"] + processed["failures"],
            "usage_receipts": uses, "claims_evaluated": outcomes}
