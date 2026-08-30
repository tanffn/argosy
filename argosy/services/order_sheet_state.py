"""Portfolio stance context for the canonical order sheet.

Unlike ``position_stance``'s precedence projection, this loader preserves every
available upstream voice.  These rows predate the deployment decision and are
therefore marked ``prior_context``: disagreements remain visible to self-audit,
while the current author/reviewer run owns the executable decision.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from argosy.services.order_sheet import NoActionLine, VoiceVerdict
from argosy.state.models import PositionStance


def _aware(value: datetime | None, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def load_portfolio_voices(
    db: Any,
    *,
    user_id: str,
    portfolio_symbols: set[str],
    observed_at: datetime | None = None,
) -> dict[str, list[VoiceVerdict]]:
    """Load plan and review verdicts separately for every held symbol."""

    now = observed_at or datetime.now(UTC)
    symbols = {s.strip().upper() for s in portfolio_symbols if s.strip()}
    if not symbols:
        return {}
    rows = (
        db.execute(
            select(PositionStance).where(
                PositionStance.user_id == user_id,
                PositionStance.symbol.in_(sorted(symbols)),
            )
        )
        .scalars()
        .all()
    )
    out: dict[str, list[VoiceVerdict]] = {}
    for row in rows:
        symbol = row.symbol.strip().upper()
        as_of = _aware(row.built_at, now)
        voices: list[VoiceVerdict] = []
        if row.plan_verdict:
            voices.append(
                VoiceVerdict(
                    source=f"plan:{row.plan_version_id or 'unknown'}",
                    verdict=row.plan_verdict,
                    as_of=as_of,
                    rationale=row.reasoning_md or "plan verdict",
                    decision_scope="prior_context",
                )
            )
        if row.review_verdict:
            voices.append(
                VoiceVerdict(
                    source=f"review:{row.review_outcome or 'unknown'}",
                    verdict=row.review_verdict,
                    as_of=as_of,
                    rationale=row.reasoning_md or "portfolio review verdict",
                    decision_scope="prior_context",
                )
            )
        if not voices:
            voices.append(
                VoiceVerdict(
                    source=row.stance_source,
                    verdict=row.stance,
                    as_of=as_of,
                    rationale=row.reasoning_md or "canonical stance",
                    decision_scope="prior_context",
                )
            )
        out[symbol] = voices

    for symbol in symbols - set(out):
        out[symbol] = [
            VoiceVerdict(
                source="coverage_missing",
                verdict="HOLD",
                as_of=now,
                rationale="No current portfolio review/stance exists.",
                decision_scope="prior_context",
            )
        ]
    return out


def build_no_action_lines(
    *,
    holdings_usd: dict[str, float],
    acted_symbols: set[str],
    voices_by_symbol: dict[str, list[VoiceVerdict]],
) -> list[NoActionLine]:
    """Represent every untouched held security explicitly."""

    acted = {s.upper() for s in acted_symbols}
    lines: list[NoActionLine] = []
    for symbol in sorted({s.upper() for s in holdings_usd} - acted):
        voices = voices_by_symbol.get(symbol) or [
            VoiceVerdict(
                source="coverage_missing",
                verdict="HOLD",
                as_of=datetime.now(UTC),
                rationale="No current portfolio review/stance exists.",
            )
        ]
        verdicts = sorted({v.verdict for v in voices})
        if verdicts == ["HOLD"]:
            reason = "Prior portfolio context supports leaving the position unchanged."
        elif len(verdicts) > 1:
            reason = (
                f"Prior plan/review voices disagree {verdicts}; no order was authored "
                "for this symbol, and the disagreement is retained for self-audit."
            )
        else:
            reason = (
                f"Prior context says {verdicts[0]}; no order was authored for this "
                "symbol in the current run, so the prior verdict remains an audit item."
            )
        lines.append(
            NoActionLine(
                symbol=symbol,
                reason=reason,
                voices=voices,
            )
        )
    return lines


__all__ = ["build_no_action_lines", "load_portfolio_voices"]
