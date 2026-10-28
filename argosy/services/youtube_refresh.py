"""Bounded on-demand refresh of configured public channel feeds (no fleet run)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from argosy.services.research_catalog import enqueue, research_session, upsert_source, utc
from argosy.state.models import YouTubeChannel, YouTubeVideo
from argosy.state.research_models import ResearchItem, ResearchSource


def _claim(user_id, channel_id, now):
    with research_session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        channel = db.scalar(
            select(YouTubeChannel).where(
                YouTubeChannel.user_id == user_id,
                YouTubeChannel.id == channel_id,
                YouTubeChannel.enabled == 1,
            )
        )
        if channel is None:
            return None, "not_enabled"
        source = upsert_source(
            db,
            user_id=user_id,
            name=channel.channel_name,
            kind="youtube",
            reference=channel.youtube_channel_id,
            enabled=True,
        )
        source.enabled = True
        config = json.loads(source.config_json or "{}")
        previous = config.get("live_poll_attempt_at")
        if previous and now - datetime.fromisoformat(previous) < timedelta(minutes=5):
            return None, "recent_attempt_or_in_progress"
        token = uuid.uuid4().hex
        config.update(live_poll_attempt_at=now.isoformat(), live_poll_token=token)
        source.config_json = json.dumps(config)
        db.commit()
        return (source.id, token, channel.youtube_channel_id, channel.channel_name), None


def _finish(user_id, claim, feed, error, now):
    from argosy.services.youtube_intelligence import _parse_iso

    source_id, token, external_channel, name = claim
    queued = 0
    with research_session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        source = db.get(ResearchSource, source_id)
        config = json.loads(source.config_json or "{}")
        if source.user_id != user_id or config.get("live_poll_token") != token:
            raise ValueError("Channel refresh ownership changed")
        channel = db.scalar(
            select(YouTubeChannel).where(
                YouTubeChannel.user_id == user_id,
                YouTubeChannel.youtube_channel_id == external_channel,
            )
        )
        if error:
            channel.last_error = source.last_error = error[:1000]
        else:
            completed = set(
                db.scalars(
                    select(YouTubeVideo.youtube_video_id).where(YouTubeVideo.user_id == user_id)
                )
            )
            completed.update(
                db.scalars(
                    select(ResearchItem.external_id).where(
                        ResearchItem.user_id == user_id,
                        ResearchItem.source_id == source_id,
                        ResearchItem.analyzed_at.is_not(None),
                    )
                )
            )
            for item in feed:
                if item["video_id"] in completed:
                    continue
                row, created = enqueue(
                    db,
                    source,
                    external_id=item["video_id"],
                    title=item.get("title") or item["video_id"],
                    url="https://www.youtube.com/watch?v=" + item["video_id"],
                    body="",
                    published_at=_parse_iso(item.get("published_at")),
                    now=now,
                )
                if (
                    row.status in {"queued", "failed"}
                    and not row.analyzed_at
                    and utc(row.published_at or row.observed_at) < now - timedelta(hours=24)
                ):
                    row.status = "archived"
                    row.error = "Outside the 24-hour automatic news window; retained as history, not pending analysis."
                queued += int(created and row.status == "queued")
            for old in db.scalars(
                select(ResearchItem).where(
                    ResearchItem.user_id == user_id,
                    ResearchItem.source_id == source_id,
                    ResearchItem.status.in_(["queued", "failed"]),
                    ResearchItem.analyzed_at.is_(None),
                    func.coalesce(ResearchItem.published_at, ResearchItem.observed_at)
                    < now - timedelta(hours=24),
                )
            ):
                old.status = "archived"
                old.error = "Outside the 24-hour automatic news window; retained as history, not pending analysis."
            channel.last_polled_at = source.last_polled_at = now
            channel.last_error = source.last_error = None
            if feed:
                channel.last_seen_video_id = feed[0]["video_id"]
                channel.last_seen_published_at = _parse_iso(feed[0].get("published_at"))
        config.pop("live_poll_token", None)
        source.config_json = json.dumps(config)
        db.commit()
    return {
        "channel": name,
        "status": "failed" if error else "checked_online",
        "checked_at": now.isoformat(),
        "uploads_returned": len(feed),
        "queued": queued,
        "error": error,
        "coverage": "Bounded public upload feed; not a complete historical census. No new video analysis ran.",
    }


def _configured_channels(user_id, channels):
    with research_session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        rows = db.scalars(select(YouTubeChannel).where(YouTubeChannel.user_id == user_id)).all()
        # Keep the research registry aligned with the subscription switch, including pauses.
        for row in rows:
            mirror = upsert_source(
                db,
                user_id=user_id,
                name=row.channel_name,
                kind="youtube",
                reference=row.youtube_channel_id,
                enabled=bool(row.enabled),
            )
            mirror.enabled = bool(row.enabled)
        db.commit()
        selected = [
            r
            for r in rows
            if r.enabled
            and (
                "*" in channels
                or any(
                    value.casefold() in {r.channel_name.casefold(), r.youtube_channel_id.casefold()}
                    for value in channels
                )
            )
        ]
        return [(r.id, r.channel_name) for r in selected]


async def refresh_channels(*, user_id, channels, progress=None):
    from argosy.services.youtube_intelligence import _fetch_feed

    if not channels or len(channels) > 6:
        raise ValueError("Select one to six configured channels")
    try:
        ids = await asyncio.to_thread(_configured_channels, user_id, channels)
    except Exception as exc:
        return [
            {
                "channel": "subscription_registry",
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"[:1000],
            }
        ]
    if not ids:
        return [
            {
                "status": "not_found",
                "error": "No matching enabled channel; no substitute was searched.",
            }
        ]
    receipts = []
    for channel_id, name in ids:
        if progress:
            await progress(f"Checking online: {name} — please wait…")
        now = datetime.now(UTC)
        claim = None
        try:
            claim, skipped = await asyncio.to_thread(_claim, user_id, channel_id, now)
            if not claim:
                receipts.append(
                    {
                        "channel": name,
                        "status": skipped,
                        "coverage": "No new network read; inspect saved poll timestamp/errors.",
                    }
                )
                continue
            feed = await asyncio.to_thread(_fetch_feed, claim[2])
            receipts.append(
                await asyncio.to_thread(_finish, user_id, claim, feed, None, datetime.now(UTC))
            )
        except Exception as exc:
            failure = {
                "channel": name,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"[:1000],
            }
            if claim:
                try:
                    failure = await asyncio.to_thread(
                        _finish, user_id, claim, [], failure["error"], datetime.now(UTC)
                    )
                except Exception as receipt_error:
                    failure["poll_receipt_error"] = str(receipt_error)[:300]
            receipts.append(failure)
    return receipts
