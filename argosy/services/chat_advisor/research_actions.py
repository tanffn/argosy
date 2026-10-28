"""Explicit, bounded Discord research tasks reusing the existing research pipeline."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from .progress import record_agent_progress


class ResearchChatTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["youtube_video", "youtube_follow", "alpha_capture"]
    reference: str = Field(min_length=1, max_length=2048)
    schema_version: Literal[1] = 1


def canonical_reference(kind, reference):
    if kind == "alpha_capture":
        if reference != "configured":
            raise ValueError("Alpha capture uses only the configured source; no external target override")
        return reference
    from argosy.services.youtube_transcript import extract_video_id

    if kind == "youtube_video":
        return "https://www.youtube.com/watch?v=" + extract_video_id(reference)
    if kind != "youtube_follow":
        raise ValueError("Unsupported research action")
    url = urlsplit(reference)
    if (
        url.scheme != "https"
        or url.hostname not in {"youtube.com", "www.youtube.com"}
        or url.username
        or url.password
        or url.port
        or url.query
        or url.fragment
    ):
        raise ValueError(
            "Send an HTTPS YouTube channel link, such as https://www.youtube.com/@channel"
        )
    if not re.fullmatch(r"/(?:@[\w.-]+|channel/UC[\w-]{22})(?:/(?:videos|streams))?/?", url.path):
        raise ValueError("Send a YouTube @handle or /channel/ link")
    return "https://www.youtube.com" + url.path.rstrip("/")


async def execute_research_task(task: ResearchChatTask, *, user_id: str):
    reference = canonical_reference(task.kind, task.reference)
    if task.kind == "alpha_capture":
        from argosy.services.jobs.alpha_capture_daily import configuration
        from scripts.run_alpha_capture import run_registered
        config = configuration()
        if not config.get("enabled") or config.get("user_id") != user_id:
            raise ValueError("Alpha capture is not enabled for this household")
        await record_agent_progress(agent="alpha_capture", state="started",
            detail="Opening the configured Alpha page; checking sign-in and saving the report", correlation_id="alpha_capture")
        receipt = await run_registered(explicit_retry=True)
        result = receipt.get("result") or {}
        if receipt.get("job_status") in {"error", "cancelled"}:
            raise ValueError("Alpha capture did not finish; the job history contains the failure details.")
        if result.get("status") != "captured":
            return {"research_task": task.kind, "text": result.get("reason") or
                    "Alpha capture hasn't completed. No new report import is confirmed.", "citations": [],
                    "job_run_id": receipt["job_run_id"], "capture_status": result.get("status")}
        capture = result.get("capture") or {}
        review = result.get("research_queue") or {}
        return {"research_task": task.kind, "text":
                f"Alpha captured and imported: latest report {capture.get('latest_report_date', 'date unavailable')}; "
                f"{capture.get('queued', 0)} new/revised item(s) queued. "
                + ("Some research reviews need retry; importing the page does not mean all reviews succeeded."
                   if result.get("failures") or review.get("failures") else "Saved reports are available for follow-up."),
                "citations": [], "job_run_id": receipt["job_run_id"], "capture": capture}
    if task.kind == "youtube_follow":
        from argosy.services.youtube_intelligence import subscribe_youtube_source

        await record_agent_progress(
            agent="subscription",
            state="started",
            detail="Resolving the channel and saving your subscription",
            correlation_id="subscription",
        )
        source = await asyncio.to_thread(subscribe_youtube_source, reference, user_id=user_id)
        return {
            "research_task": task.kind,
            "text": f"Following {source['channel_name']}. New uploads will enter the scheduled research queue; not every upload is automatically analyzed.",
            "citations": [],
        }
    return await _analyze_video(reference, user_id=user_id)


def _reuse_video(db, user_id, video_id):
    """Reconcile only a durably staged successful result; never infer it from time."""
    from argosy.services.research_catalog import complete_item
    from argosy.state.models import YouTubeVideo
    from argosy.state.research_models import ResearchItem

    saved = db.scalar(
        select(YouTubeVideo).where(
            YouTubeVideo.user_id == user_id, YouTubeVideo.youtube_video_id == video_id
        )
    )
    items = db.scalars(
        select(ResearchItem)
        .where(ResearchItem.user_id == user_id, ResearchItem.external_id == video_id)
        .order_by(ResearchItem.observed_at.desc())
    ).all()
    for item in items:
        state = json.loads(item.analysis_json or "{}")
        if not state.get("_chat_claim") or item.status not in {"processing", "failed"}:
            continue
        completed = state.get("_completed_payload")
        if (
            isinstance(completed, dict)
            and completed.get("run_id")
            and (completed.get("video") or {}).get("video_id") == video_id
        ):
            complete_item(db, item, completed)
        elif state.get('_chat_owner'):
            from argosy.services.research_catalog import automatic_owner_alive
            if not automatic_owner_alive({'_auto_owner': state['_chat_owner']}) and item.status == 'processing':
                item.status = 'failed'
                item.lease_until = None
                item.error = 'Explicit video research was interrupted before a recoverable result; retry cooldown applies.'
                item.next_attempt_at = datetime.now(UTC) + timedelta(hours=24)
            elif item.status == 'processing':
                raise ValueError('This video has an active or interrupted research claim; saved catalog data alone cannot confirm ledger completion. No duplicate review started.')
        elif item.status == "processing" or saved:
            raise ValueError(
                "This video has an active or interrupted research claim; saved catalog data alone cannot confirm ledger completion. No duplicate review started."
            )
    db.flush()
    done = next((item for item in items if item.analyzed_at), None)
    if not done and not saved:
        return None
    title = done.title if done else saved.title
    record_type = "research_item" if done else "youtube_video"
    record_id = done.id if done else str(saved.id)
    stamp = done.analyzed_at if done else saved.analyzed_at
    url = done.url if done else saved.url
    return {
        "research_task": "youtube_video",
        "text": f"Already analyzed: {title or video_id}. The saved review is available for follow-up.",
        "citations": [
            {
                "record_type": record_type,
                "record_id": record_id,
                "as_of": stamp.isoformat(),
                "url": url,
            }
        ],
    }


def _claim_video(reference, *, user_id, allow_new=True):
    from argosy.services.research_catalog import (
        enqueue,
        research_session,
        upsert_source,
        utc,
        video_retry_states,
    )
    from argosy.services.youtube_intelligence import resolve_youtube_metadata
    from argosy.services.youtube_transcript import extract_video_id
    from argosy.state.research_models import ResearchItem

    video_id = extract_video_id(reference)
    # Fast reuse, including earlier direct imports. No repeat model charge.
    with research_session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        reused = _reuse_video(db, user_id, video_id)
        db.commit()
        if reused:
            return None, reused
    metadata = resolve_youtube_metadata(reference)
    with research_session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        reused = _reuse_video(db, user_id, video_id)
        if reused:
            db.commit()
            return None, reused
        active = db.scalar(
            select(ResearchItem.id).where(
                ResearchItem.user_id == user_id,
                ResearchItem.external_id == video_id,
                ResearchItem.status == "processing",
            )
        )
        if active:
            raise ValueError(
                "This video already has an active or interrupted research claim; no duplicate analysis was started"
            )
        attempts, next_at = video_retry_states(db, user_id=user_id, video_id=video_id).get(
            video_id, (0, None)
        )
        if attempts >= 3:
            raise ValueError(
                "This video has exhausted three attempts across its saved versions; resolve its ingestion error before retry"
            )
        if next_at and next_at > datetime.now(UTC):
            raise ValueError(
                f"This video is in retry cooldown until {next_at}; no duplicate attempt started"
            )
        source = upsert_source(
            db,
            user_id=user_id,
            name=metadata.channel_name,
            kind="youtube",
            reference=metadata.channel_id,
            enabled=False,
        )
        item = db.scalar(
            select(ResearchItem)
            .where(
                ResearchItem.user_id == user_id,
                ResearchItem.source_id == source.id,
                ResearchItem.external_id == video_id,
            )
            .order_by(ResearchItem.observed_at.desc())
            .limit(1)
        )
        if item is None:
            item, _ = enqueue(
                db,
                source,
                external_id=video_id,
                title=metadata.title or video_id,
                url=reference,
                body="",
                published_at=metadata.published_at,
            )
        now = datetime.now(UTC)
        if item.analyzed_at:
            return None, {
                "research_task": "youtube_video",
                "text": f"Already analyzed: {item.title}. The saved review is available for follow-up.",
                "citations": [
                    {
                        "record_type": "research_item",
                        "record_id": item.id,
                        "as_of": item.analyzed_at.isoformat(),
                        "url": item.url,
                    }
                ],
            }
        if not allow_new:
            raise ValueError("Video analysis is paused by the configured spend limit")
        if item.status == "processing":
            raise ValueError(
                "This video already has an active or interrupted research claim; no duplicate analysis was started"
            )
        if item.attempts >= 3:
            raise ValueError(
                "This video has exhausted three attempts; its recorded ingestion error needs resolution before retry"
            )
        if item.next_attempt_at and utc(item.next_attempt_at) > now:
            raise ValueError(
                f"This video is in retry cooldown until {item.next_attempt_at}; no duplicate attempt started"
            )
        item.status = "processing"
        item.attempts += 1
        item.attempted_at = now
        item.lease_until = now + timedelta(hours=3)
        token = uuid.uuid4().hex
        payload = json.loads(item.analysis_json or "{}")
        payload["_chat_claim"] = token
        import os

        import psutil
        payload['_chat_owner'] = {'pid': os.getpid(), 'created_at': psutil.Process().create_time()}
        item.analysis_json = json.dumps(payload)
        db.commit()
        return (item.id, token), None


def _failed_video(claim, user_id, error):
    from argosy.services.research_catalog import research_session
    from argosy.state.research_models import ResearchItem

    with research_session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        item_id, token = claim
        item = db.get(ResearchItem, item_id)
        if (
            item
            and item.user_id == user_id
            and item.status == "processing"
            and json.loads(item.analysis_json or "{}").get("_chat_claim") == token
        ):
            if not item.analyzed_at and not json.loads(item.analysis_json or "{}").get(
                "_completed_payload"
            ):
                item.status = "failed"
            item.error = str(error)[:2000]
            item.lease_until = None
            item.next_attempt_at = datetime.now(UTC) + timedelta(hours=24)
            db.commit()


def _stage_video_result(claim, user_id, payload):
    from argosy.services.research_catalog import research_session
    from argosy.state.research_models import ResearchItem

    with research_session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        item = db.get(ResearchItem, claim[0])
        if (
            not item
            or item.user_id != user_id
            or item.status != "processing"
            or json.loads(item.analysis_json or "{}").get("_chat_claim") != claim[1]
        ):
            raise ValueError("Research claim ownership changed; successful result was not staged")
        item.analysis_json = json.dumps(
            {**json.loads(item.analysis_json or "{}"),
             "_chat_claim": claim[1], "_completed_payload": payload}, default=str
        )
        db.commit()


def _finish_video(claim, user_id, payload):
    from argosy.services.research_catalog import complete_item, research_session
    from argosy.state.research_models import ResearchItem

    with research_session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        item_id, token = claim
        item = db.get(ResearchItem, item_id)
        if (
            item
            and item.user_id == user_id
            and item.analyzed_at
            and json.loads(item.analysis_json or "{}").get("run_id") == payload.get("run_id")
            and payload.get("run_id")
        ):
            return  # A concurrent request reconciled this exact staged result.
        if (
            not item
            or item.user_id != user_id
            or item.status != "processing"
            or json.loads(item.analysis_json or "{}").get("_chat_claim") != token
        ):
            raise ValueError("Research claim ownership changed; completion was not written")
        # analyze_youtube already routed its reviewable notes; use the shared
        # ledger writer, not a second recommendation-routing pass.
        complete_item(db, item, payload)
        db.commit()


async def _analyze_video(reference, *, user_id):
    from argosy.services.research_worker import await_owned
    return await await_owned(_analyze_video_owned(reference, user_id=user_id))


async def _analyze_video_owned(reference, *, user_id):
    from argosy.orchestrator.cost_guard import get_cost_guard
    from argosy.services.youtube_analysis import analyze_youtube, result_to_dict

    # Explicit requests bypass autonomous daily selection limits, NOT the spend cap.
    paused = await get_cost_guard(user_id=user_id).should_pause_non_routine(
        loop_name="research_inputs"
    )
    await record_agent_progress(
        agent="video_intake",
        state="started",
        detail="Checking saved analysis and claiming this video",
        correlation_id="video_intake",
    )
    claim, reused = await asyncio.to_thread(
        _claim_video, reference, user_id=user_id, allow_new=not paused
    )
    if reused:
        return reused

    async def owned_analysis():
        try:
            await record_agent_progress(
                agent="transcript",
                state="started",
                detail="Fetching and cataloging captions; then deploying the research team",
                correlation_id="transcript",
            )
            async def stage(payload):
                await asyncio.to_thread(_stage_video_result, claim, user_id, payload)
            payload = result_to_dict(await analyze_youtube(reference, user_id=user_id, save=True, on_completed=stage))
            await asyncio.to_thread(_finish_video, claim, user_id, payload)
        except Exception as exc:
            await asyncio.to_thread(_failed_video, claim, user_id, exc)
            raise
    await owned_analysis()
    item_id = claim[0]
    try:
        from .contracts import Principal
        from .conversation import GroundedAnswerAgent
        from .read_team import AnswerCoverageAgent, ReadExecutor, ReadRequest, answer_with_review
        from .retrieval import RetrievalService

        executor = ReadExecutor(
            RetrievalService(), Principal(user_id, "", "", "", provider="internal")
        )
        await executor.read(ReadRequest(topic="research", filters={"record_id": item_id}))
        answer = await answer_with_review(
            executor=executor,
            question="Summarize this video's saved analysis in at most 100 words: the author's important claims, Argosy's assessment and portfolio relevance. Attribute claims; no new trade advice.",
            history="",
            hints="Completed video research, not an instruction to run more analysis.",
            answerer=GroundedAnswerAgent(user_id=user_id),
            reviewer=AnswerCoverageAgent(user_id=user_id),
        )
        summary = answer.text
    except Exception:
        summary = "Video analysis saved, but its short chat summary is unavailable. The detailed review remains available."
    return {
        "research_task": "youtube_video",
        "text": summary,
        "citations": [
            {
                "record_type": "research_item",
                "record_id": item_id,
                "as_of": datetime.now(UTC).isoformat(),
                "url": reference,
            }
        ],
    }
