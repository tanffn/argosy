"""grade_discovery_ticker — the single-ticker fleet grader for the discovery
funnel (codex #6).

Wraps the existing per-ticker analyst orchestration (``run_per_ticker_analysts``,
long-hold mode) + ONE Opus synthesis pass into a :class:`FleetPick`. It
deliberately does NOT call ``DecisionFlow.run`` and never persists a proposal —
discovery grading is research, not an executable action. Properties required by
codex #6:

- explicit ``tier`` (default T1 — a real fleet pass, but the lightest one),
- an idempotency key ``ticker + radar_fingerprint + day`` so the funnel can skip
  re-grading an unchanged candidate the same day,
- a per-run cost guard (cap on analyst reports fed to the synthesizer),
- no proposal persistence (we open/close a ``decision_runs`` row for the analyst
  reports' lineage only).

Concurrency-cap + top-K live at the funnel layer (it decides how many tickers to
grade); this module grades exactly one.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

from argosy.agents.base import BaseAgent
from argosy.decisions.per_ticker_analysts import (
    InsufficientAnalystQuorum,
    open_decision_run_for_consult,
    run_per_ticker_analysts,
)
from argosy.logging import get_logger
from argosy.services.contracts import FleetPick

log = get_logger(__name__)

# Per-run cost guard: at most this many analyst reports are rendered into the
# synthesis prompt (a runaway analyst set must not blow the synthesis budget).
_MAX_REPORTS_FOR_SYNTHESIS = 8


class FleetGradeOutput(BaseModel):
    ticker: str
    conviction: Literal["HIGH", "MED", "LOW"]
    verdict: Literal["BUY", "WATCH", "PASS"]
    thesis_md: str
    cites: list[str] = []


class DiscoveryGraderAgent(BaseAgent[FleetGradeOutput]):
    """One Opus synthesis pass over the per-ticker analyst reports → a graded
    conviction/verdict/thesis. NOT a proposal; no execution."""

    agent_role = "discovery_grader"   # not in tables -> Opus fallback
    output_model = FleetGradeOutput
    require_citations = False          # cites are carried from the analyst reports

    def build_prompt(self, *, ticker, analyst_reports):
        rendered = "\n\n".join(
            f"### {getattr(r, 'agent_role', 'analyst')}\n"
            f"{(getattr(r, 'response_text', '') or '')[:4000]}"
            for r in analyst_reports[:_MAX_REPORTS_FOR_SYNTHESIS]
        )
        system = (
            "You are Argosy's discovery grader. Synthesize the per-ticker analyst "
            "reports for ONE candidate into a long-hold growth verdict. This is "
            "RESEARCH, not an executable order: produce conviction (HIGH/MED/LOW), "
            "a verdict (BUY = worth a sleeve position, WATCH = track, PASS = not "
            "now), a concise thesis (markdown), and cite which analyst roles "
            "support it. Weigh fundamentals + durable thesis over momentum."
        )
        user = (
            f"TICKER: {ticker}\n\nANALYST REPORTS:\n{rendered or '(none)'}\n\n"
            "Return a JSON object {\"ticker\": str, \"conviction\": \"HIGH|MED|LOW\", "
            "\"verdict\": \"BUY|WATCH|PASS\", \"thesis_md\": str, \"cites\": [str]}."
        )
        return system, user


def discovery_idempotency_key(user_id: str, ticker: str,
                              radar_fingerprint: str, day: str) -> str:
    """Stable key for (user, ticker, radar fingerprint, calendar day) — the funnel
    uses it to avoid re-grading an unchanged candidate within the same day."""
    raw = f"discovery|{user_id}|{ticker.upper()}|{radar_fingerprint}|{day}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


async def _close_decision_run(*, decision_run_id: int, status: str) -> None:
    """Close the lineage decision_run row (completed/blocked). Best-effort —
    discovery grading never persists a proposal, only the analyst reports' run."""
    from argosy.state import database as db_mod
    from argosy.state.models import DecisionRun

    async with db_mod.get_session() as session:
        row = await session.get(DecisionRun, decision_run_id)
        if row is not None:
            row.status = status
            row.finished_at = datetime.now(timezone.utc)
            await session.commit()


def _ticker_of(candidate) -> str:
    if isinstance(candidate, dict):
        return candidate.get("ticker", "")
    return getattr(candidate, "ticker", "")


async def grade_discovery_ticker(user_id: str, candidate, *,
                                 tier: str = "T1") -> FleetPick | None:
    """Grade ONE radar candidate into a FleetPick (analysts + light synthesis).

    Returns ``None`` when the analyst quorum fails (the ticker can't be grounded)
    — the funnel skips it rather than fabricating a verdict. Never persists a
    proposal."""
    ticker = _ticker_of(candidate)
    run_id = await open_decision_run_for_consult(
        user_id=user_id, ticker=ticker, tier_value=tier)
    try:
        result = await run_per_ticker_analysts(
            user_id=user_id, ticker=ticker, decision_run_id=run_id,
            mode="long_hold")
    except InsufficientAnalystQuorum as exc:
        await _close_decision_run(decision_run_id=run_id, status="blocked")
        log.info("discovery_grader.quorum_failed", ticker=ticker, reason=exc.reason)
        return None
    except Exception:
        await _close_decision_run(decision_run_id=run_id, status="blocked")
        raise

    agent = DiscoveryGraderAgent(user_id=user_id)
    out: FleetGradeOutput = (await agent.run(
        ticker=ticker, analyst_reports=list(result.reports))).output
    await _close_decision_run(decision_run_id=run_id, status="completed")

    return FleetPick(
        ticker=out.ticker or ticker, conviction=out.conviction,
        thesis_md=out.thesis_md, verdict=out.verdict, cites=tuple(out.cites),
    )


__all__ = [
    "FleetGradeOutput", "DiscoveryGraderAgent", "discovery_idempotency_key",
    "grade_discovery_ticker",
]
