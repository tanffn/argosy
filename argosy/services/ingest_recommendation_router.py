"""Route standardized research-ingest recommendations into Argosy.

WATCH and BUY candidates enter the persisted discovery list.  WATCH also gets
an info-level ``set_watchlist`` observer row, which the thesis monitor can
consume without cluttering the inbox.  BUY gets a warning-level inbox note for
human review.  Neither path creates, sizes, approves, or executes a trade.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.services.action_proposer_runner import write_action_proposal
from argosy.state.models import ActionProposal, ScanState

Disposition = Literal["watch", "buy", "pass"]
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9./-]{0,15}$")


class RecommendationLike(Protocol):
    ticker: str
    disposition: str
    rationale: str
    next_step: str
    confidence: Any


@dataclass(frozen=True)
class RoutedIngestRecommendation:
    ticker: str
    disposition: Disposition
    added_to_argosy_list: bool
    inbox_proposal_id: int | None = None
    watchlist_proposal_id: int | None = None


def route_ingest_recommendations(
    session: Session,
    *,
    user_id: str,
    source_kind: str,
    source_id: str,
    source_url: str | None,
    recommendations: Sequence[RecommendationLike | dict[str, Any]],
    now: datetime | None = None,
) -> tuple[RoutedIngestRecommendation, ...]:
    """Persist WATCH/BUY dispositions with deterministic open-row deduplication."""
    observed_at = now or datetime.now(UTC)
    source_key = _slug(source_kind)
    result: list[RoutedIngestRecommendation] = []

    for raw in recommendations:
        ticker = str(_value(raw, "ticker", "")).strip().upper()
        disposition = str(_value(raw, "disposition", "pass")).strip().lower()
        if disposition not in {"watch", "buy", "pass"}:
            continue
        if not _TICKER_RE.fullmatch(ticker):
            continue
        _supersede_conflicting_open_rows(
            session,
            user_id=user_id,
            source_key=source_key,
            source_id=source_id,
            ticker=ticker,
            keep=disposition,
            now=observed_at,
        )
        if disposition == "pass":
            result.append(
                RoutedIngestRecommendation(
                    ticker=ticker,
                    disposition="pass",
                    added_to_argosy_list=False,
                )
            )
            continue

        rationale = str(_value(raw, "rationale", "")).strip()
        next_step = str(_value(raw, "next_step", "")).strip()
        confidence = _confidence(_value(raw, "confidence", "LOW"))
        citation = source_url or f"{source_kind}:{source_id}"
        evidence = {
            "stream": f"ingest_{source_key}",
            "source_kind": source_kind,
            "source_id": source_id,
            "source_url": source_url,
            "disposition": disposition,
            "rationale": rationale,
            "next_step": next_step,
            "observed_at": observed_at.isoformat(),
        }
        pick = {
            "ticker": ticker,
            "conviction": confidence,
            "verdict": disposition.upper(),
            "thesis_md": rationale,
            "cites": [citation],
        }
        row = session.get(ScanState, {"user_id": user_id, "ticker": ticker})
        if row is None:
            row = ScanState(user_id=user_id, ticker=ticker)
            session.add(row)
            row.radar_fingerprint = (
                f"s=0|f=ingest_{source_key}|stream=ingest_{source_key}|event={source_id}"
            )
            row.nomination_evidence_json = json.dumps(evidence, default=str)
            row.fleet_json = json.dumps(pick, default=str)
        row.status = "active"
        row.quarantine_reason = ""
        row.last_seen_at = observed_at
        row.last_fleet_at = observed_at
        row.updated_at = observed_at
        session.flush()
        session.commit()

        watchlist_id: int | None = None
        inbox_id: int | None = None
        if disposition == "watch":
            dedup_key = f"ingest_watch|{source_key}|{source_id}|{ticker}"
            proposal = _existing_open(session, user_id, dedup_key) or (
                write_action_proposal(
                    session,
                    user_id,
                    kind="set_watchlist",
                    summary=f"Watch {ticker}",
                    rationale_md=(
                        rationale or f"The {source_kind} ingest marked {ticker} for monitoring."
                    ),
                    suggested_payload={
                        "ticker": ticker,
                        "watch_kind": "candidate",
                        "source_kind": source_kind,
                        "source_id": source_id,
                        "next_step": next_step,
                    },
                    severity="info",
                    dedup_key=dedup_key,
                    now=observed_at,
                )
            )
            watchlist_id = proposal.id
        else:
            dedup_key = f"ingest_buy|{source_key}|{source_id}|{ticker}"
            proposal = _existing_open(session, user_id, dedup_key) or (
                write_action_proposal(
                    session,
                    user_id,
                    kind="note_only",
                    summary=f"Review BUY candidate: {ticker}",
                    rationale_md=(
                        f"An ingested {source_kind} analysis rated **{ticker}** as a "
                        f"BUY candidate. {rationale}\n\n"
                        f"Next verification: "
                        f"{next_step or 'Run the full decision funnel.'}\n\n"
                        "This is a review request, not a sized or approved trade."
                    ),
                    suggested_payload={
                        "ticker": ticker,
                        "recommendation": "buy",
                        "requested_action": "run_full_decision_funnel",
                        "source_kind": source_kind,
                        "source_id": source_id,
                        "next_step": next_step,
                    },
                    severity="warning",
                    dedup_key=dedup_key,
                    now=observed_at,
                )
            )
            inbox_id = proposal.id

        result.append(
            RoutedIngestRecommendation(
                ticker=ticker,
                disposition=disposition,  # type: ignore[arg-type]
                added_to_argosy_list=True,
                inbox_proposal_id=inbox_id,
                watchlist_proposal_id=watchlist_id,
            )
        )
    return tuple(result)


def _value(value: RecommendationLike | dict[str, Any], key: str, default: Any) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _confidence(value: Any) -> str:
    raw = getattr(value, "value", value)
    normalized = str(raw or "LOW").strip().upper()
    return normalized if normalized in {"LOW", "MEDIUM", "HIGH"} else "LOW"


def _existing_open(session: Session, user_id: str, dedup_key: str) -> ActionProposal | None:
    return session.execute(
        select(ActionProposal).where(
            ActionProposal.user_id == user_id,
            ActionProposal.dedup_key == dedup_key,
            ActionProposal.status == "open",
        )
    ).scalar_one_or_none()


def _supersede_conflicting_open_rows(
    session: Session,
    *,
    user_id: str,
    source_key: str,
    source_id: str,
    ticker: str,
    keep: str,
    now: datetime,
) -> None:
    keys = {
        "watch": f"ingest_watch|{source_key}|{source_id}|{ticker}",
        "buy": f"ingest_buy|{source_key}|{source_id}|{ticker}",
    }
    conflicting = [key for disposition, key in keys.items() if disposition != keep]
    if not conflicting:
        return
    rows = session.execute(
        select(ActionProposal).where(
            ActionProposal.user_id == user_id,
            ActionProposal.dedup_key.in_(conflicting),
            ActionProposal.status == "open",
        )
    ).scalars()
    changed = False
    for row in rows:
        row.status = "superseded"
        row.execution_state = "dismissed"
        row.decided_at = now
        row.decided_by_user_note = f"superseded by {keep.upper()} ingest disposition"
        changed = True
    if changed:
        session.commit()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "research"


__all__ = [
    "RoutedIngestRecommendation",
    "route_ingest_recommendations",
]
