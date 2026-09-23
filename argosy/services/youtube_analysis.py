"""Orchestrate and persist Argosy's read-only YouTube research fleet."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from argosy.config import get_settings
from argosy.logging import get_logger
from argosy.services.agent_report_persistence import persist_agent_report_async
from argosy.services.file_catalog import UserFileDTO, catalog_upload
from argosy.services.portfolio_snapshot_store import load_current_book_snapshot
from argosy.services.youtube_transcript import (
    TranscriptBackend,
    YouTubeTranscript,
    fetch_transcript,
)
from argosy.state import db as db_mod
from argosy.state.models import User

_log = get_logger("argosy.services.youtube_analysis")

if TYPE_CHECKING:
    from argosy.agents.youtube_analysis import (
        TranscriptClaimsOutput,
        TranscriptPortfolioOutput,
        TranscriptSkepticOutput,
        YouTubeFleetSynthesisOutput,
    )


@dataclass(frozen=True)
class YouTubeFleetResult:
    run_id: str
    transcript: YouTubeTranscript
    transcript_file: UserFileDTO | None
    claims: TranscriptClaimsOutput
    skeptic: TranscriptSkepticOutput
    portfolio: TranscriptPortfolioOutput
    synthesis: YouTubeFleetSynthesisOutput
    agent_report_ids: tuple[int, ...]
    cost_usd: float
    routed_recommendations: tuple[Any, ...] = ()
    artifact_path: Path | None = None
    json_path: Path | None = None


async def fetch_and_catalog_transcript(
    reference: str,
    *,
    user_id: str = "ariel",
    languages: Sequence[str] = ("en",),
    backend: TranscriptBackend = "auto",
    catalog: bool = True,
    artifact_format: Literal["markdown", "vtt"] = "markdown",
) -> tuple[YouTubeTranscript, UserFileDTO | None]:
    transcript = await asyncio.to_thread(
        fetch_transcript, reference, languages=languages, backend=backend
    )
    if artifact_format == "vtt" and transcript.raw_captions is None:
        raise ValueError(
            "Exact VTT output requires --backend yt-dlp; the transcript API "
            "returns parsed caption segments rather than the original VTT."
        )
    if artifact_format not in {"markdown", "vtt"}:
        raise ValueError(f"Unknown transcript artifact format: {artifact_format!r}")
    if not catalog:
        return transcript, None
    await _ensure_user(user_id)
    exact_vtt = artifact_format == "vtt"
    raw_bytes = (
        transcript.raw_caption_bytes
        if exact_vtt and transcript.raw_caption_bytes is not None
        else transcript.raw_captions.encode("utf-8")
        if exact_vtt and transcript.raw_captions is not None
        else transcript.to_markdown().encode("utf-8")
    )
    cataloged = await catalog_upload(
        user_id=user_id,
        raw_bytes=raw_bytes,
        original_name=(
            f"youtube-{transcript.video_id}-captions.vtt"
            if exact_vtt
            else f"youtube-{transcript.video_id}-transcript.md"
        ),
        mime_type="text/vtt" if exact_vtt else "text/markdown",
        kind="text",
        source="youtube_transcript",
    )
    return transcript, cataloged


async def analyze_youtube(
    reference: str,
    *,
    user_id: str = "ariel",
    languages: Sequence[str] = ("en",),
    backend: TranscriptBackend = "auto",
    save: bool = True,
    claims_agent: Any | None = None,
    skeptic_agent: Any | None = None,
    portfolio_agent: Any | None = None,
    synthesis_agent: Any | None = None,
) -> YouTubeFleetResult:
    """Fetch one transcript, run three independent readers, then synthesize."""
    from argosy.agents.youtube_analysis import (
        TranscriptClaimsOutput,
        TranscriptPortfolioOutput,
        TranscriptSkepticOutput,
        YouTubeClaimsAgent,
        YouTubeFleetSynthesisOutput,
        YouTubePortfolioAgent,
        YouTubeSkepticAgent,
        YouTubeSynthesisAgent,
        model_json,
    )

    transcript, cataloged = await fetch_and_catalog_transcript(
        reference,
        user_id=user_id,
        languages=languages,
        backend=backend,
        catalog=save,
    )
    if save:
        await _ensure_user(user_id)

    run_id = f"youtube:{transcript.video_id}:{uuid.uuid4().hex[:12]}"
    transcript_source_id = f"youtube:{transcript.video_id}:transcript"
    transcript_text = transcript.agent_text()
    portfolio_context = await asyncio.to_thread(_portfolio_context, user_id)
    portfolio_source_id = f"argosy:{user_id}:current-portfolio"

    claim_reader = claims_agent or YouTubeClaimsAgent(user_id=user_id)
    skeptic_reader = skeptic_agent or YouTubeSkepticAgent(user_id=user_id)
    portfolio_reader = portfolio_agent or YouTubePortfolioAgent(user_id=user_id)

    claims_report, skeptic_report, portfolio_report = await asyncio.gather(
        claim_reader.run(
            transcript=transcript_text,
            source_id=transcript_source_id,
            decision_id=run_id,
        ),
        skeptic_reader.run(
            transcript=transcript_text,
            source_id=transcript_source_id,
            decision_id=run_id,
        ),
        portfolio_reader.run(
            transcript=transcript_text,
            transcript_source_id=transcript_source_id,
            portfolio_context=portfolio_context,
            portfolio_source_id=portfolio_source_id,
            decision_id=run_id,
        ),
    )

    final_reader = synthesis_agent or YouTubeSynthesisAgent(user_id=user_id)
    synthesis_report = await final_reader.run(
        transcript_source_id=transcript_source_id,
        transcript=transcript_text,
        claims_source_id=f"{run_id}:claims",
        claims_json=model_json(claims_report.output),
        skeptic_source_id=f"{run_id}:skeptic",
        skeptic_json=model_json(skeptic_report.output),
        portfolio_source_id=f"{run_id}:portfolio",
        portfolio_json=model_json(portfolio_report.output),
        portfolio_context_source_id=portfolio_source_id,
        portfolio_context=portfolio_context,
        decision_id=run_id,
    )
    reports = (claims_report, skeptic_report, portfolio_report, synthesis_report)
    report_ids: list[int] = []
    if save:
        for report in reports:
            report_id = await persist_agent_report_async(report, decision_id=run_id)
            if report_id is not None:
                report_ids.append(report_id)

    claims_output = TranscriptClaimsOutput.model_validate(claims_report.output)
    skeptic_output = TranscriptSkepticOutput.model_validate(skeptic_report.output)
    portfolio_output = TranscriptPortfolioOutput.model_validate(portfolio_report.output)
    synthesis_output = YouTubeFleetSynthesisOutput.model_validate(synthesis_report.output)
    routed_recommendations: tuple[Any, ...] = ()
    if save and synthesis_output.ticker_recommendations:
        from sqlalchemy.orm import sessionmaker

        from argosy.services.ingest_recommendation_router import (
            route_ingest_recommendations,
        )
        from argosy.state.db import create_sync_engine

        url = str(get_settings().database_url).replace("+aiosqlite", "")
        factory = sessionmaker(bind=create_sync_engine(url))

        def _route_recommendations() -> tuple[Any, ...]:
            with factory() as routing_session:
                return route_ingest_recommendations(
                    routing_session,
                    user_id=user_id,
                    source_kind="youtube",
                    source_id=transcript.video_id,
                    source_url=transcript.url,
                    recommendations=[
                        item.model_dump(mode="python")
                        for item in synthesis_output.ticker_recommendations
                    ],
                )

        routed_recommendations = await asyncio.to_thread(_route_recommendations)
    partial = YouTubeFleetResult(
        run_id=run_id,
        transcript=transcript,
        transcript_file=cataloged,
        claims=claims_output,
        skeptic=skeptic_output,
        portfolio=portfolio_output,
        synthesis=synthesis_output,
        agent_report_ids=tuple(report_ids),
        cost_usd=sum(float(report.cost_usd) for report in reports),
        routed_recommendations=routed_recommendations,
    )
    if not save:
        return partial
    artifact_path, json_path = _write_artifacts(partial)
    completed = YouTubeFleetResult(
        **{
            **partial.__dict__,
            "artifact_path": artifact_path,
            "json_path": json_path,
        }
    )
    try:
        from argosy.services.youtube_intelligence import persist_youtube_analysis

        await asyncio.to_thread(
            persist_youtube_analysis,
            result_to_dict(completed),
            user_id=user_id,
        )
    except Exception as exc:  # research result remains valid if indexing fails
        _log.warning(
            "youtube.intelligence_persist_failed",
            video_id=transcript.video_id,
            error=str(exc)[:300],
        )
    return completed


async def _ensure_user(user_id: str) -> None:
    async with db_mod.get_session() as session:
        existing = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if existing is None:
            session.add(User(id=user_id))
            await session.commit()


def _portfolio_context(user_id: str) -> str:
    snapshot = load_current_book_snapshot(None, user_id)
    if snapshot is None:
        return "No current portfolio snapshot is available."
    positions: list[dict[str, Any]] = []
    total_usd_k = 0.0
    for position in snapshot.positions:
        row = position.model_dump() if hasattr(position, "model_dump") else dict(position)
        try:
            total_usd_k += float(row.get("usd_value_k") or 0.0)
        except (TypeError, ValueError):
            pass
        positions.append(row)
    lines = [
        f"Snapshot date: {snapshot.snapshot_date}",
        f"Total listed-position value: approximately ${total_usd_k:,.1f}k",
        "Current positions:",
    ]
    for row in positions[:150]:
        symbol = str(row.get("symbol") or "(no symbol)")
        asset_type = str(row.get("asset_type") or "unknown")
        account = str(row.get("account") or row.get("account_id") or "unknown")
        value = row.get("usd_value_k")
        lines.append(f"- {symbol}: asset_type={asset_type}; account={account}; usd_value_k={value}")
    return "\n".join(lines)


def render_fleet_markdown(result: YouTubeFleetResult) -> str:
    syn = result.synthesis
    lines = [
        "# Argosy YouTube fleet analysis",
        "",
        f"- Video: {result.transcript.title or result.transcript.video_id}",
        f"- URL: {result.transcript.url}",
        f"- Transcript: {result.transcript.language_code} via {result.transcript.backend}",
        f"- Fleet run: {result.run_id}",
        f"- Decision status: {syn.decision_status}",
        f"- Confidence: {syn.confidence.value}",
        "",
        "## Executive summary",
        "",
        syn.executive_summary,
        "",
        "## What the author argues",
        "",
        *_bullets(syn.what_the_author_argues),
        "",
        "## Credible takeaways",
        "",
        *_bullets(syn.credible_takeaways),
        "",
        "## Challenged claims",
        "",
    ]
    if syn.challenged_claims:
        for item in syn.challenged_claims:
            lines.extend(
                [
                    f"- **{item.claim}** — {item.concern}",
                    f"  Resolve with: {item.what_would_resolve_it}",
                ]
            )
    else:
        lines.append("- None identified by the panel.")
    lines.extend(
        [
            "",
            "## Market outlook",
            "",
        ]
    )
    if syn.market_outlook:
        for market_item in syn.market_outlook:
            lines.extend(
                [
                    f"- **{market_item.claim}** — {market_item.portfolio_impact}",
                    f"  Transcript: {market_item.transcript_evidence}",
                    f"  Corroborate with: {market_item.external_verification_test}",
                ]
            )
    else:
        lines.append("- No material market-level outlook in this transcript.")
    lines.extend(
        [
            "",
            "## Portfolio read-through",
            "",
            *_bullets(syn.portfolio_readthrough),
            "",
            "## Verification queue",
            "",
            *_bullets(syn.verification_queue),
            "",
            "## Routed recommendations",
            "",
            *_routed_bullets(result.routed_recommendations),
            "",
            "## Skeptic's audit",
            "",
            f"Source quality: **{result.skeptic.source_quality}**",
            "",
        ]
    )
    for finding in result.skeptic.findings:
        at = f" at {finding.timestamp}" if finding.timestamp else ""
        lines.extend(
            [
                f"- **{finding.severity.upper()}**{at}: {finding.issue}",
                f"  Why it matters: {finding.why_it_matters}",
                f"  Test: {finding.verification_test}",
            ]
        )
    lines.extend(
        [
            "",
            "## Bottom line",
            "",
            syn.bottom_line,
            "",
            "---",
            "This is a transcript-grounded research note. The fleet did not "
            "create or approve a trade proposal.",
            "",
        ]
    )
    return "\n".join(lines)


def result_to_dict(result: YouTubeFleetResult) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "video": {
            "video_id": result.transcript.video_id,
            "url": result.transcript.url,
            "title": result.transcript.title,
            "channel": result.transcript.channel,
            "language": result.transcript.language,
            "language_code": result.transcript.language_code,
            "is_generated": result.transcript.is_generated,
            "backend": result.transcript.backend,
            "segments": len(result.transcript.segments),
        },
        "transcript_file": (
            result.transcript_file.model_dump(mode="json")
            if result.transcript_file is not None
            else None
        ),
        "claims": result.claims.model_dump(mode="json"),
        "skeptic": result.skeptic.model_dump(mode="json"),
        "portfolio": result.portfolio.model_dump(mode="json"),
        "synthesis": result.synthesis.model_dump(mode="json"),
        "agent_report_ids": list(result.agent_report_ids),
        "cost_usd": result.cost_usd,
        "routed_recommendations": [
            {
                "ticker": item.ticker,
                "disposition": item.disposition,
                "added_to_argosy_list": item.added_to_argosy_list,
                "inbox_proposal_id": item.inbox_proposal_id,
                "watchlist_proposal_id": item.watchlist_proposal_id,
            }
            for item in result.routed_recommendations
        ],
        "artifact_path": str(result.artifact_path) if result.artifact_path else None,
        "json_path": str(result.json_path) if result.json_path else None,
    }


def _write_artifacts(result: YouTubeFleetResult) -> tuple[Path, Path]:
    now = datetime.now(UTC)
    output_dir = (
        get_settings().logs_dir / "youtube" / now.strftime("%Y-%m-%d") / result.transcript.video_id
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = now.strftime("%H%M%S") + "__" + result.run_id.rsplit(":", 1)[-1]
    markdown_path = output_dir / f"{stem}.md"
    json_path = output_dir / f"{stem}.json"
    markdown_path.write_text(render_fleet_markdown(result), encoding="utf-8")
    payload = result_to_dict(result)
    payload["artifact_path"] = str(markdown_path)
    payload["json_path"] = str(json_path)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return markdown_path, json_path


def _bullets(items: Sequence[str]) -> list[str]:
    return [f"- {item}" for item in items] or ["- None."]


def _routed_bullets(items: Sequence[Any]) -> list[str]:
    lines: list[str] = []
    for item in items:
        destination = (
            f"inbox proposal #{item.inbox_proposal_id}"
            if item.inbox_proposal_id is not None
            else "Argosy watchlist"
        )
        lines.append(f"- **{item.ticker}**: {item.disposition.upper()} → {destination}")
    return lines or ["- None."]


__all__ = [
    "YouTubeFleetResult",
    "analyze_youtube",
    "fetch_and_catalog_transcript",
    "render_fleet_markdown",
    "result_to_dict",
]
