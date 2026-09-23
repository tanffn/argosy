"""Persistence, source management, and discovery for YouTube research inputs."""

from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from argosy.config import get_settings
from argosy.state.db import create_sync_engine
from argosy.state.models import (
    User,
    YouTubeChannel,
    YouTubeClaim,
    YouTubeClaimEvaluation,
    YouTubeVideo,
)

_ATOM = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")


@dataclass(frozen=True)
class YouTubeMetadata:
    video_id: str | None
    title: str | None
    channel_id: str
    channel_name: str
    channel_url: str
    published_at: datetime | None = None


def _factory() -> sessionmaker:
    url = str(get_settings().database_url).replace("+aiosqlite", "")
    return sessionmaker(bind=create_sync_engine(url), expire_on_commit=False)


def resolve_youtube_metadata(reference: str) -> YouTubeMetadata:
    """Resolve either a video or channel URL without downloading media."""
    from yt_dlp import YoutubeDL

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "playlist_items": "1",
        "socket_timeout": 20,
        "retries": 1,
    }
    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(reference, download=False)
    if not isinstance(info, dict):
        raise ValueError("YouTube did not return source metadata")
    entries = info.get("entries") or []
    first = next((item for item in entries if isinstance(item, dict)), None)
    item = first or info
    channel_id = str(item.get("channel_id") or info.get("channel_id") or "").strip()
    channel_name = str(
        item.get("channel")
        or item.get("uploader")
        or info.get("channel")
        or info.get("uploader")
        or ""
    ).strip()
    channel_url = str(item.get("channel_url") or info.get("channel_url") or "").strip()
    if not channel_id:
        # Canonical /channel/ URLs are already stable IDs.
        match = re.search(r"youtube\.com/channel/([^/?#]+)", reference)
        channel_id = match.group(1) if match else ""
    if not channel_id:
        raise ValueError("Could not resolve the YouTube channel ID")
    if not channel_url:
        channel_url = f"https://www.youtube.com/channel/{channel_id}"
    published_at = _parse_upload_date(item.get("upload_date") or info.get("upload_date"))
    video_id = str(item.get("id") or "").strip() or None
    if video_id and len(video_id) != 11:
        video_id = None
    return YouTubeMetadata(
        video_id=video_id,
        title=str(item.get("title") or "").strip() or None,
        channel_id=channel_id,
        channel_name=channel_name or channel_id,
        channel_url=channel_url,
        published_at=published_at,
    )


def subscribe_youtube_source(reference: str, *, user_id: str = "ariel") -> dict[str, Any]:
    metadata = resolve_youtube_metadata(reference)
    factory = _factory()
    with factory() as session:
        _ensure_user(session, user_id)
        source = session.scalar(
            select(YouTubeChannel).where(
                YouTubeChannel.user_id == user_id,
                YouTubeChannel.youtube_channel_id == metadata.channel_id,
            )
        )
        if source is None:
            source = YouTubeChannel(
                user_id=user_id,
                youtube_channel_id=metadata.channel_id,
                channel_name=metadata.channel_name,
                channel_url=metadata.channel_url,
                enabled=1,
                last_seen_video_id=metadata.video_id,
                last_seen_published_at=metadata.published_at,
            )
            session.add(source)
        else:
            source.channel_name = metadata.channel_name
            source.channel_url = metadata.channel_url
            source.enabled = 1
            # Re-enabling or idempotently subscribing must not skip unseen uploads.
        session.commit()
        session.refresh(source)
        return _source_row(session, source)


def set_youtube_source_enabled(
    source_id: int, enabled: bool, *, user_id: str = "ariel"
) -> dict[str, Any]:
    with _factory()() as session:
        source = session.scalar(
            select(YouTubeChannel).where(
                YouTubeChannel.id == source_id, YouTubeChannel.user_id == user_id
            )
        )
        if source is None:
            raise KeyError(source_id)
        source.enabled = int(enabled)
        source.updated_at = datetime.now(UTC)
        session.commit()
        return _source_row(session, source)


def list_youtube_sources(*, user_id: str = "ariel") -> dict[str, Any]:
    with _factory()() as session:
        sources = session.scalars(
            select(YouTubeChannel)
            .where(YouTubeChannel.user_id == user_id)
            .order_by(YouTubeChannel.channel_name)
        ).all()
        rows = [_source_row(session, source) for source in sources]
    totals = {
        "sources": len(rows),
        "enabled": sum(1 for row in rows if row["enabled"]),
        "videos_ingested": sum(row["stats"]["videos_ingested"] for row in rows),
        "tickers_collected": len({ticker for row in rows for ticker in row["stats"]["tickers"]}),
        "claims_captured": sum(row["stats"]["claims_captured"] for row in rows),
        "claims_evaluated": sum(row["stats"]["claims_evaluated"] for row in rows),
    }
    return {"sources": rows, "totals": totals}


def persist_youtube_analysis(payload: dict[str, Any], *, user_id: str = "ariel") -> int:
    """Idempotently retain an analysis artifact and its attributable claims."""
    video_data = payload.get("video") or {}
    video_id = str(video_data.get("video_id") or "").strip()
    if not video_id:
        raise ValueError("analysis payload has no video id")
    try:
        metadata = resolve_youtube_metadata(str(video_data.get("url") or video_id))
    except Exception:
        channel_name = str(video_data.get("channel") or "Unknown channel")
        metadata = YouTubeMetadata(
            None, video_data.get("title"), f"unknown:{channel_name}", channel_name, ""
        )
    with _factory()() as session:
        _ensure_user(session, user_id)
        source = _get_or_create_channel(session, user_id, metadata, enabled=False)
        row = session.scalar(
            select(YouTubeVideo).where(
                YouTubeVideo.user_id == user_id, YouTubeVideo.youtube_video_id == video_id
            )
        )
        claims_data = payload.get("claims") or {}
        synth = payload.get("synthesis") or {}
        recommendations = synth.get("ticker_recommendations") or []
        tickers = sorted(
            {
                _ticker(t)
                for t in [
                    *claims_data.get("named_tickers", []),
                    *(item.get("ticker") for item in recommendations),
                ]
                if _ticker(t)
            }
        )
        transcript_file = payload.get("transcript_file") or {}
        if row is None:
            row = YouTubeVideo(
                user_id=user_id, youtube_video_id=video_id, url=str(video_data.get("url") or "")
            )
            session.add(row)
            session.flush()
        row.channel_id = source.id
        row.title = str(video_data.get("title") or metadata.title or "") or None
        row.published_at = metadata.published_at
        row.transcript_file_id = transcript_file.get("id")
        row.analysis_run_id = payload.get("run_id")
        row.artifact_path = payload.get("artifact_path")
        row.tickers_json = json.dumps(tickers)
        row.recommendations_json = json.dumps(recommendations, ensure_ascii=False)
        row.analyzed_at = datetime.now(UTC)
        row.cost_usd = float(payload.get("cost_usd") or 0.0)
        session.flush()
        # Re-analysis must not erase the claim/evaluation history.
        existing_claims = session.scalars(select(YouTubeClaim).where(YouTubeClaim.video_id == row.id)).all()
        existing_statements = {c.statement for c in existing_claims}
        existing_keys = {c.claim_key for c in existing_claims}
        def unique_claim_key(key, statement):
            from hashlib import sha256
            return key if key not in existing_keys else key[:45] + ":" + sha256(statement.encode()).hexdigest()[:12]
        calls_by_statement = {
            str(c.get("statement")): c for c in claims_data.get("speaker_calls", [])
        }
        for index, claim in enumerate(claims_data.get("claims", []), 1):
            statement = str(claim.get("statement") or "")
            if statement in existing_statements:
                continue
            call = calls_by_statement.get(statement, {})
            entities = [str(item) for item in claim.get("named_entities", [])]
            claim_tickers = sorted(set(tickers).intersection({_ticker(item) for item in entities}))
            session.add(
                YouTubeClaim(
                    video_id=row.id,
                    claim_key=unique_claim_key(str(claim.get("claim_id") or f"C{index}"), statement),
                    timestamp=claim.get("timestamp"),
                    claim_type=str(claim.get("claim_type") or "opinion"),
                    statement=statement,
                    evidence_excerpt=str(claim.get("evidence_excerpt") or ""),
                    named_entities_json=json.dumps(entities, ensure_ascii=False),
                    tickers_json=json.dumps(claim_tickers),
                    direction=call.get("direction"),
                    horizon_days=call.get("horizon_days"),
                    evaluation_due_at=_due_at(call.get("horizon_days")),
                    status=(
                        "open"
                        if call or claim.get("claim_type") in {"prediction", "recommendation"}
                        else "unscored"
                    ),
                )
            )
        for index, claim in enumerate(claims_data.get("market_outlook_claims", []), 1):
            if str(claim.get("statement") or "") in existing_statements:
                continue
            session.add(
                YouTubeClaim(
                    video_id=row.id,
                    claim_key=unique_claim_key(f"M{index}", str(claim.get("statement") or "")),
                    timestamp=claim.get("timestamp"),
                    claim_type="market_outlook",
                    statement=str(claim.get("statement") or ""),
                    evidence_excerpt=str(claim.get("evidence_excerpt") or ""),
                    named_entities_json="[]",
                    tickers_json="[]",
                    is_market_outlook=1,
                    status="open",
                )
            )
        session.commit()
        video_row_id = row.id
    from argosy.services.research_catalog import index_youtube
    index_youtube(payload, user_id=user_id, database_url=str(session.get_bind().url))
    return video_row_id


def backfill_youtube_artifacts(*, user_id: str = "ariel") -> dict[str, int]:
    root = get_settings().logs_dir / "youtube"
    imported = failed = 0
    for path in sorted(root.glob("**/*.json")) if root.exists() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["json_path"] = str(path)
            persist_youtube_analysis(payload, user_id=user_id)
            imported += 1
        except Exception:
            failed += 1
    return {"imported": imported, "failed": failed}


async def sync_youtube_subscriptions(
    *, user_id: str = "ariel", max_videos_per_source: int = 2
) -> dict[str, Any]:
    from argosy.services.research_worker import queue_youtube, sync_research
    queued = await queue_youtube(user_id=user_id)
    result = await sync_research(user_id=user_id)
    failures = queued["failures"] + result["failures"]
    return {**result, "youtube": queued, "failures": failures, "error_count": len(failures)}



def _source_row(session: Session, source: YouTubeChannel) -> dict[str, Any]:
    videos = session.scalars(select(YouTubeVideo).where(YouTubeVideo.channel_id == source.id)).all()
    video_ids = [video.id for video in videos]
    claims = (
        session.scalars(select(YouTubeClaim).where(YouTubeClaim.video_id.in_(video_ids))).all()
        if video_ids
        else []
    )
    claim_ids = [claim.id for claim in claims]
    evaluations = (
        session.scalars(
            select(YouTubeClaimEvaluation).where(YouTubeClaimEvaluation.claim_id.in_(claim_ids))
        ).all()
        if claim_ids
        else []
    )
    tickers = sorted(
        {ticker for video in videos for ticker in json.loads(video.tickers_json or "[]")}
    )
    resolved = [e for e in evaluations if e.verdict != "inconclusive"]
    accuracy = (
        None
        if not resolved
        else round(
            sum(
                1 if e.verdict == "correct" else 0.5 if e.verdict == "partially_correct" else 0
                for e in resolved
            )
            / len(resolved),
            3,
        )
    )
    return {
        "id": source.id,
        "youtube_channel_id": source.youtube_channel_id,
        "channel_name": source.channel_name,
        "channel_url": source.channel_url,
        "enabled": bool(source.enabled),
        "last_seen_video_id": source.last_seen_video_id,
        "last_polled_at": _iso(source.last_polled_at),
        "last_error": source.last_error,
        "stats": {
            "videos_ingested": len(videos),
            "tickers_collected": len(tickers),
            "tickers": tickers,
            "claims_captured": len(claims),
            "claims_open": sum(c.status == "open" for c in claims),
            "claims_evaluated": len(evaluations),
            "accuracy": accuracy,
            "last_analyzed_at": _iso(max((v.analyzed_at for v in videos), default=None)),
        },
    }


def _get_or_create_channel(
    session: Session, user_id: str, meta: YouTubeMetadata, *, enabled: bool
) -> YouTubeChannel:
    row = session.scalar(
        select(YouTubeChannel).where(
            YouTubeChannel.user_id == user_id, YouTubeChannel.youtube_channel_id == meta.channel_id
        )
    )
    if row is None:
        row = YouTubeChannel(
            user_id=user_id,
            youtube_channel_id=meta.channel_id,
            channel_name=meta.channel_name,
            channel_url=meta.channel_url,
            enabled=int(enabled),
        )
        session.add(row)
        session.flush()
    return row


def _fetch_feed(channel_id: str) -> list[dict[str, Any]]:
    request = Request(
        f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
        headers={"User-Agent": "Argosy/1.0"},
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed YouTube host
            root = ET.fromstring(response.read())
    except HTTPError as exc:
        if exc.code not in {404, 410}:
            raise
        # The RSS endpoint can disappear while the public channel is available.
        # Use the same channel's public uploads, never search/name substitution.
        return _fetch_public_uploads(channel_id)
    result = []
    for entry in root.findall("atom:entry", _ATOM):
        video_id = entry.findtext("yt:videoId", default="", namespaces=_ATOM)
        published = entry.findtext("atom:published", default="", namespaces=_ATOM)
        if video_id:
            result.append({"video_id": video_id, "published_at": published,
                           "title": entry.findtext("atom:title", default="", namespaces=_ATOM)})
    return result


def _fetch_public_uploads(channel_id: str) -> list[dict[str, Any]]:
    """Metadata-only fallback, bounded to the RSS feed's 15-item window."""
    from yt_dlp import YoutubeDL

    from argosy.logging import get_logger

    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
        raise ValueError("Invalid YouTube channel identity")
    options = {"quiet": True, "no_warnings": True, "extract_flat": True,
               "skip_download": True, "playlistend": 15, "socket_timeout": 15,
               "retries": 1, "extractor_retries": 1,
               # A partial playlist must not advance our durable checkpoint.
               "extractor_args": {"youtube": {"raise_incomplete_data": ["true"]}}}
    with YoutubeDL(options) as extractor:
        data = extractor.extract_info(
            "https://www.youtube.com/playlist?list=UU" + channel_id[2:], download=False,
        )
    if not isinstance(data, dict) or data.get("channel_id") != channel_id:
        raise ValueError("Public uploads could not verify the subscribed channel identity")
    result = []
    for entry in data.get("entries") or []:
        if not isinstance(entry, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", entry.get("id") or ""):
            raise ValueError("Public uploads returned an invalid video identity")
        result.append({"video_id": entry["id"], "title": entry.get("title") or entry["id"],
                       # Flat metadata does not prove a publication date. Never
                       # substitute fetch time or an approximate relative date.
                       "published_at": None})
        if len(result) == 15:
            break
    get_logger(__name__).info("youtube.public_uploads_fallback", channel_id=channel_id, videos=len(result))
    return result


def _unseen(feed: list[dict[str, Any]], checkpoint: str | None) -> list[dict[str, Any]]:
    if checkpoint is None:
        return feed[:1]
    return [item for item in feed if item["video_id"] != checkpoint][
        : next((i for i, item in enumerate(feed) if item["video_id"] == checkpoint), len(feed))
    ]


def _update_poll(
    source_id: int, user_id: str, newest: dict[str, Any] | None, error: str | None
) -> None:
    with _factory()() as session:
        values = {"last_polled_at": datetime.now(UTC), "last_error": error}
        if newest and error is None:
            values.update(last_seen_video_id=newest["video_id"],
                          last_seen_published_at=_parse_iso(newest.get("published_at")))
        # One write, not a read transaction upgraded under another SQLite writer.
        session.execute(
            update(YouTubeChannel).where(
                YouTubeChannel.id == source_id, YouTubeChannel.user_id == user_id
            ).values(**values)
        )
        session.commit()


def _video_ingested(user_id: str, video_id: str) -> bool:
    with _factory()() as session:
        return (
            session.scalar(
                select(YouTubeVideo.id).where(
                    YouTubeVideo.user_id == user_id,
                    YouTubeVideo.youtube_video_id == video_id,
                )
            )
            is not None
        )


def _ensure_user(session: Session, user_id: str) -> None:
    if session.get(User, user_id) is None:
        session.add(User(id=user_id))
        session.flush()


def _ticker(value: Any) -> str:
    item = str(value or "").strip().upper().lstrip("$")
    return item if _TICKER_RE.fullmatch(item) else ""


def _due_at(days: Any) -> datetime | None:
    try:
        return datetime.now(UTC) + timedelta(days=int(days)) if days else None
    except (TypeError, ValueError):
        return None


def _parse_upload_date(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y%m%d").replace(tzinfo=UTC) if value else None
    except ValueError:
        return None


def _parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


__all__ = [
    "backfill_youtube_artifacts",
    "list_youtube_sources",
    "persist_youtube_analysis",
    "resolve_youtube_metadata",
    "set_youtube_source_enabled",
    "subscribe_youtube_source",
    "sync_youtube_subscriptions",
]
