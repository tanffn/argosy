"""Read saved subscription coverage, without implying a complete live upload census."""

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from argosy.services.chat_advisor.contracts import Citation, ReadResult
from argosy.state.models import YouTubeChannel, YouTubeVideo
from argosy.state.research_models import ResearchItem, ResearchSource, ResearchReviewRequest


def followthrough(db, user_id, item_ids):
    result = {}
    for request in db.scalars(select(ResearchReviewRequest).where(
        ResearchReviewRequest.user_id == user_id, ResearchReviewRequest.item_id.in_(list(item_ids))
    ).order_by(ResearchReviewRequest.updated_at.desc()).limit(100)):
        result.setdefault(request.item_id, []).append({
            "ticker": request.ticker, "state": request.state, "updated_at": request.updated_at,
            "result": json.loads(request.result_json or "{}"),
        })
    return result


def read_subscriptions(db, user_id, *, limit, offset, channel="", day="", kind="", **_):
    zone = ZoneInfo("Asia/Jerusalem")
    now = datetime.now(UTC)
    selected = (
        now.astimezone(zone).date()
        if day in ("", "today")
        else now.astimezone(zone).date() - timedelta(days=1)
        if day == "yesterday"
        else date.fromisoformat(day)
    )
    start = datetime.combine(selected, datetime.min.time(), zone).astimezone(UTC)
    end = datetime.combine(selected + timedelta(days=1), datetime.min.time(), zone).astimezone(UTC)
    sources = select(ResearchSource).where(ResearchSource.user_id == user_id)
    channels = select(YouTubeChannel).where(YouTubeChannel.user_id == user_id)
    if kind:
        if kind != "youtube":
            raise ValueError(
                "Subscription kind filter supports youtube only; omit for all research sources"
            )
        sources = sources.where(ResearchSource.kind == "youtube")
    if channel:
        sources = sources.where(ResearchSource.name.contains(channel, autoescape=True))
        channels = channels.where(YouTubeChannel.channel_name.contains(channel, autoescape=True))
    source_ids = sources.with_only_columns(ResearchSource.id)
    channel_ids = channels.with_only_columns(YouTubeChannel.id)
    base = select(ResearchItem).where(
        ResearchItem.user_id == user_id, ResearchItem.source_id.in_(source_ids)
    )
    counts = dict(
        db.execute(
            select(ResearchItem.status, func.count())
            .where(ResearchItem.user_id == user_id, ResearchItem.source_id.in_(source_ids))
            .group_by(ResearchItem.status)
        ).all()
    )
    activity = base.where(
        or_(
            (ResearchItem.observed_at >= start) & (ResearchItem.observed_at < end),
            (ResearchItem.attempted_at >= start) & (ResearchItem.attempted_at < end),
            (ResearchItem.analyzed_at >= start) & (ResearchItem.analyzed_at < end),
        )
    )
    activity_total = db.scalar(select(func.count()).select_from(activity.subquery())) or 0
    rows = db.scalars(
        activity.order_by(
            func.coalesce(
                ResearchItem.analyzed_at, ResearchItem.attempted_at, ResearchItem.observed_at
            ).desc(),
            ResearchItem.id,
        )
        .offset(offset)
        .limit(limit)
    ).all()
    # Include completed videos from direct CLI/chat imports, not just the queue.
    videos = select(YouTubeVideo).where(
        YouTubeVideo.user_id == user_id,
        YouTubeVideo.analyzed_at >= start,
        YouTubeVideo.analyzed_at < end,
    )
    if channel:
        videos = videos.where(YouTubeVideo.channel_id.in_(channel_ids))
    video_total = db.scalar(select(func.count()).select_from(videos.subquery())) or 0
    done = db.scalars(
        videos.order_by(YouTubeVideo.analyzed_at.desc(), YouTubeVideo.id)
        .offset(offset)
        .limit(limit)
    ).all()
    pending = db.scalars(
        base.where(ResearchItem.status.in_(["queued", "failed", "processing"]))
        .order_by(ResearchItem.observed_at.desc(), ResearchItem.id)
        .offset(offset)
        .limit(limit)
    ).all()
    recent = db.scalars(
        base.where(ResearchItem.analyzed_at.is_not(None))
        .order_by(ResearchItem.analyzed_at.desc(), ResearchItem.id)
        .offset(offset)
        .limit(min(limit, 3))
    ).all()

    reviews = followthrough(db, user_id, {r.id for r in [*rows, *recent, *pending]})

    def item(r):
        source_kind = db.scalar(
            select(ResearchSource.kind).where(
                ResearchSource.id == r.source_id, ResearchSource.user_id == user_id
            )
        )
        analysis = json.loads(r.analysis_json or "{}")
        synthesis = analysis.get("synthesis") or {}
        return {
            "id": r.id,
            "title": r.title,
            "author": r.author,
            "url": r.url,
            "source_kind": source_kind,
            "video_id": r.external_id if source_kind == "youtube" else None,
            "status": r.status,
            "published_at": r.published_at,
            "observed_at": r.observed_at,
            "attempted_at": r.attempted_at,
            "analyzed_at": r.analyzed_at,
            "next_attempt_at": r.next_attempt_at,
            "error": r.error,
            "attempts": r.attempts,
            "automatic_selection": analysis.get('daily_selection'),
            "follow_up_reviews": reviews.get(r.id, []),
            "review_summary": {
                "summary": str(synthesis.get("executive_summary") or "")[:800],
                "bottom_line": str(synthesis.get("bottom_line") or "")[:400],
                "portfolio_relevance": str(analysis.get("portfolio") or "")[:400],
            }
            if r.analyzed_at
            else None,
        }

    return ReadResult(
        "subscriptions",
        {
            "checked_at": now.isoformat(),
            "day": selected.isoformat(),
            "timezone": str(zone),
            "automatic_policy": {
                "max_videos_per_channel_per_day": 5,
                "selection": "Model selects relevance; fewer or none is valid. Explicit requests bypass this automatic allowance.",
                "fresh_window_hours": 24,
                "unknown_publication_date": "First observed timestamp is the intake-age fallback, not proof of publication today.",
                "archived": "Retained audit history, not work queued for later. Explicit requests can still select an archived video.",
            },
            "coverage": "Current saved poll/processing state, not an immutable historical event log or fresh upload census. "
            "Latest attempt timestamps can replace earlier attempts after retries. "
            "Completed videos can also appear in activity; deduplicate by video_id, never add those counts.",
            "channel_polls": [
                {
                    "name": r.channel_name,
                    "enabled": bool(r.enabled),
                    "last_polled_at": r.last_polled_at,
                    "last_error": r.last_error,
                }
                for r in db.scalars(channels.order_by(YouTubeChannel.id).limit(50))
            ],
            "source_polls": [
                {
                    "name": r.name,
                    "kind": r.kind,
                    "enabled": r.enabled,
                    "last_polled_at": r.last_polled_at,
                    "last_error": r.last_error,
                }
                for r in db.scalars(sources.order_by(ResearchSource.id).limit(50))
            ],
            "poll_list_limit": 50,
            "channel_polls_total": db.scalar(select(func.count()).select_from(channels.subquery()))
            or 0,
            "source_polls_total": db.scalar(select(func.count()).select_from(sources.subquery()))
            or 0,
            "activity": [item(r) for r in rows],
            "activity_total": activity_total,
            "completed_videos": [
                {
                    "id": r.id,
                    "video_id": r.youtube_video_id,
                    "title": r.title,
                    "url": r.url,
                    "published_at": r.published_at,
                    "analyzed_at": r.analyzed_at,
                    "recommendations": json.loads(r.recommendations_json),
                }
                for r in done
            ],
            "completed_videos_total": video_total,
            "backlog": [item(r) for r in pending],
            "all_dates_counts_by_status": counts,
            "latest_reviewed_across_dates": [item(r) for r in recent],
            "latest_reviewed_limit": min(limit, 3),
            "page_limit": limit,
            "page_offset": offset,
            "more_details": "Use research record_id or videos record_id; backlog covers all dates, activity is the selected processing day.",
        },
        [
            *[
                Citation("research_item", r.id, str(r.analyzed_at or r.observed_at), url=r.url)
                for r in {r.id: r for r in [*rows, *pending, *recent]}.values()
            ],
            *[Citation("youtube_video", str(r.id), str(r.analyzed_at), url=r.url) for r in done],
        ],
        [
            "An empty/stale feed or incomplete saved queue cannot prove there are no missing uploads. "
            "Queued means not yet processed, not rejected or skipped; do not invent a reason or completion ETA."
        ],
    )


def read_video(db, user_id, *, record_id=None, limit=20, offset=0, **_):
    from argosy.state.models import YouTubeClaim

    stmt = select(YouTubeVideo).where(YouTubeVideo.user_id == user_id)
    if record_id is not None:
        stmt = stmt.where(YouTubeVideo.id == int(record_id))
    rows = db.scalars(
        stmt.order_by(YouTubeVideo.analyzed_at.desc()).offset(offset).limit(limit)
    ).all()
    return ReadResult(
        "videos",
        [
            {
                "id": r.id,
                "title": r.title,
                "url": r.url,
                "published_at": r.published_at,
                "analyzed_at": r.analyzed_at,
                "recommendations": json.loads(r.recommendations_json),
                "claims": [
                    {
                        "statement": c.statement,
                        "timestamp": c.timestamp,
                        "evidence_excerpt": c.evidence_excerpt,
                        "status": c.status,
                    }
                    for c in db.scalars(
                        select(YouTubeClaim)
                        .where(YouTubeClaim.video_id == r.id)
                        .order_by(YouTubeClaim.id)
                        .limit(50)
                    )
                ]
                if record_id
                else [],
            }
            for r in rows
        ],
        [Citation("youtube_video", str(r.id), str(r.analyzed_at), url=r.url) for r in rows],
        [
            "Speaker claims are attributed research, not verified facts or trade instructions. Claims limited to 50 per video."
        ],
    )
